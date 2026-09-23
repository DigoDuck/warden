import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { AppShell } from "./AppShell";

function renderShell() {
  const client = new QueryClient();
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={["/"]}>
        <Routes>
          <Route element={<AppShell />}>
            <Route path="/" element={<h1>Página A</h1>} />
            <Route path="/tarefas/nova" element={<h1>Página B</h1>} />
          </Route>
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("AppShell", () => {
  beforeEach(() => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response("[]", { status: 200 })));
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('has "Pular para o conteúdo" as the very first focusable element', async () => {
    renderShell();
    await userEvent.tab();
    expect(screen.getByRole("link", { name: /pular para o conteúdo/i })).toHaveFocus();
  });

  it("moves focus to the new page's h1 after navigating", async () => {
    renderShell();
    // Two nav links share the same accessible name once (mobile summary + desktop nav both
    // render "Nova tarefa"); the desktop one is what a keyboard/mouse user on a real
    // >=1024px screen would use, so it is picked by index rather than failing on ambiguity.
    const links = screen.getAllByRole("link", { name: "Nova tarefa" });
    await userEvent.click(links[links.length - 1]);

    const heading = await screen.findByRole("heading", { name: "Página B" });
    expect(heading).toHaveFocus();
  });
});
