import { useEffect, useState } from "react";
import { openTaskEventStream } from "../api/stream";
import { SseParser } from "../lib/sse";

export interface StreamedEvent {
  seq: number;
  type: string;
  payload: unknown;
  created_at: string;
}

export type StreamStatus = "connecting" | "open" | "closed" | "error";

const TASK_FINISHED = "task.finished";
// ponytail: fixed delay, no exponential backoff. A control-plane operator watching one
// task's timeline is the only caller; add backoff if this ever needs to survive a
// long-down backend without hammering it every second.
const RECONNECT_DELAY_MS = 1000;

type ConnectOutcome = "terminal" | "dropped";

/** Live timeline for one task's events (docs/adr/ADR-024): connects with `fetch` +
 * `ReadableStream` (never `EventSource`, which cannot carry a bearer token), reconnects
 * with `Last-Event-ID` whenever the connection drops before a terminal event, and stops
 * for good once it sees `task.finished` (the marker every code path to a terminal task
 * status writes — see `backend/warden/api/routes_tasks.py::stream_task_events`). */
export function useTaskEventStream(taskId: string | undefined) {
  const [events, setEvents] = useState<StreamedEvent[]>([]);
  const [status, setStatus] = useState<StreamStatus>("connecting");

  useEffect(() => {
    if (!taskId) return;
    setEvents([]);
    setStatus("connecting");

    const controller = new AbortController();
    let lastSeq: number | undefined;
    let cancelled = false;

    async function connectOnce(): Promise<ConnectOutcome> {
      const response = await openTaskEventStream(taskId, {
        afterId: lastSeq,
        signal: controller.signal,
      });
      setStatus("open");

      const parser = new SseParser();
      const reader = response.body!.getReader();
      const decoder = new TextDecoder();

      for (;;) {
        const { value, done } = await reader.read();
        if (done) {
          return "dropped";
        }
        for (const message of parser.feed(decoder.decode(value, { stream: true }))) {
          if (!message.data) continue;
          const parsed = JSON.parse(message.data) as StreamedEvent;
          lastSeq = parsed.seq;
          setEvents((prev) => [...prev, parsed]);
          if (message.event === TASK_FINISHED) {
            return "terminal";
          }
        }
      }
    }

    async function run() {
      while (!cancelled) {
        try {
          const outcome = await connectOnce();
          if (outcome === "terminal") {
            setStatus("closed");
            return;
          }
          // "dropped": the server ended the stream (or the network did) before a terminal
          // event. Reconnect with Last-Event-ID = lastSeq once the delay above passes.
        } catch (error) {
          if (controller.signal.aborted) {
            return; // unmount or taskId change tore this down; not a real error
          }
          setStatus("error");
        }
        if (cancelled) return;
        await new Promise((resolve) => setTimeout(resolve, RECONNECT_DELAY_MS));
      }
    }

    void run();

    return () => {
      cancelled = true;
      controller.abort();
    };
  }, [taskId]);

  return { events, status };
}
