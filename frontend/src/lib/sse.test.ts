import { describe, expect, it } from "vitest";
import { SseParser } from "./sse";

describe("SseParser", () => {
  it("parses a single complete message delivered in one chunk", () => {
    const parser = new SseParser();
    const messages = parser.feed('id: 1\nevent: task.finished\ndata: {"status":"SUCCEEDED"}\n\n');
    expect(messages).toEqual([{ id: "1", event: "task.finished", data: '{"status":"SUCCEEDED"}' }]);
  });

  it("buffers a message split across chunk boundaries, mid-field", () => {
    const parser = new SseParser();
    // The boundary lands in the middle of the "event:" field name itself.
    expect(parser.feed("id: 2\nev")).toEqual([]);
    expect(parser.feed('ent: policy.decided\ndata: {"effect":"deny"}\n\n')).toEqual([
      { id: "2", event: "policy.decided", data: '{"effect":"deny"}' },
    ]);
  });

  it("buffers a message split right on the terminating blank line", () => {
    const parser = new SseParser();
    expect(parser.feed("id: 3\nevent: tool.executed\ndata: {}\n")).toEqual([]);
    expect(parser.feed("\n")).toEqual([{ id: "3", event: "tool.executed", data: "{}" }]);
  });

  it("emits more than one message from a single chunk", () => {
    const parser = new SseParser();
    const messages = parser.feed(
      "id: 1\nevent: a\ndata: one\n\nid: 2\nevent: b\ndata: two\n\n",
    );
    expect(messages).toEqual([
      { id: "1", event: "a", data: "one" },
      { id: "2", event: "b", data: "two" },
    ]);
  });

  it("ignores comment lines (the backend's heartbeat)", () => {
    const parser = new SseParser();
    const messages = parser.feed(": heartbeat\n\nid: 1\nevent: a\ndata: x\n\n");
    expect(messages).toEqual([{ id: "1", event: "a", data: "x" }]);
  });

  it("a heartbeat split across chunks never becomes a message", () => {
    const parser = new SseParser();
    expect(parser.feed(": heart")).toEqual([]);
    expect(parser.feed("beat\n\n")).toEqual([]);
  });

  it("keeps the last id sticky across a message that sends none", () => {
    const parser = new SseParser();
    parser.feed("id: 5\nevent: a\ndata: x\n\n");
    const messages = parser.feed("event: b\ndata: y\n\n");
    expect(messages).toEqual([{ id: "5", event: "b", data: "y" }]);
  });

  it("defaults the event type to message when none is sent", () => {
    const parser = new SseParser();
    expect(parser.feed("data: x\n\n")).toEqual([{ id: undefined, event: "message", data: "x" }]);
  });
});
