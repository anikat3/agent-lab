import { describe, expect, it } from "vitest";
import { backoffMs, canTransition, checkLaunchRateLimit, effectiveDemo, isRetryableHttp } from "@/lib/api/engine-logic";
import { validateEvalConfig } from "@/lib/api/eval-config";
import { computeDashboard } from "@/lib/api/metrics";

describe("retry policy", () => {
  it("retries network errors, 429 and 5xx but not other 4xx", () => {
    expect(isRetryableHttp(null)).toBe(true);
    expect(isRetryableHttp(429)).toBe(true);
    expect(isRetryableHttp(503)).toBe(true);
    expect(isRetryableHttp(400)).toBe(false);
    expect(isRetryableHttp(401)).toBe(false);
  });
  it("backs off 1s, 2s, 4s", () => {
    expect([0, 1, 2].map(backoffMs)).toEqual([1000, 2000, 4000]);
  });
});

describe("run status transitions", () => {
  it("allows forward transitions and blocks leaving terminal states", () => {
    expect(canTransition("pending", "queued")).toBe(true);
    expect(canTransition("queued", "running")).toBe(true);
    expect(canTransition("running", "completed")).toBe(true);
    expect(canTransition("failed", "pending")).toBe(false);
    expect(canTransition("completed", "running")).toBe(false);
    expect(canTransition("running", "queued")).toBe(false);
  });
});

describe("launch rate limit", () => {
  it("rejects the 6th launch within the window", () => {
    expect(checkLaunchRateLimit(4, 0)).toBeNull();
    expect(checkLaunchRateLimit(5, 0)).not.toBeNull();
  });
  it("rejects when 10 runs are already active", () => {
    expect(checkLaunchRateLimit(0, 9)).toBeNull();
    expect(checkLaunchRateLimit(0, 10)).not.toBeNull();
  });
});

describe("demo scoping", () => {
  it("excludes demo data for signed-in users by default and forces demo for anonymous", () => {
    expect(effectiveDemo("user-1", false)).toBe(false);
    expect(effectiveDemo("user-1", true)).toBe(true);
    expect(effectiveDemo(null, false)).toBe(true);
  });
});

describe("eval config validation", () => {
  it("keyword requires at least one keyword", () => {
    expect(validateEvalConfig("keyword", { required: [] }).ok).toBe(false);
    expect(validateEvalConfig("keyword", { required: ["refund"] }).ok).toBe(true);
  });
  it("exact requires expected text", () => {
    expect(validateEvalConfig("exact", { expected: "" }).ok).toBe(false);
  });
  it("json_schema rejects invalid types", () => {
    expect(validateEvalConfig("json_schema", { schema: { type: "obj" } }).ok).toBe(false);
    expect(validateEvalConfig("json_schema", { schema: { type: "object", required: ["a"] } }).ok).toBe(true);
  });
  it("llm_judge threshold must be between 0 and 1", () => {
    expect(validateEvalConfig("llm_judge", { rubric: "Be polite and helpful", pass_threshold: 1.5 }).ok).toBe(false);
    expect(validateEvalConfig("llm_judge", { rubric: "Be polite and helpful", pass_threshold: 0.7 }).ok).toBe(true);
  });
  it("rejects unknown evaluation types and extra keys", () => {
    expect(validateEvalConfig("regex", {}).ok).toBe(false);
    expect(validateEvalConfig("exact", { expected: "x", foo: 1 }).ok).toBe(false);
  });
});

describe("dashboard metrics", () => {
  it("averages pass rate over completed runs only", () => {
    const m = computeDashboard(
      [
        { id: "a", created_at: "2026-01-01", status: "completed", pass_rate: 0.5, avg_latency_ms: 100 },
        { id: "b", created_at: "2026-01-02", status: "completed", pass_rate: 1, avg_latency_ms: 300 },
        { id: "c", created_at: "2026-01-03", status: "failed", pass_rate: null, avg_latency_ms: null },
      ],
      [{ failure_category: "tone" }, { failure_category: "tone" }, { failure_category: null }],
    );
    expect(m.avgPassRate).toBe(0.75);
    expect(m.avgLatencyMs).toBe(200);
    expect(m.failedRuns).toBe(1);
    expect(m.categories[0]).toEqual({ category: "tone", count: 2 });
  });
});
