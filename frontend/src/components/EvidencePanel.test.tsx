import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { EvidencePanel } from "./EvidencePanel";

const TASK_ID = "11111111-1111-1111-1111-111111111111";

function command(kind: string, status: string, output: string, exitCode: number | null = 0) {
  return {
    kind,
    created_at: "2026-01-01T00:00:00Z",
    payload: {
      kind,
      status,
      passed: status === "passed",
      commands: [
        { argv: [kind, "--check", "."], exit_code: exitCode, output, duration_ms: 1234 },
      ],
    },
  };
}

const DIFF = {
  kind: "diff",
  created_at: "2026-01-01T00:00:00Z",
  payload: {
    kind: "diff",
    status: "ok",
    files: [
      { path: "src/app.py", change: "modified", additions: 3, deletions: 1, binary: false },
      { path: "tests/test_new.py", change: "added", additions: 5, deletions: 0, binary: false },
    ],
    files_changed: 2,
    additions: 8,
    deletions: 1,
    patch: "--- a/src/app.py\n+++ b/src/app.py\n+def helper() -> int:\n",
    patch_truncated: false,
  },
};

function serve(body: unknown, status = 200) {
  vi.mocked(fetch).mockImplementation(
    async () => new Response(JSON.stringify(body), { status }),
  );
}

function renderPanel(live = false) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <EvidencePanel taskPath={TASK_ID} live={live} />
    </QueryClientProvider>,
  );
}

describe("EvidencePanel", () => {
  beforeEach(() => {
    vi.stubGlobal("fetch", vi.fn());
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("shows a loading state, then an empty state when nothing was recorded yet", async () => {
    serve({ evidence: [] });
    renderPanel();

    expect(screen.getByText(/carregando evidência/i)).toBeInTheDocument();
    expect(await screen.findByText(/nenhuma evidência ainda/i)).toBeInTheDocument();
  });

  it("shows the cause of a failure and lets the user try again", async () => {
    serve({ detail: "boom" }, 500);
    renderPanel();

    expect(await screen.findByRole("alert")).toHaveTextContent(/falha ao carregar a evidência/i);

    serve({ evidence: [DIFF] });
    await userEvent.click(screen.getByRole("button", { name: /tentar de novo/i }));
    expect(await screen.findByText("src/app.py")).toBeInTheDocument();
  });

  it("marks every evidence block as Verificado: it is computed by the control plane", async () => {
    serve({
      evidence: [DIFF, command("lint", "passed", "All checks passed!")],
    });
    renderPanel();

    await screen.findByText("All checks passed!");
    expect(screen.getAllByText("Verificado")).toHaveLength(2);
    expect(screen.queryByText(/^Gerado/)).not.toBeInTheDocument();
  });

  it("shows a command check's status, command, exit code and output", async () => {
    serve({
      evidence: [command("tests", "failed", "FAILED tests/test_app.py::test_average", 1)],
    });
    renderPanel();

    const block = (await screen.findByRole("region", { name: /testes/i })) as HTMLElement;
    expect(within(block).getByText("Falhou")).toBeInTheDocument();
    expect(within(block).getByText("tests --check .")).toBeInTheDocument();
    expect(within(block).getByText(/exit 1/)).toBeInTheDocument();
    expect(within(block).getByText(/FAILED tests\/test_app.py::test_average/)).toBeInTheDocument();
  });

  it.each([
    ["passed", "Passou"],
    ["timeout", "Tempo esgotado"],
    ["error", "Erro"],
  ])("labels a %s check as %s", async (status, label) => {
    serve({ evidence: [command("types", status, "out", status === "passed" ? 0 : null)] });
    renderPanel();

    expect(await screen.findByText(label)).toBeInTheDocument();
  });

  it("shows the diff's stats, file list and patch", async () => {
    serve({ evidence: [DIFF] });
    renderPanel();

    const block = (await screen.findByRole("region", { name: /diff/i })) as HTMLElement;
    expect(within(block).getByText(/2 arquivos/)).toBeInTheDocument();
    expect(within(block).getByText("+8")).toBeInTheDocument();
    expect(within(block).getByText("−1")).toBeInTheDocument();
    expect(within(block).getByText("src/app.py")).toBeInTheDocument();
    expect(within(block).getByText("tests/test_new.py")).toBeInTheDocument();
    expect(within(block).getByText("Adicionado")).toBeInTheDocument();
    // The patch sits in a monospace <pre> that a keyboard user can scroll.
    const patch = within(block).getByText(/\+def helper\(\) -> int:/);
    expect(patch.tagName).toBe("PRE");
    expect(patch).toHaveAttribute("tabindex", "0");
  });

  it("says when the patch was truncated and when the diff could not be collected", async () => {
    serve({
      evidence: [
        { ...DIFF, payload: { ...DIFF.payload, patch_truncated: true } },
        {
          kind: "diff",
          created_at: "2026-01-01T00:00:00Z",
          payload: { kind: "diff", status: "error", error: "export too large" },
        },
      ],
    });
    renderPanel();

    expect(await screen.findByText(/patch truncado/i)).toBeInTheDocument();
    expect(screen.getByText(/export too large/)).toBeInTheDocument();
  });
});
