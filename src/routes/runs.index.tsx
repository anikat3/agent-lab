import { createFileRoute, Link } from "@tanstack/react-router";
import { useServerFn } from "@tanstack/react-start";
import { useAppQuery, useAuth } from "@/hooks/use-auth";
import { listRuns } from "@/lib/api/runs.functions";
import { DemoBadge, EmptyState, ErrorState, fmtDate, fmtMs, fmtPct, LoadingBlock, PageHeader, SignInPrompt, StatusPill } from "@/components/states";
import { Button } from "@/components/ui/button";

export const Route = createFileRoute("/runs/")({
  head: () => ({
    meta: [
      { title: "Results — AgentLab" },
      { name: "description", content: "All evaluation runs with status, pass rate and latency." },
      { property: "og:title", content: "Results — AgentLab" },
      { property: "og:description", content: "All evaluation runs with status, pass rate and latency." },
    ],
  }),
  component: RunsPage,
});

function RunsPage() {
  const { userId } = useAuth();
  const fetchRuns = useServerFn(listRuns);
  const q = useAppQuery(["runs"], () => fetchRuns(), {
    refetchInterval: (query) => ((query.state.data as { status: string }[] | undefined)?.some((r) => ["pending", "queued", "running"].includes(r.status)) ? 5000 : false),
  });
  return (
    <div>
      <PageHeader title="Results" description="Evaluation runs, newest first." actions={userId && <Button asChild size="sm"><Link to="/runs/new">Run evaluation</Link></Button>} />
      {!userId && <div className="mb-6"><SignInPrompt /></div>}
      {q.isLoading ? <LoadingBlock /> : q.isError ? <ErrorState error={q.error} onRetry={() => q.refetch()} /> : !q.data?.length ? (
        <EmptyState title="No runs yet" body="Launch an evaluation to see results here." />
      ) : (
        <div className="rounded-md border border-border bg-card">
          <table className="w-full text-sm">
            <thead className="border-b border-border text-left text-xs uppercase text-muted-foreground">
              <tr>{["Run", "Suite", "Agent", "Status", "Passed", "Pass rate", "Latency", "Created"].map((h) => <th key={h} className="px-4 py-2 font-medium">{h}</th>)}</tr>
            </thead>
            <tbody>
              {q.data.map((r) => (
                <tr key={r.id} className="border-b border-border last:border-0 hover:bg-accent/40">
                  <td className="px-4 py-2">
                    <Link to="/runs/$id" params={{ id: r.id }} className="font-mono text-xs hover:underline">{r.id.slice(0, 8)}</Link>
                    {r.is_demo && <DemoBadge className="ml-2" />}
                    {r.retry_of_run_id && <span className="ml-2 font-mono text-[10px] text-muted-foreground">retry</span>}
                  </td>
                  <td className="px-4 py-2">{r.test_suites?.name}</td>
                  <td className="px-4 py-2 text-muted-foreground">{(r.agent_snapshot as { name?: string })?.name}</td>
                  <td className="px-4 py-2"><StatusPill status={r.status} /></td>
                  <td className="px-4 py-2 font-mono">{r.passed}/{r.total}</td>
                  <td className="px-4 py-2 font-mono">{fmtPct(r.pass_rate)}</td>
                  <td className="px-4 py-2 font-mono">{fmtMs(r.avg_latency_ms)}</td>
                  <td className="px-4 py-2 text-muted-foreground">{fmtDate(r.created_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
