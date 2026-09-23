import { renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
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
});
