import { useEffect } from "react";
import { createFileRoute, Link, useNavigate } from "@tanstack/react-router";
import { useServerFn } from "@tanstack/react-start";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { RefreshCw, RotateCcw } from "lucide-react";
import { useAppQuery, useAuth } from "@/hooks/use-auth";
import { getRun, retryRun, syncRun } from "@/lib/api/runs.functions";
import { isActive, type RunStatus } from "@/lib/api/engine-logic";
import { EVAL_TYPE_LABELS, type EvalType } from "@/lib/api/eval-config";
import { DemoBadge, ErrorState, fmtDate, fmtMs, fmtPct, LoadingBlock, PageHeader, StatusPill } from "@/components/states";
import { Button } from "@/components/ui/button";

export const Route = createFileRoute("/runs/$id")({
  head: () => ({
    meta: [
      { title: "Run results — AgentLab" },
      { name: "description", content: "Per-test outcomes, quality scores, latency and failure explanations." },
      { property: "og:title", content: "Run results — AgentLab" },
      { property: "og:description", content: "Per-test evaluation outcomes." },
    ],
  }),
  component: RunDetail,
});

function RunDetail() {
  const { id } = Route.useParams();
  const { userId } = useAuth();
  const qc = useQueryClient();
  const navigate = useNavigate();
  const fetchRun = useServerFn(getRun);
  const sync = useServerFn(syncRun);
  const retry = useServerFn(retryRun);

  const q = useAppQuery(["run", id], () => fetchRun({ data: { id } }));
  const run = q.data?.run;
  const active = !!run && isActive(run.status as RunStatus) && !run.is_demo;

  const syncQ = useAppQuery(["run-sync", id], () => sync({ data: { id } }), {
    enabled: active && !!userId,
    refetchInterval: active ? 3000 : false,
  });
  useEffect(() => {
    if (syncQ.dataUpdatedAt) qc.invalidateQueries({ queryKey: ["run", id] });
  }, [syncQ.dataUpdatedAt, qc, id]);

  const retryM = useMutation({
    mutationFn: () => retry({ data: { id } }),
    onSuccess: (r) => {
      qc.invalidateQueries({ queryKey: ["runs"] });
      qc.invalidateQueries({ queryKey: ["run", id] });
      if (r.ok) toast.success("Retry queued as a new run");
      else toast.error(`${r.code}: ${r.message}`);
      if (r.runId) navigate({ to: "/runs/$id", params: { id: r.runId } });
    },
    onError: (e) => toast.error((e as Error).message),
  });

  if (q.isLoading) return <LoadingBlock rows={8} />;
  if (q.isError || !q.data || !run) return <ErrorState error={q.error ?? "Not found"} onRetry={() => q.refetch()} />;
  const { results, retries } = q.data;
  const agent = run.agent_snapshot as { name?: string; model?: string };
  const syncError = syncQ.data?.error;

  return (
    <div>
      <Link to="/runs" className="text-xs text-muted-foreground hover:underline">← Results</Link>
      <PageHeader
        title={`Run ${run.id.slice(0, 8)}`}
        description={`${run.test_suites?.name ?? ""} · ${agent.name ?? "agent"} (${agent.model ?? "?"})`}
        actions={
          <>
            {run.is_demo && <DemoBadge />}
            <StatusPill status={run.status} />
            {userId && active && (
              <Button size="sm" variant="outline" onClick={() => syncQ.refetch()} disabled={syncQ.isFetching}>
                <RefreshCw className={`h-3.5 w-3.5 ${syncQ.isFetching ? "animate-spin" : ""}`} /> Refresh status
              </Button>
            )}
            {userId && run.status === "failed" && !run.is_demo && (
              <Button size="sm" onClick={() => retryM.mutate()} disabled={retryM.isPending}>
                <RotateCcw className="h-3.5 w-3.5" /> {retryM.isPending ? "Retrying…" : "Retry run"}
              </Button>
            )}
          </>
        }
      />

      <div className="mb-6 space-y-3">
        {run.error_code && <ErrorState error={run.error_message ?? ""} code={run.error_code} />}
        {syncError && <ErrorState error={syncError.message} code={syncError.code} onRetry={() => syncQ.refetch()} />}
        {syncQ.isError && <ErrorState error={syncQ.error} onRetry={() => syncQ.refetch()} />}
        {(run.retry_of_run_id || retries.length > 0) && (
          <div className="flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
            {run.retry_of_run_id && (
              <span>Retry of <Link to="/runs/$id" params={{ id: run.retry_of_run_id }} className="font-mono underline">{run.retry_of_run_id.slice(0, 8)}</Link></span>
            )}
            {retries.map((r) => (
              <span key={r.id}>Retried as <Link to="/runs/$id" params={{ id: r.id }} className="font-mono underline">{r.id.slice(0, 8)}</Link> ({r.status})</span>
            ))}
          </div>
        )}
      </div>

      <div className="mb-6 grid grid-cols-2 gap-3 md:grid-cols-5">
        {[
          ["Passed", `${run.passed}/${run.total}`],
          ["Pass rate", fmtPct(run.pass_rate)],
          ["Avg latency", fmtMs(run.avg_latency_ms)],
          ["Launch attempts", String(run.attempt_count)],
          ["Started", fmtDate(run.started_at)],
        ].map(([k, v]) => (
          <div key={k} className="rounded-md border border-border bg-card p-3">
            <p className="text-xs uppercase text-muted-foreground">{k}</p>
            <p className="mt-1 font-mono text-sm">{v}</p>
          </div>
        ))}
      </div>

      {results.length === 0 ? (
        <p className="rounded-md border border-dashed border-border p-6 text-center text-sm text-muted-foreground">
          {active ? "Waiting for results from the engine…" : "No results recorded for this run."}
        </p>
      ) : (
        <div className="space-y-2">
          {results.map((r) => (
            <details key={r.id} className="group rounded-md border border-border bg-card">
              <summary className="flex cursor-pointer list-none items-center gap-3 px-4 py-3 text-sm">
                <StatusPill status={r.passed ? "pass" : "fail"} />
                <span className="flex-1 font-medium">{r.test_scenarios?.name}</span>
                {r.test_scenarios?.eval_type && (
                  <span className="hidden rounded bg-secondary px-1.5 py-0.5 font-mono text-[11px] md:inline">{EVAL_TYPE_LABELS[r.test_scenarios.eval_type as EvalType]}</span>
                )}
                {r.failure_category && <span className="font-mono text-xs text-destructive">{r.failure_category}</span>}
                <span className="w-16 text-right font-mono text-xs text-muted-foreground">{r.quality_score != null ? Number(r.quality_score).toFixed(2) : "—"}</span>
                <span className="w-16 text-right font-mono text-xs text-muted-foreground">{fmtMs(r.latency_ms)}</span>
              </summary>
              <div className="grid gap-3 border-t border-border p-4 text-sm md:grid-cols-2">
                {r.failure_explanation && (
                  <div className="md:col-span-2 rounded border border-destructive/30 bg-destructive/10 p-3 text-xs">{r.failure_explanation}</div>
                )}
                <div>
                  <p className="mb-1 text-xs uppercase text-muted-foreground">Expected</p>
                  <pre className="whitespace-pre-wrap rounded bg-muted p-2 font-mono text-xs">{r.test_scenarios?.expected_response ?? "(defined by evaluation config)"}</pre>
                </div>
                <div>
                  <p className="mb-1 text-xs uppercase text-muted-foreground">Actual</p>
                  <pre className="whitespace-pre-wrap rounded bg-muted p-2 font-mono text-xs">{r.actual_response ?? "—"}</pre>
                </div>
                {r.evaluator_details && (
                  <div className="md:col-span-2">
                    <p className="mb-1 text-xs uppercase text-muted-foreground">Evaluator details</p>
                    <pre className="overflow-x-auto rounded bg-muted p-2 font-mono text-xs">{JSON.stringify(r.evaluator_details, null, 2)}</pre>
                  </div>
                )}
              </div>
            </details>
          ))}
        </div>
      )}
    </div>
  );
}
