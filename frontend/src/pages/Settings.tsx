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
    <div>
      <h1>Configurações</h1>
      <p>
        Ainda não existe <code>/auth/login</code>. Gere um token no backend com{" "}
        <code>
          make user-token email=voce@exemplo.com scopes="tasks:write tasks:read audit:read
          approvals:read approvals:decide" role=worker
        </code>{" "}
        e cole abaixo. Sem <code>role=worker</code>, as tarefas desse usuário só leem o
        repositório: a policy só deixa alterar código a pedido de quem tem esse papel.
      </p>
      {/* sessionStorage, não localStorage: token de dev colado à mão, vale só para esta
          aba/janela e some ao fechá-la (ver frontend/README.md). */}
      <form onSubmit={handleSubmit} className="mt-4 flex max-w-[72ch] flex-col gap-3">
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
        <div className="flex gap-2">
          <button type="submit" className="bg-accent text-bg">
            Salvar
          </button>
          <button type="button" className="border-border-control" onClick={handleClear}>
            Limpar
          </button>
        </div>
      </form>
      {saved && <p role="status">Token salvo nesta aba.</p>}
    </div>
  );
}
