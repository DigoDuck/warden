import { getToken } from "./token";

/** A non-2xx response, typed instead of leaving callers to inspect `Response` by hand. */
export class ApiError extends Error {
  readonly status: number;
  readonly body: unknown;

  constructor(status: number, body: unknown) {
    super(`request failed with status ${status}`);
    this.name = "ApiError";
    this.status = status;
    this.body = body;
  }
}

type UnauthorizedHandler = () => void;

// Module-level instead of React context: the router lives above every page that calls
// apiFetch, so the handler is registered once from App.tsx (useNavigate to /configuracoes)
// and every call site below stays free of a Router dependency, which keeps this file
// testable with fetch alone.
let unauthorizedHandler: UnauthorizedHandler = () => {};

export function onUnauthorized(handler: UnauthorizedHandler): void {
  unauthorizedHandler = handler;
}

export interface ApiFetchInit extends Omit<RequestInit, "body"> {
  /** JSON-serialised as the request body; sets Content-Type automatically. */
  json?: unknown;
}

// Same-origin on purpose: the backend has no CORS middleware, so a browser call from the dev
// server's origin straight to :8000 dies on the preflight. `vite.config.ts` proxies /api to
// the backend in dev; in production the reverse proxy (Caddy, week 12) plays the same role.
const API_BASE_URL = import.meta.env.VITE_API_BASE_URL ?? "/api";

export async function apiFetch<T>(path: string, init: ApiFetchInit = {}): Promise<T> {
  const { json, headers, ...rest } = init;
  const token = getToken();

  const response = await fetch(`${API_BASE_URL}${path}`, {
    ...rest,
    headers: {
      ...(json !== undefined ? { "Content-Type": "application/json" } : {}),
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
      ...headers,
    },
    body: json !== undefined ? JSON.stringify(json) : undefined,
  });

  if (!response.ok) {
    if (response.status === 401) {
      unauthorizedHandler();
    }
    const body: unknown = await response.json().catch(() => null);
    throw new ApiError(response.status, body);
  }

  if (response.status === 204) {
    return undefined as T;
  }
  return (await response.json()) as T;
}
