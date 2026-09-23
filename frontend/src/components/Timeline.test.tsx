import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { Timeline } from "./Timeline";
import type { StreamedEvent } from "../hooks/useTaskEventStream";

function event(overrides: Partial<StreamedEvent>): StreamedEvent {
  return {
    seq: 1,
    type: "policy.decided",
    payload: {},
    created_at: "2026-01-01T00:00:00Z",
    ...overrides,
  };
}

describe("Timeline", () => {
  it("shows an empty message when there are no events yet", () => {
    render(<Timeline events={[]} />);
    expect(screen.getByText(/nenhum evento ainda/i)).toBeInTheDocument();
  });

  it("renders a policy.decided deny with its matched rule, in Fira Code", () => {
    render(
      <Timeline
        events={[
          event({
            type: "policy.decided",
            payload: { tool: "run_command", effect: "deny", matched_rules: ["no-network"] },
          }),
        ]}
      />,
    );
    expect(screen.getByText("Negado")).toBeInTheDocument();
    const rule = screen.getByText("no-network");
    expect(rule).toBeInTheDocument();
    expect(rule.className).toContain("font-mono");
  });

  it("renders a policy.decided allow without a bare rule list when there are none", () => {
    render(
      <Timeline
        events={[event({ type: "policy.decided", payload: { tool: "read_file", effect: "allow", matched_rules: [] } })]}
      />,
    );
    expect(screen.getByText("Permitido")).toBeInTheDocument();
  });

  it("renders tool.requested and tool.executed readably", () => {
    render(
      <Timeline
        events={[
          event({ seq: 1, type: "tool.requested", payload: { tool: "write_file" } }),
          event({ seq: 2, type: "tool.executed", payload: { tool: "write_file", ok: true } }),
        ]}
      />,
    );
    expect(screen.getByText(/tool solicitada/i)).toBeInTheDocument();
    expect(screen.getByText(/tool executada/i)).toBeInTheDocument();
  });

  it("renders approval and finish events readably", () => {
    render(
      <Timeline
        events={[
          event({ seq: 1, type: "approval.requested", payload: { tool: "open_pr" } }),
          event({ seq: 2, type: "approval.granted", payload: {} }),
          event({ seq: 3, type: "task.finished", payload: { status: "SUCCEEDED" } }),
        ]}
      />,
    );
    expect(screen.getByText(/aguardando aprovação/i)).toBeInTheDocument();
    expect(screen.getByText(/aprovação concedida/i)).toBeInTheDocument();
    expect(screen.getByText("Concluída")).toBeInTheDocument();
  });
});
