import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { Approvals } from "./Approvals";

const APPROVAL_ID = "22222222-2222-2222-2222-222222222222";
const TASK_ID = "11111111-1111-1111-1111-111111111111";

function approval(overrides: Partial<Record<string, unknown>> = {}) {
  return {
    id: APPROVAL_ID,
    task_id: TASK_ID,
    tool: "github.open_pr",
    args_safe: { branch: "feat/x" },
    matched_rules: ["require-approval-open-pr"],
    reason: "abre PR em repositório protegido",
    status: "pending",
    requested_at: "2026-01-01T00:00:00Z",
    ...overrides,
  };
}

function renderApprovals() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <Approvals />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("Approvals (Decision Queue)", () => {
  beforeEach(() => {
    vi.stubGlobal("fetch", vi.fn());
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("shows the pending approval's task link, tool, rules and reason", async () => {
    vi.mocked(fetch).mockResolvedValue(
      new Response(JSON.stringify([approval()]), { status: 200 }),
    );
    renderApprovals();

    expect(await screen.findByText("github.open_pr")).toBeInTheDocument();
    expect(screen.getByText("require-approval-open-pr")).toBeInTheDocument();
    expect(screen.getByText(/abre pr em repositório protegido/i)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: new RegExp(TASK_ID) })).toHaveAttribute(
      "href",
      `/tarefas/${TASK_ID}`,
    );
  });

  it("approving a pending approval removes its card once the list refetches empty", async () => {
    vi.mocked(fetch).mockImplementation(async (input, init) => {
      const url = String(input);
      if (url.includes("/approve")) {
        expect(init?.method).toBe("POST");
        return new Response(JSON.stringify(approval({ status: "approved" })), { status: 200 });
      }
      // First GET returns the pending approval; every GET after the approve returns none.
      const alreadyApproved = vi.mocked(fetch).mock.calls.some(([i]) => String(i).includes("/approve"));
      return new Response(JSON.stringify(alreadyApproved ? [] : [approval()]), { status: 200 });
    });

    renderApprovals();
    await screen.findByText("github.open_pr");

    await userEvent.click(screen.getByRole("button", { name: /^aprovar$/i }));

    await waitFor(() => expect(screen.queryByText("github.open_pr")).not.toBeInTheDocument());
    expect(await screen.findByText(/nenhuma aprovação pendente/i)).toBeInTheDocument();
  });

  it("keeps Rejeitar disabled, and explains why, until a note is entered", async () => {
    vi.mocked(fetch).mockResolvedValue(
      new Response(JSON.stringify([approval()]), { status: 200 }),
    );
    renderApprovals();
    await screen.findByText("github.open_pr");

    const rejectButton = screen.getByRole("button", { name: /rejeitar/i });
    expect(rejectButton).toBeDisabled();
    expect(screen.getByText(/adicione uma nota para rejeitar/i)).toBeInTheDocument();

    await userEvent.type(screen.getByLabelText(/nota/i), "não autorizado neste repositório");
    expect(rejectButton).toBeEnabled();

    await userEvent.click(rejectButton);
    const rejectCall = vi.mocked(fetch).mock.calls.find(([input]) => String(input).includes("/reject"));
    expect(rejectCall).toBeDefined();
    expect(JSON.parse(String(rejectCall?.[1]?.body))).toMatchObject({
      note: "não autorizado neste repositório",
    });
  });

  it("asks for confirmation before rejecting, like every other destructive action", async () => {
    vi.mocked(fetch).mockResolvedValue(
      new Response(JSON.stringify([approval()]), { status: 200 }),
    );
    const confirmSpy = vi.spyOn(window, "confirm").mockReturnValue(false);
    renderApprovals();
    await screen.findByText("github.open_pr");

    await userEvent.type(screen.getByLabelText(/nota/i), "não autorizado neste repositório");
    await userEvent.click(screen.getByRole("button", { name: /rejeitar/i }));

    expect(confirmSpy).toHaveBeenCalledOnce();
    // Declined: the request must never go out.
    expect(
      vi.mocked(fetch).mock.calls.some(([input]) => String(input).includes("/reject")),
    ).toBe(false);

    confirmSpy.mockReturnValue(true);
    await userEvent.click(screen.getByRole("button", { name: /rejeitar/i }));
    await waitFor(() =>
      expect(
        vi.mocked(fetch).mock.calls.some(([input]) => String(input).includes("/reject")),
      ).toBe(true),
    );
  });

  it("gives the retry button a --border-control border (DESIGN.md)", async () => {
    vi.mocked(fetch).mockResolvedValue(new Response("boom", { status: 500 }));
    renderApprovals();
    expect(await screen.findByRole("button", { name: /tentar de novo/i })).toHaveClass(
      "border-border-control",
    );
  });

  it("handles a 409 (already decided by someone else) without crashing", async () => {
    vi.mocked(fetch).mockImplementation(async (input) => {
      const url = String(input);
      if (url.includes("/approve")) {
        return new Response(JSON.stringify({ detail: "approval already decided" }), {
          status: 409,
        });
      }
      return new Response(JSON.stringify([approval()]), { status: 200 });
    });

    renderApprovals();
    await screen.findByText("github.open_pr");

    await userEvent.click(screen.getByRole("button", { name: /^aprovar$/i }));

    expect(await screen.findByText(/já foi decidida/i)).toBeInTheDocument();
  });
});
