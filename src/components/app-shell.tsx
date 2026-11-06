import type { ReactNode } from "react";
import { Link, useNavigate } from "@tanstack/react-router";
import { useQueryClient } from "@tanstack/react-query";
import { useServerFn } from "@tanstack/react-start";
import { Bot, FlaskConical, LayoutDashboard, ListChecks, LogOut, Play } from "lucide-react";
import { supabase } from "@/integrations/supabase/client";
import { useAppQuery, useAuth } from "@/hooks/use-auth";
import { getEngineStatus } from "@/lib/api/runs.functions";
import { errorTitle } from "@/lib/api/types";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";

const NAV = [
  { to: "/", label: "Dashboard", icon: LayoutDashboard },
  { to: "/suites", label: "Test suites", icon: ListChecks },
  { to: "/agents", label: "Agents", icon: Bot },
  { to: "/runs/new", label: "Run evaluation", icon: Play },
  { to: "/runs", label: "Results", icon: FlaskConical },
] as const;

function EngineIndicator() {
  const fetchStatus = useServerFn(getEngineStatus);
  const q = useAppQuery(["engine-status"], () => fetchStatus(), { refetchInterval: 30_000 });
  const state = q.isLoading ? "checking" : q.data?.ok ? "online" : "offline";
  const label = q.isLoading
    ? "Checking engine…"
    : q.data?.ok
      ? `Engine online${q.data.version ? ` · v${q.data.version}` : ""}`
      : errorTitle(q.data?.code ?? (q.isError ? "ENGINE_ERROR" : null));
  return (
    <div className="rounded-md border border-sidebar-border px-3 py-2" title={q.data?.message ?? undefined}>
      <div className="flex items-center gap-2 text-xs">
        <span
          className={cn(
            "h-2 w-2 rounded-full",
            state === "online" && "bg-success",
            state === "offline" && "bg-destructive",
            state === "checking" && "animate-pulse bg-muted-foreground",
          )}
        />
        <span className="truncate text-sidebar-foreground">{label}</span>
      </div>
    </div>
  );
}

export function AppShell({ children }: { children: ReactNode }) {
  const { email, userId } = useAuth();
  const queryClient = useQueryClient();
  const navigate = useNavigate();

  async function signOut() {
    await queryClient.cancelQueries();
    queryClient.clear();
    await supabase.auth.signOut();
    navigate({ to: "/auth", replace: true });
  }

  return (
    <div className="flex min-h-screen bg-background">
      <aside className="sticky top-0 flex h-screen w-56 shrink-0 flex-col border-r border-sidebar-border bg-sidebar p-3">
        <Link to="/" className="mb-6 flex items-center gap-2 px-2 pt-1">
          <span className="grid h-6 w-6 place-items-center rounded bg-primary font-mono text-xs font-bold text-primary-foreground">
            A
          </span>
          <span className="font-semibold tracking-tight">AgentLab</span>
        </Link>
        <nav className="flex flex-col gap-0.5">
          {NAV.map(({ to, label, icon: Icon }) => (
            <Link
              key={to}
              to={to}
              activeOptions={{ exact: to === "/" || to === "/runs" }}
              className="flex items-center gap-2 rounded px-2 py-1.5 text-sm text-sidebar-foreground transition-colors hover:bg-sidebar-accent"
              activeProps={{ className: "bg-sidebar-accent text-sidebar-accent-foreground" }}
            >
              <Icon className="h-4 w-4" />
              {label}
            </Link>
          ))}
        </nav>
        <div className="mt-auto space-y-2">
          <EngineIndicator />
          {userId ? (
            <div className="flex items-center gap-2 rounded-md px-2 py-1">
              <span className="flex-1 truncate text-xs text-muted-foreground">{email}</span>
              <Button size="icon" variant="ghost" className="h-7 w-7" onClick={signOut} aria-label="Sign out">
                <LogOut className="h-3.5 w-3.5" />
              </Button>
            </div>
          ) : (
            <Button asChild size="sm" className="w-full">
              <Link to="/auth">Sign in</Link>
            </Button>
          )}
        </div>
      </aside>
      <main className="min-w-0 flex-1 px-8 py-6">{children}</main>
    </div>
  );
}
