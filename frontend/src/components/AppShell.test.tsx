import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { Approvals } from "../pages/Approvals";
import { Settings } from "../pages/Settings";
import { SubmitTask } from "../pages/SubmitTask";
import { TaskDetail } from "../pages/TaskDetail";
import { Tasks } from "../pages/Tasks";
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

  it("closes the mobile <details> menu after navigating to a new page", async () => {
    renderShell();
    const details = document.querySelector("details");
    const summary = details?.querySelector("summary");
    if (!details || !summary) {
      throw new Error("expected the mobile <details>/<summary> menu to be in the document");
    }

    await userEvent.click(summary); // opens the menu
    expect(details.open).toBe(true);

    const links = screen.getAllByRole("link", { name: "Nova tarefa" });
    await userEvent.click(links[0]); // the mobile nav's own copy, inside <details>

    await waitFor(() => expect(details.open).toBe(false));
  });

  it("gives the pending-approvals badge its accessible name via visually-hidden text", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(new Response(JSON.stringify([{ id: "1" }, { id: "2" }]), { status: 200 })),
    );
    renderShell();

    // Rendered twice (mobile menu copy + desktop nav copy of NavItems), same as every other
    // nav item's accessible name in this shell.
    const srTexts = await screen.findAllByText("2 aprovações pendentes");
    expect(srTexts.length).toBeGreaterThan(0);
    for (const el of srTexts) expect(el).toHaveClass("sr-only");
    // The visible numeral is a sibling, hidden from assistive tech so its text is not read
    // a second time on top of the sr-only sentence above.
    expect(screen.getAllByText("2").some((el) => el.getAttribute("aria-hidden") === "true")).toBe(
      true,
    );
  });

  it.each([
    ["/", <Tasks />],
    ["/tarefas/nova", <SubmitTask />],
    ["/tarefas/:id", <TaskDetail />],
    ["/aprovacoes", <Approvals />],
    ["/configuracoes", <Settings />],
  ])("renders exactly one main landmark on %s", (path, page) => {
    // Never resolves: every page stays in its first render, which is all this looks at.
    vi.stubGlobal("fetch", vi.fn(() => new Promise(() => {})));
    render(
      <QueryClientProvider client={new QueryClient()}>
        <MemoryRouter initialEntries={[path.replace(":id", "abc")]}>
          <Routes>
            <Route element={<AppShell />}>
              <Route path={path} element={page} />
            </Route>
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>,
    );
    // AppShell owns <main id="main-content">; a page rendering its own <main> nests a
    // second main landmark inside it (invalid HTML, two "main" stops for a screen reader).
    expect(screen.getAllByRole("main")).toHaveLength(1);
  });
});
