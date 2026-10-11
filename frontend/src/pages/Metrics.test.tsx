import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { Metrics } from "./Metrics";

function summary(overrides: Partial<Record<string, unknown>> = {}) {
  return {
    tasks: 4,
    by_status: { SUCCEEDED: 3, FAILED: 1, CANCELLED: 0, TIMED_OUT: 0, BUDGET_EXCEEDED: 0 },
    successes: 3,
    success_rate: 0.75,
    cost_total: "1.200000",
    cost_per_task: "0.300000",
    cost_per_success: "0.400000",
    tokens_in_per_task: 1500,
    tokens_out_per_task: 250.5,
    latency_p50_s: 12.4,
    latency_p95_s: 95,
    avg_iterations: 3.5,
    tool_calls_per_task: 6,
    tool_failure_rate: 0.125,
    interventions_per_task: 0.25,
    deny_rate: null,
    ...overrides,
  };
}

function ok(body: unknown) {
  return new Response(JSON.stringify(body), { status: 200 });
}

function renderMetrics() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <Metrics />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("Metrics", () => {
  beforeEach(() => {
    vi.stubGlobal("fetch", vi.fn());
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("shows a loading state before the first response arrives", () => {
    vi.mocked(fetch).mockReturnValue(new Promise(() => {}));
    renderMetrics();
    expect(screen.getByText(/carregando métricas/i)).toBeInTheDocument();
  });

  it("shows an empty state when no task finished in the period", async () => {
    vi.mocked(fetch).mockResolvedValue(
      ok(summary({ tasks: 0, success_rate: null, cost_per_task: null })),
    );
    renderMetrics();
    expect(await screen.findByText(/nenhuma tarefa concluída neste período/i)).toBeInTheDocument();
    expect(screen.queryByRole("table")).not.toBeInTheDocument();
  });

  it("shows the cause and a retry button on error, and retries", async () => {
    vi.mocked(fetch)
      .mockResolvedValueOnce(new Response("{}", { status: 500 }))
      .mockResolvedValueOnce(ok(summary()));
    renderMetrics();
    expect(await screen.findByRole("alert")).toHaveTextContent(/falha ao carregar as métricas/i);

    await userEvent.click(screen.getByRole("button", { name: /tentar de novo/i }));

    expect(await screen.findByText("75%")).toBeInTheDocument();
    expect(fetch).toHaveBeenCalledTimes(2);
  });

  it("shows every indicator formatted, and a dash where there is no data", async () => {
    vi.mocked(fetch).mockResolvedValue(ok(summary()));
    renderMetrics();

    expect(await screen.findByText("75%")).toBeInTheDocument(); // taxa de sucesso
    expect(screen.getByText("US$ 0,30")).toBeInTheDocument(); // custo por tarefa
    expect(screen.getByText("US$ 0,40")).toBeInTheDocument(); // custo por sucesso
    expect(screen.getByText("12,4 s")).toBeInTheDocument(); // p50
    expect(screen.getByText("1 min 35 s")).toBeInTheDocument(); // p95
    expect(screen.getByText("12,5%")).toBeInTheDocument(); // falha de tool
    expect(screen.getByRole("row", { name: /taxa de negação/i })).toHaveTextContent("—");
    // Numbers are read as columns of digits.
    expect(screen.getByText("75%")).toHaveClass("tabular-nums");
    // Counts by status come with the status text, not color alone.
    expect(screen.getByRole("row", { name: /^concluída 3$/i })).toBeInTheDocument();
  });

  it("asks for the selected period", async () => {
    vi.mocked(fetch).mockResolvedValue(ok(summary()));
    renderMetrics();
    await screen.findByText("75%");
    expect(String(vi.mocked(fetch).mock.calls[0][0])).toMatch(/\/metrics\/summary\?from=/);

    await userEvent.selectOptions(screen.getByLabelText(/período/i), "all");

    await waitFor(() => {
      const last = String(vi.mocked(fetch).mock.calls.at(-1)?.[0]);
      expect(last).toMatch(/\/metrics\/summary$/);
    });
  });
});
