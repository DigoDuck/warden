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

/** Live timeline for one task's events (docs/adr/ADR-024): connects once, does not
 * reconnect on a drop and does not stop cleanly at the terminal event.
 * TODO(skeleton): filled in next commit.
 */
export function useTaskEventStream(taskId: string | undefined) {
  const [events, setEvents] = useState<StreamedEvent[]>([]);
  const [status, setStatus] = useState<StreamStatus>("connecting");

  useEffect(() => {
    if (!taskId) return;
    setEvents([]);
    setStatus("connecting");

    const controller = new AbortController();

    async function run() {
      const response = await openTaskEventStream(taskId!, { signal: controller.signal });
      setStatus("open");
      const parser = new SseParser();
      const reader = response.body!.getReader();
      const decoder = new TextDecoder();
      const { value } = await reader.read();
      for (const message of parser.feed(decoder.decode(value ?? new Uint8Array()))) {
        if (!message.data) continue;
        setEvents((prev) => [...prev, JSON.parse(message.data) as StreamedEvent]);
      }
      setStatus("closed");
    }

    void run();
    return () => controller.abort();
  }, [taskId]);

  return { events, status };
}
