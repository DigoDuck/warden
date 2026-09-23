/** Incremental parser for a `text/event-stream` body read as raw text chunks. See
 * docs/adr/ADR-024 for why this exists instead of the browser's `EventSource`.
 */

export interface SseMessage {
  id: string | undefined;
  event: string;
  data: string;
}

export class SseParser {
  // TODO(skeleton): does not buffer partial lines across chunks, does not skip comment
  // lines, and never tracks `id`. Filled in next commit.
  feed(chunk: string): SseMessage[] {
    const messages: SseMessage[] = [];
    for (const block of chunk.split("\n\n")) {
      if (!block) continue;
      messages.push({ id: undefined, event: "message", data: block });
    }
    return messages;
  }
}
