import { useMutation } from "@tanstack/react-query";
import { type FormEvent, useState } from "react";
import { useNavigate } from "react-router-dom";
import { ApiError, apiFetch } from "../api/client";
import type { HTTPValidationError, TaskCreate, TaskOut } from "../api/types";

function submitTask(body: TaskCreate): Promise<TaskOut> {
  return apiFetch<TaskOut>("/tasks", {
    method: "POST",
    json: body,
    // One key per submit attempt: crypto.randomUUID() is a native platform feature
    // (Web Crypto API), no uuid dependency needed for this.
    headers: { "Idempotency-Key": crypto.randomUUID() },
  });
}

/** The `detail` list a 422 from this API carries (warden.api.app's validation handler). */
function validationMessages(error: unknown): string[] {
  if (!(error instanceof ApiError) || error.status !== 422) {
    return [];
  }
  const body = error.body as HTTPValidationError | null;
  return (body?.detail ?? []).map((item) => item.msg);
}

export function SubmitTask() {
  const navigate = useNavigate();
  const [spec, setSpec] = useState("");
  const [targetRepo, setTargetRepo] = useState("");
  const [emptySpecError, setEmptySpecError] = useState(false);

  const { mutate, isPending, error } = useMutation({
    mutationFn: submitTask,
    onSuccess: (task) => navigate(`/tarefas/${task.id}`),
  });

  function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const trimmedSpec = spec.trim();
    if (trimmedSpec.length === 0) {
      setEmptySpecError(true);
      return;
    }
    setEmptySpecError(false);
    mutate({ spec: trimmedSpec, target_repo: targetRepo.trim() || undefined });
  }

  const serverValidationErrors = validationMessages(error);
  const isUnexpectedError = error !== null && !(error instanceof ApiError && error.status === 422);

  return (
    <main>
      <h1>Submeter tarefa</h1>
      <form onSubmit={handleSubmit} noValidate className="mt-4 flex max-w-[72ch] flex-col gap-4">
        <div className="flex flex-col gap-1">
          <label htmlFor="spec">Especificação da tarefa</label>
          <textarea
            id="spec"
            required
            rows={8}
            value={spec}
            onChange={(event) => setSpec(event.target.value)}
          />
        </div>
        <div className="flex flex-col gap-1">
          <label htmlFor="target-repo">Repositório alvo (opcional)</label>
          <input
            id="target-repo"
            type="text"
            value={targetRepo}
            onChange={(event) => setTargetRepo(event.target.value)}
          />
        </div>

        {emptySpecError && <p role="alert" className="text-danger">Descreva a tarefa antes de enviar.</p>}
        {serverValidationErrors.map((message) => (
          <p role="alert" className="text-danger" key={message}>
            {message}
          </p>
        ))}
        {isUnexpectedError && (
          <p role="alert" className="text-danger">
            Falha ao enviar a tarefa. Tente novamente.
          </p>
        )}

        <button type="submit" className="self-start bg-accent text-bg" disabled={isPending}>
          {isPending ? "Enviando…" : "Enviar tarefa"}
        </button>
      </form>
    </main>
  );
}
