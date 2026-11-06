import { createServerFn } from "@tanstack/react-start";
import { z } from "zod";
import { requireSupabaseAuth } from "@/integrations/supabase/auth-middleware";
import { optionalAuth } from "./optional-auth";
import { launchInputSchema } from "./eval-config";
import { effectiveDemo } from "./engine-logic";
import { computeDashboard } from "./metrics";
import type { LaunchResult } from "./types";

const idSchema = z.object({ id: z.string().uuid() });

export const listRuns = createServerFn({ method: "GET" })
  .middleware([optionalAuth])
  .handler(async ({ context }) => {
    let q = context.supabase
      .from("eval_runs")
      .select("*, test_suites(name)")
      .order("created_at", { ascending: false })
      .limit(100);
    if (!context.userId) q = q.eq("is_demo", true);
    const { data, error } = await q;
    if (error) throw new Error(error.message);
    return data ?? [];
  });

export const getRun = createServerFn({ method: "GET" })
  .middleware([optionalAuth])
  .inputValidator((d) => idSchema.parse(d))
  .handler(async ({ data, context }) => {
    let q = context.supabase.from("eval_runs").select("*, test_suites(name)").eq("id", data.id);
    if (!context.userId) q = q.eq("is_demo", true);
    const { data: run, error } = await q.maybeSingle();
    if (error) throw new Error(error.message);
    if (!run) throw new Error("Run not found (or sign in required)");
    const [{ data: results, error: e2 }, { data: retries }] = await Promise.all([
      context.supabase
        .from("eval_results")
        .select("*, test_scenarios(name, eval_type, expected_response, category)")
        .eq("run_id", data.id)
        .order("passed")
        .order("created_at"),
      context.supabase.from("eval_runs").select("id, status, created_at").eq("retry_of_run_id", data.id).order("created_at"),
    ]);
    if (e2) throw new Error(e2.message);
    return { run, results: results ?? [], retries: retries ?? [] };
  });

export const launchRun = createServerFn({ method: "POST" })
  .middleware([requireSupabaseAuth])
  .inputValidator((d) => launchInputSchema.parse(d))
  .handler(async ({ data, context }): Promise<LaunchResult> => {
    const { createAndLaunchRun } = await import("./runs.server");
    return createAndLaunchRun(context.supabase, context.userId, data);
  });

export const retryRun = createServerFn({ method: "POST" })
  .middleware([requireSupabaseAuth])
  .inputValidator((d) => idSchema.parse(d))
  .handler(async ({ data, context }): Promise<LaunchResult> => {
    const { data: orig, error } = await context.supabase.from("eval_runs").select("*").eq("id", data.id).maybeSingle();
    if (error) throw new Error(error.message);
    if (!orig) return { ok: false, runId: null, code: "NOT_FOUND", message: "Run not found" };
    if (orig.is_demo) return { ok: false, runId: null, code: "INVALID_REQUEST", message: "Demo runs cannot be retried" };
    if (orig.status !== "failed") {
      return { ok: false, runId: null, code: "INVALID_REQUEST", message: "Only failed runs can be retried" };
    }
    if (!orig.agent_config_id) {
      return { ok: false, runId: null, code: "NOT_FOUND", message: "The agent configuration for this run was deleted" };
    }
    const { createAndLaunchRun } = await import("./runs.server");
    return createAndLaunchRun(context.supabase, context.userId, {
      suite_id: orig.suite_id,
      agent_config_id: orig.agent_config_id,
      timeout_seconds: orig.timeout_seconds,
      max_retries: orig.max_retries,
      retry_of_run_id: orig.id,
    });
  });

export const syncRun = createServerFn({ method: "POST" })
  .middleware([requireSupabaseAuth])
  .inputValidator((d) => idSchema.parse(d))
  .handler(async ({ data, context }) => {
    const { syncRunFromEngine } = await import("./runs.server");
    return syncRunFromEngine(context.supabase, data.id);
  });

export const getDashboard = createServerFn({ method: "GET" })
  .middleware([optionalAuth])
  .inputValidator((d) => z.object({ demo: z.boolean().default(false) }).parse(d))
  .handler(async ({ data, context }) => {
    const demo = effectiveDemo(context.userId, data.demo);
    const { data: runs, error } = await context.supabase
      .from("eval_runs")
      .select("id, created_at, status, pass_rate, avg_latency_ms, total, passed, agent_snapshot, test_suites(name)")
      .eq("is_demo", demo)
      .order("created_at", { ascending: false })
      .limit(200);
    if (error) throw new Error(error.message);
    const ids = (runs ?? []).map((r) => r.id);
    const { data: failures, error: e2 } = ids.length
      ? await context.supabase
          .from("eval_results")
          .select("failure_category")
          .eq("is_demo", demo)
          .eq("passed", false)
          .in("run_id", ids)
      : { data: [], error: null };
    if (e2) throw new Error(e2.message);
    return {
      demo,
      signedIn: !!context.userId,
      metrics: computeDashboard(runs ?? [], failures ?? []),
      recentRuns: (runs ?? []).slice(0, 8),
    };
  });

export const getEngineStatus = createServerFn({ method: "GET" }).handler(async () => {
  const { getEngineHealth, EngineError } = await import("./engine.server");
  try {
    const h = await getEngineHealth();
    return { ok: true as const, version: h.version ?? null, code: null, message: null };
  } catch (e) {
    const err = e instanceof EngineError ? e : null;
    return { ok: false as const, version: null, code: err?.code ?? "ENGINE_ERROR", message: err?.message ?? "Unknown error" };
  }
});
