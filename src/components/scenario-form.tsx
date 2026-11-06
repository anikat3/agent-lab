import { useState, type FormEvent } from "react";
import { useMutation } from "@tanstack/react-query";
import { useServerFn } from "@tanstack/react-start";
import { toast } from "sonner";
import { createScenario, updateScenario } from "@/lib/api/suites.functions";
import { EVAL_TYPE_LABELS, EVAL_TYPES, scenarioInputSchema, type EvalType } from "@/lib/api/eval-config";
import type { Tables } from "@/integrations/supabase/types";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import { Switch } from "@/components/ui/switch";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";

type Scenario = Tables<"test_scenarios">;
type Cfg = Record<string, unknown>;

const splitList = (s: string) => s.split(",").map((x) => x.trim()).filter(Boolean);
const DEFAULT_MESSAGES = JSON.stringify([{ role: "user", content: "" }], null, 2);

export function ScenarioForm({ suiteId, initial, onDone }: { suiteId: string; initial?: Scenario | undefined; onDone: () => void }) {
  const cfg0 = (initial?.eval_config ?? {}) as Cfg;
  const [name, setName] = useState(initial?.name ?? "");
  const [category, setCategory] = useState(initial?.category ?? "");
  const [tags, setTags] = useState((initial?.tags ?? []).join(", "));
  const [messages, setMessages] = useState(initial ? JSON.stringify(initial.input_messages, null, 2) : DEFAULT_MESSAGES);
  const [expected, setExpected] = useState(initial?.expected_response ?? "");
  const [evalType, setEvalType] = useState<EvalType>((initial?.eval_type as EvalType) ?? "keyword");
  // type-specific fields
  const [required, setRequired] = useState(((cfg0["required"] as string[]) ?? []).join(", "));
  const [forbidden, setForbidden] = useState(((cfg0["forbidden"] as string[]) ?? []).join(", "));
  const [caseSensitive, setCaseSensitive] = useState(Boolean(cfg0["case_sensitive"]));
  const [exactText, setExactText] = useState((cfg0["expected"] as string) ?? "");
  const [normalizeWs, setNormalizeWs] = useState(cfg0["normalize_whitespace"] !== false);
  const [schemaText, setSchemaText] = useState(cfg0["schema"] ? JSON.stringify(cfg0["schema"], null, 2) : '{\n  "type": "object"\n}');
  const [rubric, setRubric] = useState((cfg0["rubric"] as string) ?? "");
  const [threshold, setThreshold] = useState(String(cfg0["pass_threshold"] ?? "0.7"));
  const [judgeModel, setJudgeModel] = useState((cfg0["judge_model"] as string) ?? "");
  const [errors, setErrors] = useState<string[]>([]);

  const create = useServerFn(createScenario);
  const update = useServerFn(updateScenario);
  const m = useMutation({
    mutationFn: (payload: Record<string, unknown>) =>
      initial ? update({ data: { id: initial.id, ...payload } as never }) : create({ data: payload as never }),
    onSuccess: () => {
      toast.success(initial ? "Scenario updated" : "Scenario created");
      onDone();
    },
  });

  function buildConfig(): Cfg | string {
    switch (evalType) {
      case "keyword": {
        const c: Cfg = { required: splitList(required), case_sensitive: caseSensitive };
        const f = splitList(forbidden);
        if (f.length) c["forbidden"] = f;
        return c;
      }
      case "exact":
        return { expected: exactText, normalize_whitespace: normalizeWs, case_sensitive: caseSensitive };
      case "json_schema":
        try {
          return { schema: JSON.parse(schemaText) };
        } catch {
          return "Schema is not valid JSON";
        }
      case "llm_judge": {
        const c: Cfg = { rubric, pass_threshold: Number(threshold) };
        if (judgeModel.trim()) c["judge_model"] = judgeModel.trim();
        return c;
      }
    }
  }

  function onSubmit(e: FormEvent) {
    e.preventDefault();
    let parsedMessages: unknown;
    try {
      parsedMessages = JSON.parse(messages);
    } catch {
      return setErrors(["Input messages must be valid JSON"]);
    }
    const config = buildConfig();
    if (typeof config === "string") return setErrors([config]);
    const payload = {
      suite_id: suiteId,
      name,
      category: category || null,
      input_messages: parsedMessages,
      expected_response: expected || null,
      eval_type: evalType,
      eval_config: config,
      tags: splitList(tags),
    };
    const r = scenarioInputSchema.safeParse(payload);
    if (!r.success) return setErrors(r.error.issues.map((i) => `${i.path.join(".") || "form"}: ${i.message}`));
    setErrors([]);
    m.mutate(payload);
  }

  return (
    <form onSubmit={onSubmit} className="space-y-3">
      <div className="grid grid-cols-2 gap-3">
        <div className="space-y-1.5"><Label>Name</Label><Input required value={name} onChange={(e) => setName(e.target.value)} /></div>
        <div className="space-y-1.5"><Label>Category</Label><Input value={category} onChange={(e) => setCategory(e.target.value)} placeholder="billing" /></div>
      </div>
      <div className="space-y-1.5"><Label>Tags (comma separated)</Label><Input value={tags} onChange={(e) => setTags(e.target.value)} /></div>
      <div className="space-y-1.5">
        <Label>Input messages (JSON array of {"{ role, content }"})</Label>
        <Textarea rows={5} className="font-mono text-xs" value={messages} onChange={(e) => setMessages(e.target.value)} />
      </div>
      <div className="space-y-1.5"><Label>Expected response (reference)</Label><Textarea rows={2} value={expected} onChange={(e) => setExpected(e.target.value)} /></div>
      <div className="space-y-1.5">
        <Label>Evaluation type</Label>
        <Select value={evalType} onValueChange={(v) => setEvalType(v as EvalType)}>
          <SelectTrigger><SelectValue /></SelectTrigger>
          <SelectContent>{EVAL_TYPES.map((t) => <SelectItem key={t} value={t}>{EVAL_TYPE_LABELS[t]}</SelectItem>)}</SelectContent>
        </Select>
      </div>
      <div className="space-y-3 rounded-md border border-border bg-muted/40 p-3">
        {evalType === "keyword" && (
          <>
            <div className="space-y-1.5"><Label>Required keywords</Label><Input value={required} onChange={(e) => setRequired(e.target.value)} placeholder="refund, 30 days" /></div>
            <div className="space-y-1.5"><Label>Forbidden keywords</Label><Input value={forbidden} onChange={(e) => setForbidden(e.target.value)} /></div>
            <Toggle label="Case sensitive" checked={caseSensitive} onChange={setCaseSensitive} />
          </>
        )}
        {evalType === "exact" && (
          <>
            <div className="space-y-1.5"><Label>Expected exact text</Label><Textarea rows={2} value={exactText} onChange={(e) => setExactText(e.target.value)} /></div>
            <Toggle label="Normalize whitespace" checked={normalizeWs} onChange={setNormalizeWs} />
            <Toggle label="Case sensitive" checked={caseSensitive} onChange={setCaseSensitive} />
          </>
        )}
        {evalType === "json_schema" && (
          <div className="space-y-1.5"><Label>JSON Schema</Label><Textarea rows={8} className="font-mono text-xs" value={schemaText} onChange={(e) => setSchemaText(e.target.value)} /></div>
        )}
        {evalType === "llm_judge" && (
          <>
            <div className="space-y-1.5"><Label>Rubric</Label><Textarea rows={3} value={rubric} onChange={(e) => setRubric(e.target.value)} /></div>
            <div className="grid grid-cols-2 gap-3">
              <div className="space-y-1.5"><Label>Pass threshold (0–1)</Label><Input type="number" step="0.05" min="0" max="1" value={threshold} onChange={(e) => setThreshold(e.target.value)} /></div>
              <div className="space-y-1.5"><Label>Judge model (optional)</Label><Input value={judgeModel} onChange={(e) => setJudgeModel(e.target.value)} /></div>
            </div>
          </>
        )}
      </div>
      {(errors.length > 0 || m.isError) && (
        <ul role="alert" className="space-y-1 rounded-md border border-destructive/40 bg-destructive/10 p-3 font-mono text-xs text-destructive">
          {errors.map((e) => <li key={e}>{e}</li>)}
          {m.isError && <li>{(m.error as Error).message}</li>}
        </ul>
      )}
      <div className="flex justify-end"><Button type="submit" disabled={m.isPending}>{m.isPending ? "Saving…" : "Save scenario"}</Button></div>
    </form>
  );
}

function Toggle({ label, checked, onChange }: { label: string; checked: boolean; onChange: (v: boolean) => void }) {
  return (
    <label className="flex items-center gap-2 text-sm">
      <Switch checked={checked} onCheckedChange={onChange} /> {label}
    </label>
  );
}
