import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { EffectBadge } from "./EffectBadge";

// DESIGN.md's domain-semantics table (allow/deny/require_approval).
const EFFECTS_AND_LABELS: [string, string][] = [
  ["allow", "Permitido"],
  ["deny", "Negado"],
  ["require_approval", "Aguarda aprovação"],
];

describe("EffectBadge", () => {
  it.each(EFFECTS_AND_LABELS)("shows the Portuguese label for %s", (effect, label) => {
    render(<EffectBadge effect={effect} />);
    expect(screen.getByText(label)).toBeInTheDocument();
  });

  it("never renders color without text", () => {
    for (const [effect] of EFFECTS_AND_LABELS) {
      const { container, unmount } = render(<EffectBadge effect={effect} />);
      expect(container.textContent?.trim()).not.toBe("");
      unmount();
    }
  });
});
