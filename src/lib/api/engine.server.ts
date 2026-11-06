import { backoffMs, HTTP_TIMEOUT_MS, isRetryableHttp } from "./engine-logic";
import {
  engineErrorBodySchema,
  healthResponseSchema,
  launchResponseSchema,
  runStatusResponseSchema,
  type AppErrorCode,
  type LaunchRequest,
  type RunStatusResponse,
} from "./types";

export class EngineError extends Error {
  attempts = 1;
  constructor(
    public code: AppErrorCode,
    message: string,
    public httpStatus: number | null = null,
    public retryable = false,
  ) {
    super(message);
  }
}

function engineConfig() {
  const url = process.env["EVAL_ENGINE_URL"];
  const key = process.env["EVAL_ENGINE_API_KEY"];
  if (!url) {
    throw new EngineError(
      "ENGINE_NOT_CONFIGURED",
      "EVAL_ENGINE_URL is not set. Add your engine URL and API key to the project secrets.",
    );
  }
  return { url: url.replace(/\/+$/, ""), key };
}

async function engineRequest(
  path: string,
  init: { method: "GET" | "POST"; body?: unknown; headers?: Record<string, string> },
  timeoutMs = HTTP_TIMEOUT_MS,
): Promise<unknown> {
  const { url, key } = engineConfig();
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), timeoutMs);
  let res: Response;
  try {
    res = await fetch(url + path, {
      method: init.method,
      headers: {
        "content-type": "application/json",
        accept: "application/json",
        ...(key ? { authorization: `Bearer ${key}` } : {}),
        ...init.headers,
      },
      body: init.body === undefined ? undefined : JSON.stringify(init.body),
      signal: ctrl.signal,
    });
  } catch (e) {
    if (ctrl.signal.aborted) {
      throw new EngineError("ENGINE_TIMEOUT", `No response within ${timeoutMs / 1000}s`, null, true);
    }
    console.error("[engine] network error", e);
    throw new EngineError("ENGINE_UNREACHABLE", "Could not reach the evaluation engine", null, true);
  } finally {
    clearTimeout(timer);
  }

  const text = await res.text();
  let json: unknown = null;
  try {
    json = text ? JSON.parse(text) : null;
  } catch {
    json = null;
  }

  if (!res.ok) {
    const body = engineErrorBodySchema.safeParse(json);
    const detail = body.success ? `${body.data.error.code}: ${body.data.error.message}` : `HTTP ${res.status}`;
    const code: AppErrorCode =
      res.status === 401 || res.status === 403
        ? "ENGINE_AUTH_FAILED"
        : res.status === 429 || res.status >= 500
          ? "ENGINE_ERROR"
          : "ENGINE_BAD_REQUEST";
    throw new EngineError(code, detail, res.status, isRetryableHttp(res.status));
  }
  return json;
}

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

export async function launchEngineRun(payload: LaunchRequest, maxRetries: number) {
  let attempt = 0;
  for (;;) {
    attempt++;
    try {
      const raw = await engineRequest("/runs", {
        method: "POST",
        body: payload,
        headers: { "Idempotency-Key": payload.run_id },
      });
      const parsed = launchResponseSchema.safeParse(raw);
      if (!parsed.success) {
        throw new EngineError("ENGINE_ERROR", "Engine returned an unexpected launch response");
      }
      return { data: parsed.data, attempts: attempt };
    } catch (e) {
      const err = e instanceof EngineError ? e : new EngineError("ENGINE_ERROR", String(e));
      err.attempts = attempt;
      if (!err.retryable || attempt > maxRetries) throw err;
      await sleep(backoffMs(attempt - 1));
    }
  }
}

export async function getEngineRun(externalRunId: string): Promise<RunStatusResponse> {
  const raw = await engineRequest(`/runs/${encodeURIComponent(externalRunId)}`, { method: "GET" });
  const parsed = runStatusResponseSchema.safeParse(raw);
  if (!parsed.success) {
    throw new EngineError("ENGINE_ERROR", "Engine returned an unexpected run status response");
  }
  return parsed.data;
}

export async function getEngineHealth() {
  const raw = await engineRequest("/health", { method: "GET" }, 5000);
  const parsed = healthResponseSchema.safeParse(raw);
  if (!parsed.success) throw new EngineError("ENGINE_ERROR", "Unexpected health response");
  return parsed.data;
}
