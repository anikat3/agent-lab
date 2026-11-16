import { z } from "zod";

/** Shared (client + server) validation for scenarios and their evaluation configs. */
export const EVAL_TYPES = ["keyword", "exact", "json_schema", "llm_judge"] as const;
export type EvalType = (typeof EVAL_TYPES)[number];

export const EVAL_TYPE_LABELS: Record<EvalType, string> = {
  keyword: "Keyword match",
  exact: "Exact match",
  json_schema: "JSON schema",
  llm_judge: "LLM-as-judge",
};

const nonEmpty = z.string().trim().min(1);

export const keywordConfigSchema = z
  .object({
    required: z.array(nonEmpty).min(1, "Add at least one required keyword"),
    forbidden: z.array(nonEmpty).optional(),
    case_sensitive: z.boolean().optional(),
  })
  .strict();

export const exactConfigSchema = z
  .object({
    expected: z.string().min(1, "Expected text is required"),
    normalize_whitespace: z.boolean().optional(),
    case_sensitive: z.boolean().optional(),
  })
  .strict();

const JSON_TYPES = ["object", "array", "string", "number", "integer", "boolean", "null"];

/** Lightweight structural check that a value looks like a JSON Schema. Returns an error or null. */
export function jsonSchemaProblem(schema: unknown): string | null {
  if (!schema || typeof schema !== "object" || Array.isArray(schema)) {
    return "Schema must be a JSON object";
  }
  const s = schema as Record<string, unknown>;
  if (Object.keys(s).length === 0) return "Schema must not be empty";
  if ("type" in s) {
    const t = s["type"];
    const types = Array.isArray(t) ? t : [t];
    if (!types.every((x) => typeof x === "string" && JSON_TYPES.includes(x))) {
      return `"type" must be one of: ${JSON_TYPES.join(", ")}`;
    }
  }
  if ("properties" in s) {
    const p = s["properties"];
    if (!p || typeof p !== "object" || Array.isArray(p)) return '"properties" must be an object';
    for (const [k, v] of Object.entries(p)) {
      if (!v || typeof v !== "object" || Array.isArray(v)) return `Property "${k}" must be a schema object`;
    }
  }
  if ("required" in s) {
    if (!Array.isArray(s["required"]) || !(s["required"] as unknown[]).every((x) => typeof x === "string")) {
      return '"required" must be an array of strings';
    }
  }
  return null;
}

export const jsonSchemaConfigSchema = z
  .object({ schema: z.record(z.unknown()) })
  .strict()
  .superRefine((v, ctx) => {
    const problem = jsonSchemaProblem(v.schema);
    if (problem) ctx.addIssue({ code: "custom", path: ["schema"], message: problem });
  });

export const llmJudgeConfigSchema = z
  .object({
    rubric: z.string().trim().min(10, "Rubric must be at least 10 characters"),
    pass_threshold: z
      .number({ invalid_type_error: "Pass threshold must be a number" })
      .min(0, "Pass threshold must be between 0 and 1")
      .max(1, "Pass threshold must be between 0 and 1"),
    judge_model: z.string().trim().min(1).optional(),
  })
  .strict();

/**
 * What the evaluation engine assumes when an optional flag is omitted. The scenario form
 * starts from these, so a new scenario behaves the same whether or not the flag is sent.
 */
export const EVAL_FLAG_DEFAULTS = {
  keyword: { case_sensitive: false },
  exact: { case_sensitive: true, normalize_whitespace: false },
} as const;

/**
 * Initial toggle values for the scenario form. A saved value is only reused when the
 * scenario being edited already has that eval type; otherwise the type's default applies.
 */
export function initialEvalFlags(evalType: string | undefined, config: Record<string, unknown>) {
  const flag = (type: EvalType, key: string, fallback: boolean): boolean =>
    evalType === type && typeof config[key] === "boolean" ? (config[key] as boolean) : fallback;
  return {
    keywordCaseSensitive: flag(
      "keyword",
      "case_sensitive",
      EVAL_FLAG_DEFAULTS.keyword.case_sensitive,
    ),
    exactCaseSensitive: flag("exact", "case_sensitive", EVAL_FLAG_DEFAULTS.exact.case_sensitive),
    exactNormalizeWhitespace: flag(
      "exact",
      "normalize_whitespace",
      EVAL_FLAG_DEFAULTS.exact.normalize_whitespace,
    ),
  };
}

export const evalConfigSchemas = {
  keyword: keywordConfigSchema,
  exact: exactConfigSchema,
  json_schema: jsonSchemaConfigSchema,
  llm_judge: llmJudgeConfigSchema,
} as const;

export type EvalConfigResult =
  | { ok: true; config: Record<string, unknown> }
  | { ok: false; errors: string[] };

export function validateEvalConfig(type: string, config: unknown): EvalConfigResult {
  if (!(EVAL_TYPES as readonly string[]).includes(type)) {
    return { ok: false, errors: [`Unknown evaluation type "${type}"`] };
  }
  const parsed = evalConfigSchemas[type as EvalType].safeParse(config);
  if (parsed.success) return { ok: true, config: parsed.data as Record<string, unknown> };
  return {
    ok: false,
    errors: parsed.error.issues.map((i) => (i.path.length ? `${i.path.join(".")}: ${i.message}` : i.message)),
  };
}

export const messageSchema = z.object({
  role: z.enum(["system", "user", "assistant"]),
  content: z.string().min(1, "Message content is required"),
});

export const scenarioInputSchema = z
  .object({
    suite_id: z.string().uuid(),
    name: nonEmpty.max(200),
    category: z.string().trim().max(100).nullable().default(null),
    input_messages: z.array(messageSchema).min(1, "Add at least one message"),
    expected_response: z.string().nullable().default(null),
    eval_type: z.enum(EVAL_TYPES),
    eval_config: z.unknown(),
    tags: z.array(z.string().trim().min(1)).max(20).default([]),
  })
  .superRefine((v, ctx) => {
    const r = validateEvalConfig(v.eval_type, v.eval_config);
    if (!r.ok) r.errors.forEach((m) => ctx.addIssue({ code: "custom", path: ["eval_config"], message: m }));
  });

export type ScenarioInput = z.infer<typeof scenarioInputSchema>;

export const suiteInputSchema = z.object({
  name: nonEmpty.max(200),
  description: z.string().max(2000).nullable().default(null),
});

export const agentInputSchema = z.object({
  name: nonEmpty.max(200),
  system_prompt: z.string().max(20000),
  model: nonEmpty.max(200),
});

export const launchInputSchema = z.object({
  suite_id: z.string().uuid(),
  agent_config_id: z.string().uuid(),
  timeout_seconds: z.number().int().min(10).max(3600).default(300),
  max_retries: z.number().int().min(0).max(5).default(2),
});
export type LaunchInput = z.infer<typeof launchInputSchema>;
