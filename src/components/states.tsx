import type { ReactNode } from "react";
import { Link } from "@tanstack/react-router";
import { AlertTriangle, Inbox, Lock } from "lucide-react";
import { Skeleton } from "@/components/ui/skeleton";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";
import { errorTitle } from "@/lib/api/types";

export function LoadingBlock({ rows = 4 }: { rows?: number }) {
  return (
    <div className="space-y-2" aria-busy="true" aria-label="Loading">
      {Array.from({ length: rows }).map((_, i) => (
        <Skeleton key={i} className="h-10 w-full" />
      ))}
    </div>
  );
}

export function ErrorState({ error, onRetry, code }: { error: unknown; onRetry?: () => void; code?: string | null }) {
  const message = error instanceof Error ? error.message : String(error);
  return (
    <div role="alert" className="flex items-start gap-3 rounded-md border border-destructive/40 bg-destructive/10 p-4">
      <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0 text-destructive" />
      <div className="flex-1 text-sm">
        <p className="font-medium text-foreground">{code ? errorTitle(code) : "Request failed"}</p>
        <p className="mt-1 break-words font-mono text-xs text-muted-foreground">
          {code ? `${code} · ` : ""}
          {message}
        </p>
      </div>
      {onRetry && (
        <Button size="sm" variant="outline" onClick={onRetry}>
          Retry
        </Button>
      )}
    </div>
  );
}

export function EmptyState({ title, body, action }: { title: string; body?: string; action?: ReactNode }) {
  return (
    <div className="flex flex-col items-center justify-center rounded-md border border-dashed border-border px-6 py-12 text-center">
      <Inbox className="h-6 w-6 text-muted-foreground" />
      <p className="mt-3 text-sm font-medium">{title}</p>
      {body && <p className="mt-1 max-w-md text-sm text-muted-foreground">{body}</p>}
      {action && <div className="mt-4">{action}</div>}
    </div>
  );
}

export function DemoBadge({ className }: { className?: string }) {
  return (
    <span
      className={cn(
        "inline-flex items-center rounded border border-warning/40 bg-warning/10 px-1.5 py-0.5 font-mono text-[10px] uppercase tracking-wide text-warning",
        className,
      )}
    >
      Demo data
    </span>
  );
}

const STATUS_STYLES: Record<string, string> = {
  completed: "border-success/40 bg-success/10 text-success",
  failed: "border-destructive/40 bg-destructive/10 text-destructive",
  running: "border-info/40 bg-info/10 text-info",
  queued: "border-info/40 bg-info/10 text-info",
  pending: "border-border bg-muted text-muted-foreground",
  cancelled: "border-border bg-muted text-muted-foreground",
  pass: "border-success/40 bg-success/10 text-success",
  fail: "border-destructive/40 bg-destructive/10 text-destructive",
};

export function StatusPill({ status }: { status: string }) {
  return (
    <span
      className={cn(
        "inline-flex items-center gap-1.5 rounded border px-1.5 py-0.5 font-mono text-[11px] uppercase",
        STATUS_STYLES[status] ?? STATUS_STYLES["pending"],
      )}
    >
      {(status === "running" || status === "queued") && (
        <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-current" />
      )}
      {status}
    </span>
  );
}

export function SignInPrompt({ text = "Sign in to see and edit your workspace." }: { text?: string }) {
  return (
    <div className="flex items-center gap-3 rounded-md border border-border bg-card px-4 py-3 text-sm">
      <Lock className="h-4 w-4 text-muted-foreground" />
      <span className="flex-1 text-muted-foreground">{text} You're viewing demo data.</span>
      <Button asChild size="sm">
        <Link to="/auth">Sign in</Link>
      </Button>
    </div>
  );
}

export function PageHeader({ title, description, actions }: { title: string; description?: string | undefined; actions?: ReactNode }) {
  return (
    <div className="mb-6 flex flex-wrap items-end justify-between gap-4 border-b border-border pb-4">
      <div>
        <h1 className="text-xl font-semibold tracking-tight">{title}</h1>
        {description && <p className="mt-1 text-sm text-muted-foreground">{description}</p>}
      </div>
      {actions && <div className="flex items-center gap-2">{actions}</div>}
    </div>
  );
}

export function fmtPct(v: number | null | undefined) {
  return v == null ? "—" : `${(Number(v) * 100).toFixed(1)}%`;
}
export function fmtMs(v: number | null | undefined) {
  return v == null ? "—" : `${Math.round(Number(v))} ms`;
}
export function fmtDate(v: string | null | undefined) {
  return v ? new Date(v).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" }) : "—";
}
