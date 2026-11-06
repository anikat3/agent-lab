import { useState } from "react";
import { createFileRoute, Link } from "@tanstack/react-router";
import { useServerFn } from "@tanstack/react-start";
import { Bar, BarChart, CartesianGrid, Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { useAppQuery, useAuth } from "@/hooks/use-auth";
import { getDashboard } from "@/lib/api/runs.functions";
import {
  DemoBadge, EmptyState, ErrorState, fmtDate, fmtMs, fmtPct, LoadingBlock, PageHeader, SignInPrompt, StatusPill,
} from "@/components/states";
import { Switch } from "@/components/ui/switch";
import { Label } from "@/components/ui/label";
import { Button } from "@/components/ui/button";

export const Route = createFileRoute("/")({
  head: () => ({
    meta: [
      { title: "Dashboard — AgentLab" },
      { name: "description", content: "Pass rates, latency and failure categories across your AI agent evaluation runs." },
      { property: "og:title", content: "AgentLab Dashboard" },
      { property: "og:description", content: "Evaluation metrics for conversational AI agents." },
    ],
  }),
  component: Dashboard,
});

const tooltipStyle = {
  background: "var(--popover)",
  border: "1px solid var(--border)",
  borderRadius: 6,
  fontSize: 12,
  color: "var(--popover-foreground)",
};

function Kpi({ label, value, hint }: { label: string; value: string; hint?: string }) {
  return (
    <div className="rounded-md border border-border bg-card p-4">
      <p className="text-xs uppercase tracking-wide text-muted-foreground">{label}</p>
      <p className="mt-2 font-mono text-2xl font-medium">{value}</p>
      {hint && <p className="mt-1 text-xs text-muted-foreground">{hint}</p>}
    </div>
  );
}

function Dashboard() {
  const { userId } = useAuth();
  const [demo, setDemo] = useState(false);
  const fetchDashboard = useServerFn(getDashboard);
  const q = useAppQuery(["dashboard", demo], () => fetchDashboard({ data: { demo } }));
  const d = q.data;
  const showingDemo = d?.demo ?? !userId;

  return (
    <div>
      <PageHeader
        title="Dashboard"
        description={showingDemo ? "Showing synthetic demo metrics only." : "Metrics from real evaluation runs (demo data excluded)."}
        actions={
          <div className="flex items-center gap-2">
            <Switch id="demo" checked={showingDemo} disabled={!userId} onCheckedChange={setDemo} />
            <Label htmlFor="demo" className="text-sm">Show demo metrics</Label>
          </div>
        }
      />
      {!userId && <div className="mb-6"><SignInPrompt /></div>}
      {q.isLoading || !d ? (
        q.isError ? <ErrorState error={q.error} onRetry={() => q.refetch()} /> : <LoadingBlock rows={6} />
      ) : d.metrics.totalRuns === 0 ? (
        <EmptyState
          title="No real evaluation runs yet"
          body="Launch an evaluation against your engine, or toggle demo metrics to preview the dashboard."
          action={<Button asChild size="sm"><Link to="/runs/new">Run evaluation</Link></Button>}
        />
      ) : (
        <div className="space-y-6">
          {showingDemo && <DemoBadge />}
          <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
            <Kpi label="Total runs" value={String(d.metrics.totalRuns)} hint={`${d.metrics.completedRuns} completed · ${d.metrics.failedRuns} failed`} />
            <Kpi label="Avg pass rate" value={fmtPct(d.metrics.avgPassRate)} hint="completed runs" />
            <Kpi label="Avg latency" value={fmtMs(d.metrics.avgLatencyMs)} hint="per test" />
            <Kpi label="Failed tests" value={String(d.metrics.failedTests)} hint={`across ${d.metrics.totalRuns} runs`} />
          </div>
          <div className="grid gap-3 lg:grid-cols-2">
            <div className="rounded-md border border-border bg-card p-4">
              <p className="mb-3 text-sm font-medium">Pass rate over time</p>
              <div className="h-56">
                <ResponsiveContainer>
                  <LineChart data={d.metrics.trend}>
                    <CartesianGrid stroke="var(--border)" strokeDasharray="3 3" />
                    <XAxis dataKey="date" tickFormatter={(v) => new Date(v).toLocaleDateString()} stroke="var(--muted-foreground)" fontSize={11} />
                    <YAxis domain={[0, 100]} unit="%" stroke="var(--muted-foreground)" fontSize={11} />
                    <Tooltip contentStyle={tooltipStyle} labelFormatter={(v) => fmtDate(String(v))} />
                    <Line type="monotone" dataKey="passRate" name="Pass rate %" stroke="var(--chart-1)" strokeWidth={2} dot />
                  </LineChart>
                </ResponsiveContainer>
              </div>
            </div>
            <div className="rounded-md border border-border bg-card p-4">
              <p className="mb-3 text-sm font-medium">Failure categories</p>
              <div className="h-56">
                {d.metrics.categories.length === 0 ? (
                  <p className="text-sm text-muted-foreground">No failures recorded.</p>
                ) : (
                  <ResponsiveContainer>
                    <BarChart data={d.metrics.categories} layout="vertical" margin={{ left: 24 }}>
                      <CartesianGrid stroke="var(--border)" strokeDasharray="3 3" />
                      <XAxis type="number" allowDecimals={false} stroke="var(--muted-foreground)" fontSize={11} />
                      <YAxis type="category" dataKey="category" stroke="var(--muted-foreground)" fontSize={11} width={110} />
                      <Tooltip contentStyle={tooltipStyle} cursor={{ fill: "var(--accent)" }} />
                      <Bar dataKey="count" fill="var(--chart-3)" radius={[0, 3, 3, 0]} />
                    </BarChart>
                  </ResponsiveContainer>
                )}
              </div>
            </div>
          </div>
          <div className="rounded-md border border-border bg-card">
            <p className="border-b border-border px-4 py-3 text-sm font-medium">Recent runs</p>
            <table className="w-full text-sm">
              <tbody>
                {d.recentRuns.map((r) => (
                  <tr key={r.id} className="border-b border-border last:border-0 hover:bg-accent/40">
                    <td className="px-4 py-2">
                      <Link to="/runs/$id" params={{ id: r.id }} className="font-mono text-xs hover:underline">
                        {r.id.slice(0, 8)}
                      </Link>
                    </td>
                    <td className="px-4 py-2">{r.test_suites?.name}</td>
                    <td className="px-4 py-2 text-muted-foreground">{(r.agent_snapshot as { name?: string })?.name}</td>
                    <td className="px-4 py-2"><StatusPill status={r.status} /></td>
                    <td className="px-4 py-2 font-mono">{fmtPct(r.pass_rate)}</td>
                    <td className="px-4 py-2 text-muted-foreground">{fmtDate(r.created_at)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}
    </div>
  );
}
