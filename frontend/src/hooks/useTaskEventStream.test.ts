import { act, renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { onUnauthorized } from "../api/client";
import { useTaskEventStream } from "./useTaskEventStream";

function sseStream(chunks: string[]): ReadableStream<Uint8Array> {
  const encoder = new TextEncoder();
  return new ReadableStream({
    start(controller) {
      for (const chunk of chunks) {
        controller.enqueue(encoder.encode(chunk));
      }
      controller.close();
    },
  });
}

function sseResponse(chunks: string[]): Response {
  return new Response(sseStream(chunks), {
    status: 200,
    headers: { "Content-Type": "text/event-stream" },
  });
}

describe("useTaskEventStream", () => {
  beforeEach(() => {
    // shouldAdvanceTime: Testing Library's own `waitFor` polls with a real setTimeout
    // under the hood; without this, faking time stalls that polling forever alongside
    // whatever delay this test means to control (the reconnect backoff).
    vi.useFakeTimers({ shouldAdvanceTime: true });
    vi.stubGlobal("fetch", vi.fn());
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllGlobals();
  });

  it("stops without reconnecting once a task.finished event arrives", async () => {
    vi.mocked(fetch).mockResolvedValueOnce(
      sseResponse([
        'id: 1\nevent: iteration.started\ndata: {"seq":1,"type":"iteration.started","payload":{},"created_at":"2026-01-01T00:00:00Z"}\n\n',
        'id: 2\nevent: task.finished\ndata: {"seq":2,"type":"task.finished","payload":{"status":"SUCCEEDED"},"created_at":"2026-01-01T00:00:01Z"}\n\n',
      ]),
    );

    const { result } = renderHook(() => useTaskEventStream("task-1"));

    await waitFor(() => expect(result.current.status).toBe("closed"));
    expect(result.current.events.map((e) => e.type)).toEqual([
      "iteration.started",
      "task.finished",
    ]);

    // No reconnect attempt should ever follow a terminal event.
    await vi.advanceTimersByTimeAsync(5000);
    expect(fetch).toHaveBeenCalledTimes(1);
  });

  it("reconnects with Last-Event-ID after the connection drops before a terminal event", async () => {
    vi.mocked(fetch).mockResolvedValueOnce(
      sseResponse([
        'id: 1\nevent: iteration.started\ndata: {"seq":1,"type":"iteration.started","payload":{},"created_at":"2026-01-01T00:00:00Z"}\n\n',
      ]),
    );
    vi.mocked(fetch).mockResolvedValueOnce(
      sseResponse([
        'id: 2\nevent: task.finished\ndata: {"seq":2,"type":"task.finished","payload":{"status":"SUCCEEDED"},"created_at":"2026-01-01T00:00:01Z"}\n\n',
      ]),
    );

    const { result } = renderHook(() => useTaskEventStream("task-1"));

    await waitFor(() => expect(fetch).toHaveBeenCalledTimes(1));
    await vi.advanceTimersByTimeAsync(5000);
    await waitFor(() => expect(fetch).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(result.current.status).toBe("closed"));

    const secondCallHeaders = new Headers(vi.mocked(fetch).mock.calls[1]?.[1]?.headers);
    expect(secondCallHeaders.get("Last-Event-ID")).toBe("1");
    expect(result.current.events.map((e) => e.type)).toEqual([
      "iteration.started",
      "task.finished",
    ]);
  });

  it("stops for good and reports unauthorized on a 401, same handler as apiFetch", async () => {
    const handler = vi.fn();
    onUnauthorized(handler);
    vi.mocked(fetch).mockResolvedValue(new Response(null, { status: 401 }));

    renderHook(() => useTaskEventStream("task-1"));

    await waitFor(() => expect(handler).toHaveBeenCalledOnce());
    // No reconnect attempt should ever follow a 401, however long we wait.
    await vi.advanceTimersByTimeAsync(35_000);
    expect(fetch).toHaveBeenCalledTimes(1);
  });

  it("stops reconnecting on a non-401 4xx and surfaces a failed status until retry() is called", async () => {
    vi.mocked(fetch).mockResolvedValueOnce(new Response(null, { status: 404 }));

    const { result } = renderHook(() => useTaskEventStream("task-1"));

    await waitFor(() => expect(result.current.status).toBe("failed"));
    // No automatic reconnect: a 404 is not going to fix itself by waiting.
    await vi.advanceTimersByTimeAsync(35_000);
    expect(fetch).toHaveBeenCalledTimes(1);

    vi.mocked(fetch).mockResolvedValueOnce(
      sseResponse([
        'id: 1\nevent: task.finished\ndata: {"seq":1,"type":"task.finished","payload":{"status":"SUCCEEDED"},"created_at":"2026-01-01T00:00:00Z"}\n\n',
      ]),
    );
    act(() => result.current.retry());

    await waitFor(() => expect(fetch).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(result.current.status).toBe("closed"));
  });

  it("reconnects after a 5xx with capped exponential backoff, still resuming with Last-Event-ID", async () => {
    vi.mocked(fetch).mockResolvedValueOnce(
      sseResponse([
        'id: 1\nevent: iteration.started\ndata: {"seq":1,"type":"iteration.started","payload":{},"created_at":"2026-01-01T00:00:00Z"}\n\n',
      ]),
    ); // opens, then drops (body ends) before a terminal event: the first retry after this
    // uses the base delay, same as the plain-drop case above.
    vi.mocked(fetch).mockResolvedValueOnce(new Response(null, { status: 500 }));
    vi.mocked(fetch).mockResolvedValueOnce(new Response(null, { status: 500 }));
    vi.mocked(fetch).mockResolvedValueOnce(
      sseResponse([
        'id: 2\nevent: task.finished\ndata: {"seq":2,"type":"task.finished","payload":{"status":"SUCCEEDED"},"created_at":"2026-01-01T00:00:01Z"}\n\n',
      ]),
    );

    renderHook(() => useTaskEventStream("task-1"));

    await waitFor(() => expect(fetch).toHaveBeenCalledTimes(1));
    await vi.advanceTimersByTimeAsync(1000); // base delay after the drop
    await waitFor(() => expect(fetch).toHaveBeenCalledTimes(2)); // first 500

    await vi.advanceTimersByTimeAsync(1999);
    expect(fetch).toHaveBeenCalledTimes(2); // not yet: needs ~2000ms after a 500
    await vi.advanceTimersByTimeAsync(1);
    await waitFor(() => expect(fetch).toHaveBeenCalledTimes(3)); // second 500, delay doubled

    await vi.advanceTimersByTimeAsync(4000); // doubled again after the second 500
    await waitFor(() => expect(fetch).toHaveBeenCalledTimes(4));

    const lastCallHeaders = new Headers(vi.mocked(fetch).mock.calls[3]?.[1]?.headers);
    expect(lastCallHeaders.get("Last-Event-ID")).toBe("1");
  });
});
