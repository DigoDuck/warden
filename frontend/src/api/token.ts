// sessionStorage, not localStorage (see frontend/README.md "Autenticação de
// desenvolvimento"): this is a dev token pasted by hand on the Configuracoes page, and it
// should not outlive the tab it was pasted into or follow the user into a new tab/window
// that never saw it.
const STORAGE_KEY = "warden.token";

export function getToken(): string | null {
  return sessionStorage.getItem(STORAGE_KEY);
}

export function setToken(token: string): void {
  sessionStorage.setItem(STORAGE_KEY, token);
}

export function clearToken(): void {
  sessionStorage.removeItem(STORAGE_KEY);
}
