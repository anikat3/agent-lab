import { useState } from "react";
import { createFileRoute, Link, useNavigate } from "@tanstack/react-router";
import { useServerFn } from "@tanstack/react-start";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { useAppQuery, useAuth } from "@/hooks/use-auth";
import { listSuites } from "@/lib/api/suites.functions";
import { listAgents } from "@/lib/api/agents.functions";
import { launchRun } from "@/lib/api/runs.functions";
import { launchInputSchema } from "@/lib/api/eval-config";
import { RATE_LIMIT } from "@/lib/api/engine-logic";
import type { LaunchResult } from "@/lib/api/types";
import { ErrorState, LoadingBlock, PageHeader, SignInPrompt } from "@/components/states";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";

export const Route = createFileRoute("/runs/new")({
  head: () => ({
    meta: [
      { title: "Run evaluation — AgentLab" },
      { name: "description", content: "Choose a test suite and agent and launch an evaluation on your engine." },
      { property: "og:title", content: "Run evaluation — AgentLab" },
      { property: "og:description", content: "Launch an evaluation run." },
    ],
  }),
  component: NewRun,
});

function NewRun() {
  const { userId } = useAuth();
  const navigate = useNavigate();
  const qc = useQueryClient();
  const fetchSuites = useServerFn(listSuites);
  const fetchAgents = useServerFn(listAgents);
  const launch = useServerFn(launchRun);
  const suites = useAppQuery(["suites"], () => fetchSuites());
  const agents = useAppQuery(["agents"], () => fetchAgents());
  const [suiteId, setSuiteId] = useState("");
  const [agentId, setAgentId] = useState("");
  const [timeout, setTimeoutS] = useState("300");
  const [retries, setRetries] = useState("2");
  const [formError, setFormError] = useState<string | null>(null);
  const [result, setResult] = useState<LaunchResult | null>(null);

  const m = useMutation({
    mutationFn: (d: { suite_id: string; agent_config_id: string; timeout_seconds: number; max_retries: number }) => launch({ data: d }),
    onSuccess: (r) => {
      setResult(r);
      qc.invalidateQueries({ queryKey: ["runs"] });
      if (r.ok) {
        toast.success("Evaluation queued on the engine");
        navigate({ to: "/runs/$id", params: { id: r.runId } });
      }
    },
  });

  if (!userId) {
    return (
      <div>
        <PageHeader title="Run evaluation" />
        <SignInPrompt text="Sign in to launch evaluations." />
      </div>
    );
  }

  function submit() {
    const d = { suite_id: suiteId, agent_config_id: agentId, timeout_seconds: Number(timeout), max_retries: Number(retries) };
    const r = launchInputSchema.safeParse(d);
    if (!r.success) return setFormError(r.error.issues.map((i) => `${i.path.join(".")}: ${i.message}`).join("; "));
    setFormError(null);
    setResult(null);
    m.mutate(r.data);
  }

  return (
    <div className="max-w-2xl">
      <PageHeader title="Run evaluation" description="The run is executed by your external evaluation engine. Nothing is simulated here." />
      {suites.isLoading || agents.isLoading ? <LoadingBlock rows={3} /> : suites.isError || agents.isError ? (
        <ErrorState error={suites.error ?? agents.error} onRetry={() => { suites.refetch(); agents.refetch(); }} />
      ) : (
        <div className="space-y-4 rounded-md border border-border bg-card p-5">
          <div className="space-y-1.5">
            <Label>Test suite</Label>
            <Select value={suiteId} onValueChange={setSuiteId}>
              <SelectTrigger><SelectValue placeholder="Select a suite" /></SelectTrigger>
              <SelectContent>
                {suites.data?.map((s) => <SelectItem key={s.id} value={s.id}>{s.name} ({s.scenario_count} scenarios)</SelectItem>)}
              </SelectContent>
            </Select>
          </div>
          <div className="space-y-1.5">
            <Label>Agent configuration</Label>
            <Select value={agentId} onValueChange={setAgentId}>
              <SelectTrigger><SelectValue placeholder="Select an agent" /></SelectTrigger>
              <SelectContent>
                {agents.data?.map((a) => <SelectItem key={a.id} value={a.id}>{a.name} · {a.model}</SelectItem>)}
              </SelectContent>
            </Select>
            {agents.data?.length === 0 && <p className="text-xs text-muted-foreground">No agents yet. <Link to="/agents" className="underline">Create one</Link>.</p>}
          </div>
          <div className="grid grid-cols-2 gap-3">
            <div className="space-y-1.5">
              <Label>Execution timeout (seconds)</Label>
              <Input type="number" min={10} max={3600} value={timeout} onChange={(e) => setTimeoutS(e.target.value)} />
              <p className="text-xs text-muted-foreground">Enforced by the engine.</p>
            </div>
            <div className="space-y-1.5">
              <Label>Launch retries (0–5)</Label>
              <Input type="number" min={0} max={5} value={retries} onChange={(e) => setRetries(e.target.value)} />
              <p className="text-xs text-muted-foreground">On network errors, 5xx and 429.</p>
            </div>
          </div>
          {formError && <p role="alert" className="font-mono text-xs text-destructive">{formError}</p>}
          {m.isError && <ErrorState error={m.error} />}
          {result && !result.ok && (
            <div className="space-y-2">
              <ErrorState error={result.message} code={result.code} />
              {result.runId && <Link to="/runs/$id" params={{ id: result.runId }} className="text-xs underline">View failed run</Link>}
            </div>
          )}
          <div className="flex items-center justify-between">
            <p className="text-xs text-muted-foreground">Limit: {RATE_LIMIT.maxPerWindow} launches / {RATE_LIMIT.windowSeconds}s</p>
            <Button onClick={submit} disabled={m.isPending}>{m.isPending ? "Launching… (retrying if needed)" : "Launch evaluation"}</Button>
          </div>
        </div>
      )}
    </div>
  );
}
