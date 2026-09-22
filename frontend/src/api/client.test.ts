import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { apiFetch, ApiError, onUnauthorized } from "./client";
import { clearToken, setToken } from "./token";

// The client wrapper's whole job (frontend/README.md): attach the bearer token from
// sessionStorage to every request, turn a non-2xx response into a typed ApiError instead of
// a raw Response, and let the app react to an expired/invalid token (401) in one place
// instead of every call site checking `response.status` by hand.
describe("apiFetch", () => {
  beforeEach(() => {
    clearToken();
    vi.stubGlobal("fetch", vi.fn());
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("sends the stored bearer token as an Authorization header", async () => {
    setToken("abc123");
    vi.mocked(fetch).mockResolvedValue(new Response(JSON.stringify({ ok: true }), { status: 200 }));

    await apiFetch("/tasks/1");

    const [, init] = vi.mocked(fetch).mock.calls[0];
    const headers = new Headers(init?.headers);
    expect(headers.get("Authorization")).toBe("Bearer abc123");
  });

  it("sends no Authorization header when no token is stored", async () => {
    vi.mocked(fetch).mockResolvedValue(new Response("{}", { status: 200 }));

    await apiFetch("/healthz");

    const [, init] = vi.mocked(fetch).mock.calls[0];
    const headers = new Headers(init?.headers);
    expect(headers.has("Authorization")).toBe(false);
  });

  it("resolves with the parsed JSON body on a 2xx response", async () => {
    vi.mocked(fetch).mockResolvedValue(
      new Response(JSON.stringify({ id: "1", status: "queued" }), { status: 201 }),
    );

    await expect(apiFetch("/tasks")).resolves.toEqual({ id: "1", status: "queued" });
  });

  it("throws an ApiError carrying the status and parsed body on a non-2xx response", async () => {
    vi.mocked(fetch).mockResolvedValue(new Response(JSON.stringify({ detail: "nope" }), { status: 422 }));

    const failure = apiFetch("/tasks");
    await expect(failure).rejects.toBeInstanceOf(ApiError);
    await expect(failure).rejects.toMatchObject({ status: 422, body: { detail: "nope" } });
  });

  it("calls the registered unauthorized handler on a 401 response", async () => {
    const handler = vi.fn();
    onUnauthorized(handler);
    vi.mocked(fetch).mockResolvedValue(new Response("{}", { status: 401 }));

    await expect(apiFetch("/tasks")).rejects.toBeInstanceOf(ApiError);
    expect(handler).toHaveBeenCalledOnce();
  });
});
