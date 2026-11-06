import { createServerFn } from "@tanstack/react-start";
import { z } from "zod";
import { requireSupabaseAuth } from "@/integrations/supabase/auth-middleware";
import { optionalAuth } from "./optional-auth";
import { agentInputSchema } from "./eval-config";

export const listAgents = createServerFn({ method: "GET" })
  .middleware([optionalAuth])
  .handler(async ({ context }) => {
    let q = context.supabase.from("agent_configs").select("*").order("is_demo").order("created_at", { ascending: false });
    if (!context.userId) q = q.eq("is_demo", true);
    const { data, error } = await q;
    if (error) throw new Error(error.message);
    return data ?? [];
  });

export const createAgent = createServerFn({ method: "POST" })
  .middleware([requireSupabaseAuth])
  .inputValidator((d) => agentInputSchema.parse(d))
  .handler(async ({ data, context }) => {
    const { data: row, error } = await context.supabase
      .from("agent_configs")
      .insert({ ...data, created_by: context.userId, is_demo: false })
      .select("id")
      .single();
    if (error) throw new Error(error.message);
    return row;
  });

export const updateAgent = createServerFn({ method: "POST" })
  .middleware([requireSupabaseAuth])
  .inputValidator((d) => agentInputSchema.extend({ id: z.string().uuid() }).parse(d))
  .handler(async ({ data, context }) => {
    const { id, ...rest } = data;
    const { data: row, error } = await context.supabase.from("agent_configs").update(rest).eq("id", id).select("id").maybeSingle();
    if (error) throw new Error(error.message);
    if (!row) throw new Error("Agent not found or read-only (demo data cannot be edited)");
    return row;
  });

export const deleteAgent = createServerFn({ method: "POST" })
  .middleware([requireSupabaseAuth])
  .inputValidator((d) => z.object({ id: z.string().uuid() }).parse(d))
  .handler(async ({ data, context }) => {
    const { data: rows, error } = await context.supabase.from("agent_configs").delete().eq("id", data.id).select("id");
    if (error) throw new Error(error.message);
    if (!rows?.length) throw new Error("Agent not found or read-only");
    return { ok: true };
  });
