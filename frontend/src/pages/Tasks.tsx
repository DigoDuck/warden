import { useInfiniteQuery } from "@tanstack/react-query";
import type { ChangeEvent } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { ApiError, apiFetch } from "../api/client";
import type { TaskListOut } from "../api/types";
import { StatusBadge, TASK_STATUS_ENTRIES } from "../components/StatusBadge";
import { TASK_STATUSES } from "../lib/taskStatuses";

// ADR-024: the list polls (many tasks, none of which need per-event latency) while the
// detail page holds one live SSE connection (one task, "see events arrive live").
const POLL_INTERVAL_MS = 4000;
const SKELETON_ROWS = 5;

function formatDate(iso: string): string {
  return new Date(iso).toLocaleString("pt-BR");
}

export function Tasks() {
  const [searchParams, setSearchParams] = useSearchParams();
  const status = searchParams.get("status") ?? "";

  const tasksQuery = useInfiniteQuery({
    queryKey: ["tasks", status],
    queryFn: ({ pageParam }) => {
      const params = new URLSearchParams();
      if (status) params.set("status", status);
      if (pageParam) params.set("cursor", pageParam);
      const query = params.toString();
      return apiFetch<TaskListOut>(`/tasks${query ? `?${query}` : ""}`);
    },
    initialPageParam: undefined as string | undefined,
    getNextPageParam: (lastPage) => lastPage.next_cursor ?? undefined,
    refetchInterval: POLL_INTERVAL_MS,
  });

  function handleStatusChange(event: ChangeEvent<HTMLSelectElement>) {
    const next = event.target.value;
    setSearchParams(next ? { status: next } : {});
  }

  const tasks = tasksQuery.data?.pages.flatMap((p) => p.tasks) ?? [];
  const hasNextPage = Boolean(tasksQuery.data?.pages.at(-1)?.next_cursor);

  return (
    <main>
      <h1>Tarefas</h1>

      <div className="mt-4 flex items-center gap-3">
        <label htmlFor="status-filter" className="text-sm text-fg-muted">
          Status
        </label>
        <select id="status-filter" value={status} onChange={handleStatusChange}>
          <option value="">Todos</option>
          {TASK_STATUSES.map((s) => (
            <option key={s} value={s}>
              {TASK_STATUS_ENTRIES[s]?.label ?? s}
            </option>
          ))}
        </select>
      </div>

      {tasksQuery.isLoading && (
        <div className="mt-4" aria-live="polite">
          <p>Carregando tarefas…</p>
          <div className="flex flex-col gap-1" aria-hidden="true">
            {Array.from({ length: SKELETON_ROWS }, (_, i) => (
              <div key={i} className="h-10 rounded-md bg-surface" />
            ))}
          </div>
        </div>
      )}

      {tasksQuery.isError && (
        <div className="mt-4" role="alert">
          <p>
            Falha ao carregar as tarefas.{" "}
            {tasksQuery.error instanceof ApiError ? `(${tasksQuery.error.status})` : ""}
          </p>
          <button type="button" onClick={() => tasksQuery.refetch()}>
            Tentar de novo
          </button>
        </div>
      )}

      {!tasksQuery.isLoading && !tasksQuery.isError && tasks.length === 0 && (
        <p className="mt-4">
          Nenhuma tarefa ainda. <Link to="/tarefas/nova">Submeter tarefa</Link>
        </p>
      )}

      {tasks.length > 0 && (
        <>
          <table className="mt-4 w-full text-left text-sm">
            <thead>
              <tr className="h-10 border-b border-border-subtle text-fg-muted">
                <th scope="col">Especificação</th>
                <th scope="col">Status</th>
                <th scope="col">Repositório</th>
                <th scope="col">Criada em</th>
              </tr>
            </thead>
            <tbody>
              {tasks.map((task) => (
                <tr key={task.id} className="h-10 border-b border-border-subtle">
                  <td>
                    <Link to={`/tarefas/${encodeURIComponent(task.id)}`}>{task.spec}</Link>
                  </td>
                  <td>
                    <StatusBadge status={task.status} />
                  </td>
                  <td className="font-mono text-xs">{task.target_repo ?? "—"}</td>
                  <td className="tabular-nums">{formatDate(task.created_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>

          {hasNextPage && (
            <button
              type="button"
              className="mt-4"
              onClick={() => tasksQuery.fetchNextPage()}
              disabled={tasksQuery.isFetchingNextPage}
            >
              {tasksQuery.isFetchingNextPage ? "Carregando…" : "Carregar mais"}
            </button>
          )}
        </>
      )}
    </main>
  );
}
