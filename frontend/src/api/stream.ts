import { getToken } from "./token";

// Same same-origin reasoning as client.ts: the backend has no CORS, so this always goes
// through the dev proxy / same-origin reverse proxy, never straight to the backend's port.
const API_BASE_URL = import.meta.env.VITE_API_BASE_URL ?? "/api";

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
    throw new Error(`stream request failed with status ${response.status}`);
  }
  return response;
}
