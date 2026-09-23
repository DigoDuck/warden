import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import { Tabs } from "./Tabs";

function renderTabs(defaultTabId?: string) {
  return render(
    <Tabs
      label="Detalhe da tarefa"
      defaultTabId={defaultTabId}
      tabs={[
        { id: "spec", label: "Spec", panel: <p>Conteúdo da spec</p> },
        { id: "execucao", label: "Execução", panel: <p>Conteúdo da execução</p> },
        { id: "custo", label: "Custo", panel: <p>Conteúdo do custo</p> },
      ]}
    />,
  );
}

describe("Tabs", () => {
  it("uses the tablist/tab/tabpanel roles", () => {
    renderTabs();
    expect(screen.getByRole("tablist", { name: "Detalhe da tarefa" })).toBeInTheDocument();
    expect(screen.getAllByRole("tab")).toHaveLength(3);
    expect(screen.getByRole("tabpanel")).toBeInTheDocument();
  });

  it("shows the first tab's panel by default and hides the others", () => {
    renderTabs();
    expect(screen.getByText("Conteúdo da spec")).toBeVisible();
    expect(screen.queryByText("Conteúdo da execução")).not.toBeInTheDocument();
  });

  it("selects defaultTabId's panel instead of the first tab when given", () => {
    renderTabs("execucao");
    expect(screen.getByRole("tab", { name: "Execução" })).toHaveAttribute("aria-selected", "true");
    expect(screen.getByText("Conteúdo da execução")).toBeVisible();
    expect(screen.queryByText("Conteúdo da spec")).not.toBeInTheDocument();
  });

  it("switches panel on click and marks the clicked tab selected", async () => {
    renderTabs();
    await userEvent.click(screen.getByRole("tab", { name: "Execução" }));
    expect(screen.getByRole("tab", { name: "Execução" })).toHaveAttribute("aria-selected", "true");
    expect(screen.getByText("Conteúdo da execução")).toBeVisible();
  });

  it("moves focus and selection with ArrowRight/ArrowLeft, wrapping at the ends", async () => {
    renderTabs();
    const [spec, execucao, custo] = screen.getAllByRole("tab");
    spec.focus();

    await userEvent.keyboard("{ArrowRight}");
    expect(execucao).toHaveFocus();
    expect(execucao).toHaveAttribute("aria-selected", "true");

    await userEvent.keyboard("{ArrowRight}");
    expect(custo).toHaveFocus();

    await userEvent.keyboard("{ArrowRight}"); // wraps back to the first tab
    expect(spec).toHaveFocus();

    await userEvent.keyboard("{ArrowLeft}"); // wraps back to the last tab
    expect(custo).toHaveFocus();
  });

  it("jumps to the first/last tab with Home/End", async () => {
    renderTabs();
    const [spec, , custo] = screen.getAllByRole("tab");
    spec.focus();

    await userEvent.keyboard("{End}");
    expect(custo).toHaveFocus();

    await userEvent.keyboard("{Home}");
    expect(spec).toHaveFocus();
  });

  it("only the selected tab is in the normal Tab order (roving tabindex)", () => {
    renderTabs();
    const [spec, execucao, custo] = screen.getAllByRole("tab");
    expect(spec).toHaveAttribute("tabindex", "0");
    expect(execucao).toHaveAttribute("tabindex", "-1");
    expect(custo).toHaveAttribute("tabindex", "-1");
  });
});
