import { z } from "zod";
import { EVAL_TYPES, messageSchema } from "./eval-config";
import { RUN_STATUSES } from "./engine-logic";

/**
 * REST contract with the external Python evaluation engine (FastAPI + LangGraph).
 * Mirrors docs/api-contract.md — keep the two in sync.
 */

export const launchRequestSchema = z.object({
  run_id: z.string().uuid(),
  retry_of_run_id: z.string().uuid().nullable(),
  timeout_seconds: z.number().int(),
  agent: z.object({ id: z.string(), name: z.string(), system_prompt: z.string(), model: z.string() }),
  suite: z.object({ id: z.string(), name: z.string() }),
  scenarios: z.array(
    z.object({
      id: z.string(),
      name: z.string(),
      input_messages: z.array(messageSchema),
      expected_response: z.string().nullable(),
      eval_type: z.enum(EVAL_TYPES),
      eval_config: z.record(z.unknown()),
    }),
  ),
});
export type LaunchRequest = z.infer<typeof launchRequestSchema>;

export const launchResponseSchema = z.object({
  external_run_id: z.string().min(1),
  status: z.literal("queued"),
});

export const engineResultSchema = z.object({
  scenario_id: z.string(),
  passed: z.boolean(),
  quality_score: z.number().min(0).max(1).nullable().optional(),
  latency_ms: z.number().int().nonnegative().nullable().optional(),
  actual_response: z.string().nullable().optional(),
  failure_category: z.string().nullable().optional(),
  failure_explanation: z.string().nullable().optional(),
  evaluator_details: z.record(z.unknown()).nullable().optional(),
});

export const runStatusResponseSchema = z.object({
  external_run_id: z.string(),
  status: z.enum(RUN_STATUSES),
  started_at: z.string().nullable().optional(),
  completed_at: z.string().nullable().optional(),
  progress: z.object({ completed: z.number().int(), total: z.number().int() }),
  results: z.array(engineResultSchema).default([]),
  error: z.object({ code: z.string(), message: z.string() }).nullable().optional(),
});
export type RunStatusResponse = z.infer<typeof runStatusResponseSchema>;

export const healthResponseSchema = z.object({ status: z.literal("ok"), version: z.string().optional() });

export const engineErrorBodySchema = z.object({
  error: z.object({ code: z.string(), message: z.string(), details: z.record(z.unknown()).optional() }),
});

/** Error codes surfaced by this app (engine-side codes are included in the message). */
export const APP_ERROR_CODES = [
  "ENGINE_NOT_CONFIGURED",
  "ENGINE_UNREACHABLE",
  "ENGINE_TIMEOUT",
  "ENGINE_AUTH_FAILED",
  "ENGINE_BAD_REQUEST",
  "ENGINE_ERROR",
  "RUN_TIMEOUT",
  "RATE_LIMITED",
  "INVALID_REQUEST",
  "INVALID_EVAL_CONFIG",
  "NOT_FOUND",
] as const;
export type AppErrorCode = (typeof APP_ERROR_CODES)[number];

export const ERROR_TITLES: Record<AppErrorCode, string> = {
  ENGINE_NOT_CONFIGURED: "Evaluation engine not connected",
  ENGINE_UNREACHABLE: "Evaluation engine unreachable",
  ENGINE_TIMEOUT: "Evaluation engine timed out",
  ENGINE_AUTH_FAILED: "Engine rejected the API key",
  ENGINE_BAD_REQUEST: "Engine rejected the request",
  ENGINE_ERROR: "Evaluation engine error",
  RUN_TIMEOUT: "Run exceeded its timeout",
  RATE_LIMITED: "Too many launches",
  INVALID_REQUEST: "Invalid request",
  INVALID_EVAL_CONFIG: "Invalid evaluation config",
  NOT_FOUND: "Not found",
};

export function errorTitle(code: string | null | undefined): string {
  return (code && ERROR_TITLES[code as AppErrorCode]) || "Error";
}

export type LaunchResult =
  | { ok: true; runId: string; status: "queued" }
  | { ok: false; runId: string | null; code: AppErrorCode; message: string };
