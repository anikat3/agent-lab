export interface RunForMetrics {
  id: string;
  created_at: string;
  status: string;
  pass_rate: number | null;
  avg_latency_ms: number | null;
}

export function computeDashboard(runs: RunForMetrics[], failures: { failure_category: string | null }[]) {
  const completed = runs.filter((r) => r.status === "completed" && r.pass_rate != null);
  const mean = (xs: number[]) => (xs.length ? xs.reduce((a, b) => a + b, 0) / xs.length : null);
  const latencies = completed.map((r) => r.avg_latency_ms).filter((x): x is number => x != null).map(Number);
  const counts = new Map<string, number>();
  for (const f of failures) {
    const k = f.failure_category || "uncategorized";
    counts.set(k, (counts.get(k) ?? 0) + 1);
  }
  return {
    totalRuns: runs.length,
    completedRuns: completed.length,
    failedRuns: runs.filter((r) => r.status === "failed").length,
    avgPassRate: mean(completed.map((r) => Number(r.pass_rate))),
    avgLatencyMs: mean(latencies),
    failedTests: failures.length,
    trend: [...completed]
      .sort((a, b) => a.created_at.localeCompare(b.created_at))
      .map((r) => ({ date: r.created_at, passRate: Math.round(Number(r.pass_rate) * 1000) / 10 })),
    categories: [...counts.entries()].map(([category, count]) => ({ category, count })).sort((a, b) => b.count - a.count),
  };
}
