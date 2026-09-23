import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { StatusBadge } from "./StatusBadge";

// Mirrors warden/models.py's TASK_STATUSES and DESIGN.md's status table.
const STATUSES_AND_LABELS: [string, string][] = [
  ["QUEUED", "Na fila"],
  ["RUNNING", "Executando"],
  ["WAITING_APPROVAL", "Aguardando aprovação"],
  ["SUCCEEDED", "Concluída"],
  ["FAILED", "Falhou"],
  ["CANCELLED", "Cancelada"],
  ["TIMED_OUT", "Tempo esgotado"],
  ["BUDGET_EXCEEDED", "Orçamento excedido"],
];

describe("StatusBadge", () => {
  it.each(STATUSES_AND_LABELS)("shows the Portuguese label for %s", (status, label) => {
    render(<StatusBadge status={status} />);
    expect(screen.getByText(label)).toBeInTheDocument();
  });

  it("never renders color without text: every icon has a visible label next to it", () => {
    for (const [status] of STATUSES_AND_LABELS) {
      const { container, unmount } = render(<StatusBadge status={status} />);
      // DESIGN.md "Cor nunca sozinha": an icon alone, even with an aria-label, fails a
      // colorblind reading of the page. There must be visible text content too.
      expect(container.textContent?.trim()).not.toBe("");
      unmount();
    }
  });

  it("falls back to the raw status text for an unrecognised value instead of crashing", () => {
    render(<StatusBadge status="SOMETHING_NEW" />);
    expect(screen.getByText("SOMETHING_NEW")).toBeInTheDocument();
  });
});
