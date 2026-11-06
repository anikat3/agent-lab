import type { SupabaseClient } from "@supabase/supabase-js";
import type { Database, Json } from "@/integrations/supabase/types";
import { validateEvalConfig, type EvalType, type LaunchInput } from "./eval-config";
import { canTransition, checkLaunchRateLimit, isActive, isTerminal, RATE_LIMIT, type RunStatus } from "./engine-logic";
import { EngineError, getEngineRun, launchEngineRun } from "./engine.server";
import type { LaunchRequest, LaunchResult } from "./types";

type DB = SupabaseClient<Database>;

export async function createAndLaunchRun(
  sb: DB,
  userId: string,
  input: LaunchInput & { retry_of_run_id?: string | null },
): Promise<LaunchResult> {
  const since = new Date(Date.now() - RATE_LIMIT.windowSeconds * 1000).toISOString();
  const [recent, active] = await Promise.all([
    sb.from("eval_runs").select("id", { count: "exact", head: true }).eq("is_demo", false).gte("created_at", since),
    sb
      .from("eval_runs")
      .select("id", { count: "exact", head: true })
      .eq("is_demo", false)
      .in("status", ["pending", "queued", "running"]),
  ]);
  const limited = checkLaunchRateLimit(recent.count ?? 0, active.count ?? 0);
  if (limited) return { ok: false, runId: null, code: "RATE_LIMITED", message: limited };

  const [{ data: suite }, { data: agent }, { data: scenarios, error: scErr }] = await Promise.all([
    sb.from("test_suites").select("id, name").eq("id", input.suite_id).maybeSingle(),
    sb.from("agent_configs").select("id, name, system_prompt, model").eq("id", input.agent_config_id).maybeSingle(),
    sb.from("test_scenarios").select("*").eq("suite_id", input.suite_id).order("created_at"),
  ]);
  if (!suite) return { ok: false, runId: null, code: "NOT_FOUND", message: "Test suite not found" };
  if (!agent) return { ok: false, runId: null, code: "NOT_FOUND", message: "Agent configuration not found" };
  if (scErr) throw new Error(scErr.message);
  if (!scenarios?.length) {
    return { ok: false, runId: null, code: "INVALID_REQUEST", message: "This suite has no scenarios yet" };
  }

  const payloadScenarios: LaunchRequest["scenarios"] = [];
  for (const s of scenarios) {
    const v = validateEvalConfig(s.eval_type, s.eval_config);
    if (!v.ok) {
      return { ok: false, runId: null, code: "INVALID_EVAL_CONFIG", message: `"${s.name}": ${v.errors.join("; ")}` };
    }
    payloadScenarios.push({
      id: s.id,
      name: s.name,
      input_messages: s.input_messages as LaunchRequest["scenarios"][number]["input_messages"],
      expected_response: s.expected_response,
      eval_type: s.eval_type as EvalType,
      eval_config: v.config,
    });
  }

  const snapshot = { name: agent.name, system_prompt: agent.system_prompt, model: agent.model };
  const { data: run, error: insErr } = await sb
    .from("eval_runs")
    .insert({
      suite_id: suite.id,
      agent_config_id: agent.id,
      agent_snapshot: snapshot as Json,
      retry_of_run_id: input.retry_of_run_id ?? null,
      status: "pending",
      timeout_seconds: input.timeout_seconds,
      max_retries: input.max_retries,
      total: scenarios.length,
      created_by: userId,
      is_demo: false,
    })
    .select("id")
    .single();
  if (insErr || !run) throw new Error(insErr?.message ?? "Could not create run");

  const payload: LaunchRequest = {
    run_id: run.id,
    retry_of_run_id: input.retry_of_run_id ?? null,
    timeout_seconds: input.timeout_seconds,
    agent: { id: agent.id, ...snapshot },
    suite: { id: suite.id, name: suite.name },
    scenarios: payloadScenarios,
  };

  try {
    const { data, attempts } = await launchEngineRun(payload, input.max_retries);
    await sb
      .from("eval_runs")
      .update({
        status: "queued",
        external_run_id: data.external_run_id,
        attempt_count: attempts,
        started_at: new Date().toISOString(),
      })
      .eq("id", run.id);
    return { ok: true, runId: run.id, status: "queued" };
  } catch (e) {
    const err = e instanceof EngineError ? e : new EngineError("ENGINE_ERROR", "Unexpected launch failure");
    if (!(e instanceof EngineError)) console.error("[launch]", e);
    await sb
      .from("eval_runs")
      .update({
        status: "failed",
        error_code: err.code,
        error_message: err.message,
        attempt_count: err.attempts,
        completed_at: new Date().toISOString(),
      })
      .eq("id", run.id);
    return { ok: false, runId: run.id, code: err.code, message: err.message };
  }
}

/** Polls the engine for an active run, upserts results idempotently and advances status. */
export async function syncRunFromEngine(sb: DB, runId: string) {
  const { data: run, error } = await sb.from("eval_runs").select("*").eq("id", runId).maybeSingle();
  if (error) throw new Error(error.message);
  if (!run) throw new Error("Run not found");
  const status = run.status as RunStatus;
  if (run.is_demo || !isActive(status) || !run.external_run_id) return { synced: false, error: null };

  let remote;
  try {
    remote = await getEngineRun(run.external_run_id);
  } catch (e) {
    const err = e instanceof EngineError ? e : new EngineError("ENGINE_ERROR", "Unexpected sync failure");
    return { synced: false, error: { code: err.code, message: err.message } };
  }

  const { data: scenarioRows } = await sb.from("test_scenarios").select("id").eq("suite_id", run.suite_id);
  const known = new Set((scenarioRows ?? []).map((r) => r.id));
  const rows = remote.results
    .filter((r) => known.has(r.scenario_id))
    .map((r) => ({
      run_id: run.id,
      scenario_id: r.scenario_id,
      passed: r.passed,
      quality_score: r.quality_score ?? null,
      latency_ms: r.latency_ms ?? null,
      actual_response: r.actual_response ?? null,
      failure_category: r.passed ? null : (r.failure_category ?? null),
      failure_explanation: r.passed ? null : (r.failure_explanation ?? null),
      evaluator_details: (r.evaluator_details ?? null) as Json,
      is_demo: false,
    }));
  if (rows.length) {
    const { error: upErr } = await sb.from("eval_results").upsert(rows, { onConflict: "run_id,scenario_id" });
    if (upErr) throw new Error(upErr.message);
  }

  const { data: saved } = await sb.from("eval_results").select("passed, latency_ms").eq("run_id", run.id);
  const all = saved ?? [];
  const passed = all.filter((r) => r.passed).length;
  const lat = all.map((r) => r.latency_ms).filter((x): x is number => x != null);
  const next = canTransition(status, remote.status) ? remote.status : status;

  await sb
    .from("eval_runs")
    .update({
      status: next,
      total: Math.max(remote.progress.total, run.total),
      passed,
      pass_rate: all.length ? passed / all.length : null,
      avg_latency_ms: lat.length ? lat.reduce((a, b) => a + b, 0) / lat.length : null,
      started_at: run.started_at ?? remote.started_at ?? null,
      completed_at: isTerminal(next) ? (remote.completed_at ?? new Date().toISOString()) : null,
      error_code: remote.error?.code ?? null,
      error_message: remote.error?.message ?? null,
    })
    .eq("id", run.id);

  return { synced: true, error: null };
}
