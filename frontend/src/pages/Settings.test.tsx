import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { Settings } from "./Settings";

describe("Settings", () => {
  it("gives the secondary 'Limpar' button a --border-control border (DESIGN.md)", () => {
    render(<Settings />);
    expect(screen.getByRole("button", { name: /limpar/i })).toHaveClass("border-border-control");
  });
});
