import { useQuery } from "@tanstack/react-query";
import { Link, useParams } from "react-router-dom";
import { ApiError, apiFetch } from "../api/client";
import type { TaskEventPage, TaskOut } from "../api/types";

// The state machine from warden/models.py (TASK_STATUSES): once a task reaches one of
// these, core/worker.py never picks it up again, so polling past this point would just
// hit the same row forever.
const TERMINAL_STATUSES = new Set([
  "SUCCEEDED",
  "FAILED",
  "CANCELLED",
  "TIMED_OUT",
  "BUDGET_EXCEEDED",
]);

// SSE lands in week 5 (docs/plano-12-semanas.md); 2s polling is the interim mechanism the
// task explicitly asks for.
const POLL_INTERVAL_MS = 2000;

function stopPollingWhenTerminal(status: string | undefined): number | false {
  return status && TERMINAL_STATUSES.has(status) ? false : POLL_INTERVAL_MS;
}

export function TaskDetail() {
  const { id } = useParams<{ id: string }>();

  const taskQuery = useQuery({
    queryKey: ["task", id],
    queryFn: () => apiFetch<TaskOut>(`/tasks/${id}`),
    enabled: Boolean(id),
    refetchInterval: (query) => stopPollingWhenTerminal(query.state.data?.status),
  });

  const eventsQuery = useQuery({
    // ponytail: only the first page (backend default limit=100). A "carregar mais" control
    // for TaskEventPage.next_after can wait for a task long enough to need it.
    queryKey: ["task", id, "events"],
    queryFn: () => apiFetch<TaskEventPage>(`/tasks/${id}/events`),
    enabled: Boolean(id),
    refetchInterval: () => stopPollingWhenTerminal(taskQuery.data?.status),
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
        <Link to="/">Início</Link>
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
      {taskQuery.data && (
        <dl>
          <dt>Status</dt>
          <dd>{taskQuery.data.status}</dd>
          <dt>Especificação</dt>
          <dd>{taskQuery.data.spec}</dd>
          <dt>Repositório alvo</dt>
          <dd>{taskQuery.data.target_repo ?? "—"}</dd>
          <dt>Custo (USD)</dt>
          <dd>{taskQuery.data.cost_usd}</dd>
          <dt>Iterações</dt>
          <dd>{taskQuery.data.iterations}</dd>
        </dl>
      )}

      <h2>Eventos</h2>
      {eventsQuery.isLoading && <p>Carregando eventos…</p>}
      {eventsQuery.isError && <p role="alert">Falha ao carregar os eventos.</p>}
      {eventsQuery.data && eventsQuery.data.events.length === 0 && <p>Nenhum evento ainda.</p>}
      {eventsQuery.data && eventsQuery.data.events.length > 0 && (
        <ul>
          {eventsQuery.data.events.map((event) => (
            <li key={event.seq}>
              <strong>{event.type}</strong> — {event.created_at}
            </li>
          ))}
        </ul>
      )}
    </main>
  );
}
