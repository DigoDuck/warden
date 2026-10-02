import { render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import type { TaskOut } from "../api/types";
import { DecisionPanel } from "./DecisionPanel";

function task(overrides: Partial<TaskOut> = {}): TaskOut {
  return {
    id: "11111111-1111-1111-1111-111111111111",
    status: "SUCCEEDED",
    spec: "faz algo",
    target_repo: null,
    created_at: "2026-01-01T00:00:00Z",
    started_at: null,
    finished_at: null,
    cost_usd: "0.010000",
    iterations: 2,
    summary: null,
    verdict: null,
    ...overrides,
  };
}

const VERDICT = {
  passed: true,
  findings: [],
  verifier: "independent",
  malformed_reason: null,
  created_at: "2026-01-01T00:00:00Z",
};

describe("DecisionPanel", () => {
  it("shows the coder's summary under a Gerado badge, apart from the verdict", () => {
    render(<DecisionPanel task={task({ summary: "corrigi o bug, tudo perfeito" })} />);

    const summary = screen.getByRole("region", { name: /relato do agente/i });
    expect(within(summary).getByText("corrigi o bug, tudo perfeito")).toBeInTheDocument();
    expect(within(summary).getByText("Gerado")).toBeInTheDocument();
  });

  it("labels the verdict Gerado too: a reviewer is a model, not a check", () => {
    render(<DecisionPanel task={task({ verdict: VERDICT })} />);

    const verdict = screen.getByRole("region", { name: /veredito do revisor/i });
    expect(within(verdict).getByText("Gerado · revisor independente")).toBeInTheDocument();
    expect(within(verdict).getByText("Aprovado")).toBeInTheDocument();
    expect(within(verdict).queryByText("Verificado")).not.toBeInTheDocument();
    // And nothing on this tab claims to be verified: that word is for evidence only.
    expect(screen.queryByText("Verificado")).not.toBeInTheDocument();
  });

  it("lists the findings of a rejecting verdict", () => {
    render(
      <DecisionPanel
        task={task({
          status: "FAILED",
          verdict: { ...VERDICT, passed: false, findings: ["None ainda quebra average()", "falta teste"] },
        })}
      />,
    );

    const verdict = screen.getByRole("region", { name: /veredito do revisor/i });
    expect(within(verdict).getByText("Reprovado")).toBeInTheDocument();
    const items = within(verdict).getAllByRole("listitem");
    expect(items.map((item) => item.textContent)).toEqual([
      "None ainda quebra average()",
      "falta teste",
    ]);
  });

  it("shows why a malformed verdict failed the task, and never calls it approved", () => {
    render(
      <DecisionPanel
        task={task({
          status: "FAILED",
          verdict: {
            ...VERDICT,
            passed: false,
            malformed_reason: "the reviewer did not call submit_verdict",
          },
        })}
      />,
    );

    expect(screen.getByRole("alert")).toHaveTextContent(
      /veredito malformado.*the reviewer did not call submit_verdict/i,
    );
    expect(screen.queryByText("Aprovado")).not.toBeInTheDocument();
  });

  it("explains a FAILED task whose reviewer approved: the model cannot override a red check", () => {
    render(<DecisionPanel task={task({ status: "FAILED", verdict: VERDICT })} />);

    expect(screen.getByText(/revisor aprovou/i)).toBeInTheDocument();
    expect(screen.getByText(/evidência/i)).toBeInTheDocument();
  });

  it("shows the control plane's decision as the final status", () => {
    render(<DecisionPanel task={task({ status: "FAILED", verdict: VERDICT })} />);

    const decision = screen.getByRole("region", { name: /decisão do control plane/i });
    expect(within(decision).getByText("Falhou")).toBeInTheDocument();
  });

  it("has an empty state for a task that has not finished or been reviewed", () => {
    render(<DecisionPanel task={task({ status: "RUNNING" })} />);

    expect(screen.getByText(/ainda sem relato/i)).toBeInTheDocument();
    expect(screen.getByText(/ainda sem veredito/i)).toBeInTheDocument();
  });

  it("says so when a finished task never went through the independent review", () => {
    render(<DecisionPanel task={task({ status: "CANCELLED" })} />);

    expect(screen.getByText(/sem revisão independente/i)).toBeInTheDocument();
  });
});
