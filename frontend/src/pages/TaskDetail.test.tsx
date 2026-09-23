import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { TaskDetail } from "./TaskDetail";

const TASK_ID = "11111111-1111-1111-1111-111111111111";

function task(overrides: Partial<Record<string, unknown>> = {}) {
  return {
    id: TASK_ID,
    status: "RUNNING",
    spec: "faz algo importante",
    target_repo: "org/repo",
    created_at: "2026-01-01T00:00:00Z",
    started_at: "2026-01-01T00:00:01Z",
    finished_at: null,
    cost_usd: "0.120000",
    iterations: 3,
    ...overrides,
  };
}

function emptySseResponse(): Response {
  return new Response(new ReadableStream({ start: (c) => c.close() }), {
    status: 200,
    headers: { "Content-Type": "text/event-stream" },
  });
}

/** Serves each URL its own queue of bodies, repeating the last one once it runs out. The
 * stream endpoint gets an empty SSE response by default (no events, connection stays
 * "open" from the hook's point of view since it never reads a done chunk... actually the
 * empty stream closes immediately, which is fine: the Execução tab tests below only check
 * that the tab mounts and shows the "no events" message.
 */
function routeFetch(routes: Record<string, unknown[]>) {
  const served: Record<string, number> = {};
  vi.mocked(fetch).mockImplementation(async (input) => {
    const path = String(input).replace(/^\/api/, "").replace(/^http:\/\/localhost/, "");
    if (path.endsWith("/stream")) {
      return emptySseResponse();
    }
    const bodies = routes[path];
    if (!bodies) {
      return new Response(JSON.stringify({ detail: "not found" }), { status: 404 });
    }
    const index = Math.min(served[path] ?? 0, bodies.length - 1);
    served[path] = index + 1;
    return new Response(JSON.stringify(bodies[index]), { status: 200 });
  });
}

function renderAt(path: string) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[path]}>
        <Routes>
          <Route path="/tarefas/:id" element={<TaskDetail />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("TaskDetail", () => {
  beforeEach(() => {
    vi.stubGlobal("fetch", vi.fn());
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("shows a loading state, then the task's status and cost", async () => {
    routeFetch({ [`/tasks/${TASK_ID}`]: [task()] });
    renderAt(`/tarefas/${TASK_ID}`);

    expect(screen.getByText(/carregando tarefa/i)).toBeInTheDocument();
    expect(await screen.findByText("Executando")).toBeInTheDocument();
    expect(screen.getByText(/0\.120000/)).toBeInTheDocument();
  });

  it("shows a not-found message for a missing task", async () => {
    routeFetch({});
    renderAt(`/tarefas/${TASK_ID}`);
    expect(await screen.findByText("Tarefa não encontrada.")).toBeInTheDocument();
  });

  it("shows the spec tab's content by default", async () => {
    routeFetch({ [`/tasks/${TASK_ID}`]: [task()] });
    renderAt(`/tarefas/${TASK_ID}`);
    expect(await screen.findByText("faz algo importante")).toBeInTheDocument();
  });

  it("switches to the Execução tab and mounts the live timeline", async () => {
    routeFetch({ [`/tasks/${TASK_ID}`]: [task()] });
    renderAt(`/tarefas/${TASK_ID}`);
    await screen.findByText("faz algo importante");

    await userEvent.click(screen.getByRole("tab", { name: "Execução" }));
    expect(await screen.findByText(/nenhum evento ainda/i)).toBeInTheDocument();
  });

  it("does not cancel when the confirmation dialog is declined", async () => {
    routeFetch({ [`/tasks/${TASK_ID}`]: [task()] });
    vi.spyOn(window, "confirm").mockReturnValue(false);
    renderAt(`/tarefas/${TASK_ID}`);
    await screen.findByText("Executando");

    await userEvent.click(screen.getByRole("button", { name: /cancelar tarefa/i }));

    const calls = vi.mocked(fetch).mock.calls.map(([input]) => String(input));
    expect(calls.some((url) => url.includes("/cancel"))).toBe(false);
  });

  it("cancels a running task (202) and reports it is still stopping", async () => {
    routeFetch({
      [`/tasks/${TASK_ID}`]: [task({ status: "RUNNING" })],
      [`/tasks/${TASK_ID}/cancel`]: [task({ status: "RUNNING" })],
    });
    vi.spyOn(window, "confirm").mockReturnValue(true);
    renderAt(`/tarefas/${TASK_ID}`);
    await screen.findByText("Executando");

    await userEvent.click(screen.getByRole("button", { name: /cancelar tarefa/i }));

    expect(await screen.findByText(/cancelamento solicitado/i)).toBeInTheDocument();
    await waitFor(() =>
      expect(
        vi.mocked(fetch).mock.calls.some(([input]) => String(input).includes("/cancel")),
      ).toBe(true),
    );
  });

  it("cancels a queued task (200) and reports it was cancelled", async () => {
    routeFetch({
      [`/tasks/${TASK_ID}`]: [task({ status: "QUEUED" })],
      [`/tasks/${TASK_ID}/cancel`]: [task({ status: "CANCELLED" })],
    });
    vi.spyOn(window, "confirm").mockReturnValue(true);
    renderAt(`/tarefas/${TASK_ID}`);
    await screen.findByText("Na fila");

    await userEvent.click(screen.getByRole("button", { name: /cancelar tarefa/i }));

    expect(await screen.findByText(/tarefa cancelada/i)).toBeInTheDocument();
  });
});
