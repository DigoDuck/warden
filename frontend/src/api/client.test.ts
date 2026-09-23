import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import viteConfig from "../../vite.config";
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

  it("calls the API same-origin under /api, not the backend's own origin", async () => {
    // The backend has no CORS middleware, so a cross-origin call from the Vite dev server
    // (localhost:5173 -> :8000) dies on the preflight. Same-origin + the dev proxy avoids it.
    vi.mocked(fetch).mockResolvedValue(new Response("{}", { status: 200 }));

    await apiFetch("/tasks/1");

    expect(vi.mocked(fetch).mock.calls[0][0]).toBe("/api/tasks/1");
  });

  it("has the Vite dev server forward /api to the backend with the prefix stripped", () => {
    const proxy = viteConfig.server?.proxy?.["/api"];
    if (proxy === undefined || typeof proxy === "string") {
      throw new Error(`expected an /api proxy object in vite.config.ts, got ${String(proxy)}`);
    }
    expect(proxy.target).toBe("http://127.0.0.1:8000");
    expect(proxy.rewrite?.("/api/tasks/1/events")).toBe("/tasks/1/events");
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
