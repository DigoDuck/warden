import { useQuery } from "@tanstack/react-query";
import { Link, useParams } from "react-router-dom";
import { ApiError, apiFetch } from "../api/client";
import type { TaskOut } from "../api/types";

// TODO(skeleton): no StatusBadge, no cancel button, no tabs, no cost formatting, no live
// timeline. Filled in next commit.
export function TaskDetail() {
  const { id } = useParams<{ id: string }>();
  const taskPath = encodeURIComponent(id ?? "");

  const taskQuery = useQuery({
    queryKey: ["task", id],
    queryFn: () => apiFetch<TaskOut>(`/tasks/${taskPath}`),
    enabled: Boolean(id),
  });

  if (!id) {
    return (
      <main>
        <p role="alert">Id de tarefa inválido.</p>
      </main>
    );
  }

  return (
    <main>
      <p>
        <Link to="/">Tarefas</Link>
      </p>
      <h1>Tarefa {id}</h1>

      {taskQuery.isLoading && <p>Carregando tarefa…</p>}
      {taskQuery.isError && (
        <p role="alert">
          {taskQuery.error instanceof ApiError && taskQuery.error.status === 404
            ? "Tarefa não encontrada."
            : "Falha ao carregar a tarefa."}
        </p>
      )}
      {taskQuery.data && <p>{taskQuery.data.status}</p>}
    </main>
  );
}
