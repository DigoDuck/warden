import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { ProvenanceBadge } from "./ProvenanceBadge";

// Model output and deterministic evidence must never read as the same kind of thing.
describe("ProvenanceBadge", () => {
  it("labels model output as Gerado", () => {
    render(<ProvenanceBadge kind="gerado" />);
    expect(screen.getByText("Gerado")).toBeInTheDocument();
  });

  it("labels deterministic evidence as Verificado", () => {
    render(<ProvenanceBadge kind="verificado" />);
    expect(screen.getByText("Verificado")).toBeInTheDocument();
  });

  it("narrows the source in the same label when given a detail", () => {
    render(<ProvenanceBadge kind="gerado" detail="revisor independente" />);
    expect(screen.getByText("Gerado · revisor independente")).toBeInTheDocument();
  });
});
