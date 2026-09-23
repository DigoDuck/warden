import { useCallback, useEffect, useRef, useState } from "react";
import { notifyUnauthorized } from "../api/client";
import { openTaskEventStream, StreamError } from "../api/stream";
import { SseParser } from "../lib/sse";

export interface StreamedEvent {
  seq: number;
  type: string;
  payload: unknown;
  created_at: string;
}

// "failed": a non-401 4xx (e.g. the task's own 404 rules changed underneath us). Nothing
// about waiting fixes that, so retrying stops until `retry()` is called by hand.
export type StreamStatus = "connecting" | "open" | "closed" | "error" | "failed";

const TASK_FINISHED = "task.finished";
// Capped exponential backoff for a 5xx or a network error: 1s, 2s, 4s, ... up to 30s. A
// dropped connection that isn't the server's or the client's fault (a laptop closed lid,
// a Wi-Fi hiccup) still recovers quickly; a genuinely struggling backend stops getting
// hammered once a second.
const BASE_RECONNECT_DELAY_MS = 1000;
const MAX_RECONNECT_DELAY_MS = 30_000;

type ConnectOutcome = "terminal" | "dropped";

/** Live timeline for one task's events (docs/adr/ADR-024): connects with `fetch` +
 * `ReadableStream` (never `EventSource`, which cannot carry a bearer token), reconnects
 * with `Last-Event-ID` whenever the connection drops before a terminal event, and stops
 * for good once it sees `task.finished` (the marker every code path to a terminal task
 * status writes — see `backend/warden/api/routes_tasks.py::stream_task_events`).
 *
 * A non-OK response is not all one thing: a 401 means the token apiFetch already knows
 * about is dead, so this defers to the same handler apiFetch uses instead of inventing a
 * second opinion; another 4xx (the task really is gone, or no longer visible to us) will
 * not fix itself by waiting, so it stops and waits for `retry()`; a 5xx or a network error
 * is the one case actually worth reconnecting for, with backoff instead of hammering it. */
export function useTaskEventStream(taskId: string | undefined) {
  const [events, setEvents] = useState<StreamedEvent[]>([]);
  const [status, setStatus] = useState<StreamStatus>("connecting");
  // Bumped by retry() to re-run the effect below without taskId itself changing.
  const [retryToken, setRetryToken] = useState(0);
  // Persisted in a ref, not a `let` inside the effect: a manual retry() re-runs the effect
  // for the *same* task, and must resume from the last seq seen rather than replaying the
  // whole timeline (and duplicating it into `events`, which retry() does not clear).
  const lastSeqRef = useRef<number | undefined>(undefined);
  const previousTaskIdRef = useRef<string | undefined>(undefined);

  useEffect(() => {
    if (!taskId) return;
    // Narrows to `string` once and for all: TypeScript does not carry the guard above into
    // functions declared below it (they could, in principle, run after `taskId` changed).
    const id = taskId;

    // A genuine task id change (not a retry() of the same task) starts over: the previous
    // task's seq numbering means nothing here. In practice this never actually fires — the
    // route changes before a live TaskDetail would ever see a different id — but it is the
    // correctness net that `<ExecucaoPanel key={id}>` used to provide before the stream
    // moved up to the page, so a caller that stops remounting on id change stays correct.
    if (previousTaskIdRef.current !== id) {
      previousTaskIdRef.current = id;
      lastSeqRef.current = undefined;
      setEvents([]);
    }

    const controller = new AbortController();
    let cancelled = false;
    let attempt = 0;

    async function connectOnce(): Promise<ConnectOutcome> {
      const response = await openTaskEventStream(id, {
        afterId: lastSeqRef.current,
        signal: controller.signal,
      });
      setStatus("open");
      // Reaching an open connection at all proves the backend is reachable again: the next
      // failure (if any) starts backing off from the base delay, not from wherever a
      // previous run of failures had climbed to.
      attempt = 0;

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
          lastSeqRef.current = parsed.seq;
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
          // event. Falls through to the backoff + reconnect below, same as a 5xx.
        } catch (err) {
          if (controller.signal.aborted) {
            return; // unmount, taskId change, or a manual retry tore this down mid-connect
          }
          if (err instanceof StreamError) {
            if (err.status === 401) {
              notifyUnauthorized();
              setStatus("closed");
              return;
            }
            if (err.status >= 400 && err.status < 500) {
              setStatus("failed");
              return; // waits for retry(); see the effect's dependency on retryToken
            }
          }
          setStatus("error"); // 5xx or a network error: worth retrying, see below
        }
        if (cancelled) return;
        const delay = Math.min(BASE_RECONNECT_DELAY_MS * 2 ** attempt, MAX_RECONNECT_DELAY_MS);
        attempt += 1;
        await new Promise((resolve) => setTimeout(resolve, delay));
      }
    }

    void run();

    return () => {
      cancelled = true;
      controller.abort();
    };
  }, [taskId, retryToken]);

  const retry = useCallback(() => setRetryToken((token) => token + 1), []);

  return { events, status, retry };
}
