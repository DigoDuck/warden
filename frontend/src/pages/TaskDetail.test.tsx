import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { TaskDetail } from "./TaskDetail";

const TASK_ID = "11111111-1111-1111-1111-111111111111";

function task(status: string) {
  return {
    id: TASK_ID,
    status,
    spec: "faz algo",
    target_repo: null,
    created_at: "2026-01-01T00:00:00Z",
    started_at: null,
    finished_at: null,
    cost_usd: "0",
    iterations: 0,
  };
}

function eventPage(types: string[]) {
  return {
    events: types.map((type, index) => ({
      seq: index + 1,
      type,
      payload: {},
      created_at: "2026-01-01T00:00:00Z",
    })),
    next_after: null,
  };
}

/** Serves each URL its own queue of bodies, repeating the last one once it runs out. */
function routeFetch(routes: Record<string, unknown[]>) {
  const served: Record<string, number> = {};
  vi.mocked(fetch).mockImplementation(async (input) => {
    const path = String(input).replace(/^\/api/, "");
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

  it("fetches the events once more after the task turns terminal", async () => {
    // The race: the poll that first sees SUCCEEDED runs alongside an events poll that read
    // the table just before the worker's final commit. Polling stops right there, so without
    // one last events fetch `task.finished` would never show up on the page.
    routeFetch({
      [`/tasks/${TASK_ID}`]: [task("RUNNING"), task("SUCCEEDED")],
      [`/tasks/${TASK_ID}/events`]: [
        eventPage(["task.started"]),
        eventPage(["task.started"]),
        eventPage(["task.started", "task.finished"]),
      ],
    });

    renderAt(`/tarefas/${TASK_ID}`);

    expect(await screen.findByText("SUCCEEDED", {}, { timeout: 4000 })).toBeInTheDocument();
    expect(await screen.findByText("task.finished")).toBeInTheDocument();
  });
});
