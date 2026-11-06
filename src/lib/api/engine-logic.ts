/** Pure, testable rules for runs, retries, rate limits and demo scoping. */

export const RUN_STATUSES = ["pending", "queued", "running", "completed", "failed", "cancelled"] as const;
export type RunStatus = (typeof RUN_STATUSES)[number];

const TRANSITIONS: Record<RunStatus, RunStatus[]> = {
  pending: ["queued", "failed"],
  // An engine may finish between two polls, so queued -> completed is allowed.
  queued: ["running", "completed", "failed", "cancelled"],
  running: ["completed", "failed", "cancelled"],
  completed: [],
  failed: [],
  cancelled: [],
};

export function canTransition(from: RunStatus, to: RunStatus): boolean {
  return from === to || TRANSITIONS[from].includes(to);
}

export function isTerminal(status: RunStatus): boolean {
  return TRANSITIONS[status].length === 0;
}

export function isActive(status: RunStatus): boolean {
  return status === "pending" || status === "queued" || status === "running";
}

export const MAX_RETRIES_LIMIT = 5;
export const HTTP_TIMEOUT_MS = 15_000;

/** Network errors / timeouts (httpStatus null), 429 and 5xx are retried; other 4xx are not. */
export function isRetryableHttp(httpStatus: number | null): boolean {
  if (httpStatus === null) return true;
  return httpStatus === 429 || httpStatus >= 500;
}

/** Exponential backoff: retry #0 waits 1s, #1 2s, #2 4s ... capped at 16s. */
export function backoffMs(retryIndex: number): number {
  return Math.min(1000 * 2 ** retryIndex, 16_000);
}

export const RATE_LIMIT = { windowSeconds: 60, maxPerWindow: 5, maxActive: 10 } as const;

/** Returns an error message if a new launch must be rejected, otherwise null. */
export function checkLaunchRateLimit(recentLaunches: number, activeRuns: number): string | null {
  if (recentLaunches >= RATE_LIMIT.maxPerWindow) {
    return `Limit is ${RATE_LIMIT.maxPerWindow} launches per ${RATE_LIMIT.windowSeconds}s. Try again shortly.`;
  }
  if (activeRuns >= RATE_LIMIT.maxActive) {
    return `There are already ${RATE_LIMIT.maxActive} runs in progress. Wait for some to finish.`;
  }
  return null;
}

/** Anonymous visitors only ever see demo data; signed-in users choose. */
export function effectiveDemo(userId: string | null | undefined, requestedDemo: boolean): boolean {
  return userId ? requestedDemo : true;
}
