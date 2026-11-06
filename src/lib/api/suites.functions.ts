import { createServerFn } from "@tanstack/react-start";
import { z } from "zod";
import { requireSupabaseAuth } from "@/integrations/supabase/auth-middleware";
import type { Json } from "@/integrations/supabase/types";
import { optionalAuth } from "./optional-auth";
import { scenarioInputSchema, suiteInputSchema } from "./eval-config";

const idSchema = z.object({ id: z.string().uuid() });

export const listSuites = createServerFn({ method: "GET" })
  .middleware([optionalAuth])
  .handler(async ({ context }) => {
    let q = context.supabase
      .from("test_suites")
      .select("*, test_scenarios(count)")
      .order("is_demo")
      .order("created_at", { ascending: false });
    if (!context.userId) q = q.eq("is_demo", true);
    const { data, error } = await q;
    if (error) throw new Error(error.message);
    return (data ?? []).map(({ test_scenarios, ...s }) => ({
      ...s,
      scenario_count: (test_scenarios as unknown as { count: number }[])[0]?.count ?? 0,
    }));
  });

export const getSuite = createServerFn({ method: "GET" })
  .middleware([optionalAuth])
  .inputValidator((d) => idSchema.parse(d))
  .handler(async ({ data, context }) => {
    let q = context.supabase.from("test_suites").select("*").eq("id", data.id);
    if (!context.userId) q = q.eq("is_demo", true);
    const { data: suite, error } = await q.maybeSingle();
    if (error) throw new Error(error.message);
    if (!suite) throw new Error("Suite not found (or sign in required)");
    const { data: scenarios, error: e2 } = await context.supabase
      .from("test_scenarios")
      .select("*")
      .eq("suite_id", data.id)
      .order("created_at");
    if (e2) throw new Error(e2.message);
    return { suite, scenarios: scenarios ?? [] };
  });

export const createSuite = createServerFn({ method: "POST" })
  .middleware([requireSupabaseAuth])
  .inputValidator((d) => suiteInputSchema.parse(d))
  .handler(async ({ data, context }) => {
    const { data: row, error } = await context.supabase
      .from("test_suites")
      .insert({ ...data, created_by: context.userId, is_demo: false })
      .select("id")
      .single();
    if (error) throw new Error(error.message);
    return row;
  });

export const updateSuite = createServerFn({ method: "POST" })
  .middleware([requireSupabaseAuth])
  .inputValidator((d) => suiteInputSchema.extend({ id: z.string().uuid() }).parse(d))
  .handler(async ({ data, context }) => {
    const { id, ...rest } = data;
    const { data: row, error } = await context.supabase.from("test_suites").update(rest).eq("id", id).select("id").maybeSingle();
    if (error) throw new Error(error.message);
    if (!row) throw new Error("Suite not found or read-only (demo data cannot be edited)");
    return row;
  });

export const deleteSuite = createServerFn({ method: "POST" })
  .middleware([requireSupabaseAuth])
  .inputValidator((d) => idSchema.parse(d))
  .handler(async ({ data, context }) => {
    const { data: rows, error } = await context.supabase.from("test_suites").delete().eq("id", data.id).select("id");
    if (error) throw new Error(error.message);
    if (!rows?.length) throw new Error("Suite not found or read-only (demo data cannot be deleted)");
    return { ok: true };
  });

export const createScenario = createServerFn({ method: "POST" })
  .middleware([requireSupabaseAuth])
  .inputValidator((d) => scenarioInputSchema.parse(d))
  .handler(async ({ data, context }) => {
    const { data: row, error } = await context.supabase
      .from("test_scenarios")
      .insert({
        ...data,
        input_messages: data.input_messages as Json,
        eval_config: data.eval_config as Json,
        is_demo: false,
      })
      .select("id")
      .single();
    if (error) throw new Error(error.message);
    return row;
  });

export const updateScenario = createServerFn({ method: "POST" })
  .middleware([requireSupabaseAuth])
  .inputValidator((d) => {
    const { id, ...rest } = z.object({ id: z.string().uuid() }).passthrough().parse(d) as { id: string };
    return { id, ...scenarioInputSchema.parse(rest) };
  })
  .handler(async ({ data, context }) => {
    const { id, ...rest } = data;
    const { data: row, error } = await context.supabase
      .from("test_scenarios")
      .update({ ...rest, input_messages: rest.input_messages as Json, eval_config: rest.eval_config as Json })
      .eq("id", id)
      .select("id")
      .maybeSingle();
    if (error) throw new Error(error.message);
    if (!row) throw new Error("Scenario not found or read-only (demo data cannot be edited)");
    return row;
  });

export const deleteScenario = createServerFn({ method: "POST" })
  .middleware([requireSupabaseAuth])
  .inputValidator((d) => idSchema.parse(d))
  .handler(async ({ data, context }) => {
    const { data: rows, error } = await context.supabase.from("test_scenarios").delete().eq("id", data.id).select("id");
    if (error) throw new Error(error.message);
    if (!rows?.length) throw new Error("Scenario not found or read-only");
    return { ok: true };
  });
