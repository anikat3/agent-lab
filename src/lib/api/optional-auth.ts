import { createMiddleware } from "@tanstack/react-start";
import { getRequest } from "@tanstack/react-start/server";
import { createClient } from "@supabase/supabase-js";
import type { Database } from "@/integrations/supabase/types";

function makeClient(url: string, key: string, token: string | null) {
  return createClient<Database>(url, key, {
    auth: { storage: undefined, persistSession: false, autoRefreshToken: false },
    global: {
      headers: token ? { Authorization: `Bearer ${token}` } : {},
      fetch: (input, init) => {
        const h = new Headers(init?.headers);
        if (key.startsWith("sb_") && h.get("Authorization") === `Bearer ${key}`) h.delete("Authorization");
        h.set("apikey", key);
        return fetch(input, { ...init, headers: h });
      },
    },
  });
}

/**
 * Read middleware: signed-in callers get an RLS-scoped user client;
 * anonymous callers get a publishable client (RLS limits them to demo rows,
 * and handlers additionally filter is_demo = true).
 */
export const optionalAuth = createMiddleware({ type: "function" }).server(async ({ next }) => {
  const url = process.env["SUPABASE_URL"]!;
  const key = process.env["SUPABASE_PUBLISHABLE_KEY"]!;
  const header = getRequest()?.headers.get("authorization") ?? "";
  const token = header.startsWith("Bearer ") ? header.slice(7) : null;

  if (token && token.split(".").length === 3) {
    const client = makeClient(url, key, token);
    const { data } = await client.auth.getClaims(token);
    const sub = data?.claims?.sub;
    if (sub) return next({ context: { supabase: client, userId: sub as string | null } });
  }
  return next({ context: { supabase: makeClient(url, key, null), userId: null as string | null } });
});
