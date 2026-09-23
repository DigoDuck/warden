import { type FormEvent, useState } from "react";
import { clearToken, getToken, setToken } from "../api/token";

export function Settings() {
  const [token, setTokenValue] = useState(getToken() ?? "");
  const [saved, setSaved] = useState(false);

  function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const trimmed = token.trim();
    if (!trimmed) {
      return;
    }
    setToken(trimmed);
    setSaved(true);
  }

  function handleClear() {
    clearToken();
    setTokenValue("");
    setSaved(false);
  }

  return (
    <main>
      <h1>Configurações</h1>
      <p>
        Ainda não existe <code>/auth/login</code>. Gere um token no backend com{" "}
        <code>make user-token email=voce@exemplo.com scopes="tasks:write tasks:read audit:read"</code>{" "}
        e cole abaixo.
      </p>
      {/* sessionStorage, não localStorage: token de dev colado à mão, vale só para esta
          aba/janela e some ao fechá-la (ver frontend/README.md). */}
      <form onSubmit={handleSubmit}>
        <label htmlFor="token">Token</label>
        <input
          id="token"
          type="password"
          autoComplete="off"
          value={token}
          onChange={(event) => {
            setTokenValue(event.target.value);
            setSaved(false);
          }}
        />
        <button type="submit">Salvar</button>
        <button type="button" onClick={handleClear}>
          Limpar
        </button>
      </form>
      {saved && <p role="status">Token salvo nesta aba.</p>}
    </main>
  );
}
