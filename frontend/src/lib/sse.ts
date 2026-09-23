/** Incremental parser for a `text/event-stream` body read as raw text chunks.
 *
 * Used instead of the browser's `EventSource`: `EventSource` cannot set a custom
 * `Authorization` header, and the bearer token must never go in the URL (it would land in
 * access logs) — see docs/adr/ADR-024 and the backend's GET /tasks/{id}/stream. The
 * frontend reads the response body with `fetch` + `ReadableStream` instead, and feeds each
 * chunk to this parser as it arrives.
 *
 * A chunk boundary never lines up with an SSE field or message boundary (TCP/HTTP give no
 * such guarantee), so this buffers whatever partial line a chunk ends on and only emits
 * messages once their terminating blank line has actually been seen.
 */

export interface SseMessage {
  /** The stream's own event id (`id: <seq>`), sticky across events per the SSE spec: a
   * message that carries no `id` field keeps the last one seen. */
  id: string | undefined;
  /** Defaults to "message", same as `EventSource`, when no `event:` field is sent. */
  event: string;
  data: string;
}

export class SseParser {
  #buffer = "";
  #eventType = "message";
  #dataLines: string[] = [];
  #id: string | undefined;

  /** Feed one chunk of the response body. Returns every message completed by this chunk;
   * usually zero or one, but a chunk can carry more than one full message. */
  feed(chunk: string): SseMessage[] {
    this.#buffer += chunk;
    const lines = this.#buffer.split("\n");
    // The last split element is the start of the next line, still waiting for its own "\n"
    // (or "" if the chunk happened to end right on one); either way it stays buffered.
    this.#buffer = lines.pop() ?? "";

    const messages: SseMessage[] = [];
    for (const rawLine of lines) {
      const line = rawLine.endsWith("\r") ? rawLine.slice(0, -1) : rawLine;

      if (line === "") {
        // A blank line dispatches the event (SSE spec); the backend's heartbeat comment
        // never reaches here (filtered below) so this only fires for a real message.
        if (this.#dataLines.length > 0) {
          messages.push({ id: this.#id, event: this.#eventType, data: this.#dataLines.join("\n") });
        }
        this.#eventType = "message";
        this.#dataLines = [];
        continue;
      }
      if (line.startsWith(":")) {
        continue; // comment line: the backend's heartbeat, carries no field
      }

      const colon = line.indexOf(":");
      const field = colon === -1 ? line : line.slice(0, colon);
      // A single leading space after the colon is stripped, per the SSE field spec.
      const value = colon === -1 ? "" : line.slice(colon + 1).replace(/^ /, "");

      if (field === "id") {
        this.#id = value;
      } else if (field === "event") {
        this.#eventType = value;
      } else if (field === "data") {
        this.#dataLines.push(value);
      }
      // Any other field name is ignored, same as a real EventSource.
    }
    return messages;
  }
}
