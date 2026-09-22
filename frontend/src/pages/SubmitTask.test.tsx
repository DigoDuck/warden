import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { SubmitTask } from "./SubmitTask";

function renderPage() {
  const client = new QueryClient();
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <SubmitTask />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

const TASK_OUT = {
  id: "11111111-1111-1111-1111-111111111111",
  status: "queued",
  spec: "faz algo",
  target_repo: null,
  created_at: "2026-01-01T00:00:00Z",
  started_at: null,
  finished_at: null,
  cost_usd: "0",
  iterations: 0,
};

describe("SubmitTask", () => {
  beforeEach(() => {
    vi.stubGlobal("fetch", vi.fn());
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("blocks submission and shows a message when spec is empty", async () => {
    renderPage();

    await userEvent.click(screen.getByRole("button", { name: /enviar tarefa/i }));

    expect(await screen.findByRole("alert")).toHaveTextContent(/descreva a tarefa/i);
    expect(fetch).not.toHaveBeenCalled();
  });

  it("submits the spec with a fresh Idempotency-Key header", async () => {
    vi.mocked(fetch).mockResolvedValue(new Response(JSON.stringify(TASK_OUT), { status: 201 }));

    renderPage();
    await userEvent.type(screen.getByLabelText(/especificação da tarefa/i), "faz algo");
    await userEvent.click(screen.getByRole("button", { name: /enviar tarefa/i }));

    await waitFor(() => expect(fetch).toHaveBeenCalledTimes(1));
    const [url, init] = vi.mocked(fetch).mock.calls[0];
    expect(String(url)).toContain("/tasks");
    const headers = new Headers(init?.headers);
    expect(headers.get("Idempotency-Key")).toBeTruthy();
    expect(JSON.parse(String(init?.body))).toMatchObject({ spec: "faz algo" });
  });

  it("shows the server's 422 validation message", async () => {
    vi.mocked(fetch).mockResolvedValue(
      new Response(
        JSON.stringify({
          detail: [
            {
              type: "string_too_long",
              loc: ["body", "spec"],
              msg: "String should have at most 20000 characters",
            },
          ],
        }),
        { status: 422 },
      ),
    );

    renderPage();
    await userEvent.type(screen.getByLabelText(/especificação da tarefa/i), "faz algo");
    await userEvent.click(screen.getByRole("button", { name: /enviar tarefa/i }));

    expect(await screen.findByText(/at most 20000 characters/i)).toBeInTheDocument();
  });
});
