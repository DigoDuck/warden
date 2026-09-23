import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { Tasks } from "./Tasks";

function task(overrides: Partial<Record<string, unknown>> = {}) {
  return {
    id: "11111111-1111-1111-1111-111111111111",
    status: "RUNNING",
    spec: "faz algo importante",
    target_repo: null,
    created_at: "2026-01-01T00:00:00Z",
    started_at: null,
    finished_at: null,
    ...overrides,
  };
}

function page(tasks: unknown[], nextCursor: string | null = null) {
  return { tasks, next_cursor: nextCursor };
}

function renderTasks(initialEntries: string[] = ["/"]) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={initialEntries}>
        <Routes>
          <Route path="/" element={<Tasks />} />
          <Route
            path="/tarefas/:id"
            element={<h1>Tarefa aberta</h1>}
          />
          <Route path="/tarefas/nova" element={<h1>Nova tarefa</h1>} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("Tasks", () => {
  beforeEach(() => {
    vi.stubGlobal("fetch", vi.fn());
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("shows a loading state before the first response arrives", () => {
    vi.mocked(fetch).mockReturnValue(new Promise(() => {})); // never resolves
    renderTasks();
    expect(screen.getByText(/carregando tarefas/i)).toBeInTheDocument();
  });

  it("shows an empty state with an action when there are no tasks", async () => {
    vi.mocked(fetch).mockResolvedValue(new Response(JSON.stringify(page([])), { status: 200 }));
    renderTasks();
    expect(await screen.findByText(/nenhuma tarefa ainda/i)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /submeter tarefa/i })).toHaveAttribute(
      "href",
      "/tarefas/nova",
    );
  });

  it("shows an error state with a retry action on failure", async () => {
    vi.mocked(fetch).mockResolvedValue(new Response("boom", { status: 500 }));
    renderTasks();
    expect(await screen.findByText(/falha ao carregar/i)).toBeInTheDocument();

    vi.mocked(fetch).mockResolvedValue(
      new Response(JSON.stringify(page([task()])), { status: 200 }),
    );
    await userEvent.click(screen.getByRole("button", { name: /tentar de novo/i }));
    expect(await screen.findByText("faz algo importante")).toBeInTheDocument();
  });

  it("renders one row per task with a status badge and a link to its detail page", async () => {
    vi.mocked(fetch).mockResolvedValue(
      new Response(JSON.stringify(page([task({ status: "FAILED" })])), { status: 200 }),
    );
    renderTasks();

    const row = await screen.findByRole("row", { name: /faz algo importante/i });
    expect(within(row).getByText("Falhou")).toBeInTheDocument();
    expect(within(row).getByRole("link")).toHaveAttribute(
      "href",
      "/tarefas/11111111-1111-1111-1111-111111111111",
    );
  });

  it("keeps the status filter in the URL so the view deep-links", async () => {
    vi.mocked(fetch).mockResolvedValue(new Response(JSON.stringify(page([])), { status: 200 }));
    renderTasks();
    await screen.findByText(/nenhuma tarefa ainda/i);

    await userEvent.selectOptions(screen.getByLabelText(/status/i), "FAILED");

    await waitFor(() => {
      const lastUrl = String(vi.mocked(fetch).mock.calls.at(-1)?.[0]);
      expect(lastUrl).toContain("status=FAILED");
    });
  });

  it("lets a keyboard user reach and activate a row's link", async () => {
    vi.mocked(fetch).mockResolvedValue(
      new Response(JSON.stringify(page([task()])), { status: 200 }),
    );
    renderTasks();
    const link = await screen.findByRole("link", { name: /faz algo importante/i });

    link.focus();
    expect(link).toHaveFocus();
    await userEvent.keyboard("{Enter}");

    expect(await screen.findByRole("heading", { name: "Tarefa aberta" })).toBeInTheDocument();
  });
});
