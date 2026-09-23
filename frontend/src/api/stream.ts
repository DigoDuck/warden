import { getToken } from "./token";

// Same same-origin reasoning as client.ts: the backend has no CORS, so this always goes
// through the dev proxy / same-origin reverse proxy, never straight to the backend's port.
const API_BASE_URL = import.meta.env.VITE_API_BASE_URL ?? "/api";

/** A non-OK response from the stream endpoint, carrying the status so the caller
 * (useTaskEventStream) can tell a 401 or another 4xx apart from a 5xx/network failure
 * instead of treating every failure the same way. */
export class StreamError extends Error {
  readonly status: number;

  constructor(status: number) {
    super(`stream request failed with status ${status}`);
    this.name = "StreamError";
    this.status = status;
  }
}

export interface OpenTaskEventStreamOptions {
  /** Resume after this event id (`Last-Event-ID`), omitted on the very first connection. */
  afterId?: number;
  signal?: AbortSignal;
}

/** Opens the raw SSE response for a task's event stream with `fetch`, not `EventSource`:
 * `EventSource` cannot set a custom `Authorization` header, and the bearer token must
 * never go in the URL (it would land in access logs). See docs/adr/ADR-024 and the
 * backend's `GET /tasks/{id}/stream`. The caller reads `response.body` itself with a
 * `ReadableStream` reader and `SseParser` (src/lib/sse.ts). */
export async function openTaskEventStream(
  taskId: string,
  options: OpenTaskEventStreamOptions = {},
): Promise<Response> {
  const token = getToken();
  const headers: Record<string, string> = { Accept: "text/event-stream" };
  if (token) {
    headers.Authorization = `Bearer ${token}`;
  }
  if (options.afterId !== undefined) {
    headers["Last-Event-ID"] = String(options.afterId);
  }

  const response = await fetch(`${API_BASE_URL}/tasks/${encodeURIComponent(taskId)}/stream`, {
    headers,
    signal: options.signal,
  });
  if (!response.ok || !response.body) {
    throw new StreamError(response.status);
  }
  return response;
}
