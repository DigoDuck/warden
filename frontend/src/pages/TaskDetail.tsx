import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { Link, useParams } from "react-router-dom";
import { ApiError, apiFetch } from "../api/client";
import type { TaskOut } from "../api/types";
import { StatusBadge } from "../components/StatusBadge";
import { Tabs } from "../components/Tabs";
import { Timeline } from "../components/Timeline";
import { useTaskEventStream, type StreamedEvent, type StreamStatus } from "../hooks/useTaskEventStream";

// The state machine from warden/models.py (TASK_STATUSES): once a task reaches one of
// these, core/worker.py never picks it up again, so polling past this point is pointless
// and cancelling it is meaningless (routes_tasks.py already answers 409 for it).
const TERMINAL_STATUSES = new Set([
  "SUCCEEDED",
  "FAILED",
  "CANCELLED",
  "TIMED_OUT",
  "BUDGET_EXCEEDED",
]);

const POLL_INTERVAL_MS = 4000;

// Kept as the exact string from tasks.cost_usd (a Postgres NUMERIC, serialised as a
// string): parsing it through Number and reformatting would risk the very floating-point
// rounding the backend's Decimal column exists to avoid (see warden/models.py::ModelCall).
function formatCost(cost: string): string {
  return `US$ ${cost}`;
}

function SpecPanel({ task }: { task: TaskOut }) {
  return (
    <dl className="flex flex-col gap-2">
      <div>
        <dt className="text-fg-muted">Especificação</dt>
        <dd className="whitespace-pre-wrap">{task.spec}</dd>
      </div>
      <div>
        <dt className="text-fg-muted">Repositório alvo</dt>
        <dd className="font-mono text-xs">{task.target_repo ?? "—"}</dd>
      </div>
    </dl>
  );
}

// The stream itself is owned by TaskDetail, not this panel (see the Tabs call below): Tabs
// unmounts an inactive panel, and mounting useTaskEventStream *inside* this component would
// tear the connection down and reconnect from seq 0 every time Spec or Custo is shown
// instead. This panel only ever renders whatever the page hands it.
function ExecucaoPanel({
  events,
  status,
  onRetry,
}: {
  events: StreamedEvent[];
  status: StreamStatus;
  onRetry: () => void;
}) {
  return (
    <div className="flex flex-col gap-3">
      {status === "error" && <p role="alert">Conexão perdida, tentando reconectar…</p>}
      {status === "failed" && (
        <div role="alert" className="flex items-center gap-3">
          <p>Não foi possível manter a conexão com os eventos.</p>
          <button type="button" className="border-border-control" onClick={onRetry}>
            Tentar de novo
          </button>
        </div>
      )}
      <Timeline events={events} />
    </div>
  );
}

function CustoPanel({ task }: { task: TaskOut }) {
  return (
    <dl className="flex flex-col gap-2">
      <div>
        <dt className="text-fg-muted">Custo</dt>
        <dd className="tabular-nums">{formatCost(task.cost_usd)}</dd>
      </div>
      <div>
        <dt className="text-fg-muted">Iterações</dt>
        <dd className="tabular-nums">{task.iterations}</dd>
      </div>
    </dl>
  );
}

export function TaskDetail() {
  const { id } = useParams<{ id: string }>();
  // Decoded and re-encoded (not passed through raw): a crafted link like
  // /tarefas/..%2F..%2Faudit stays one path segment of /tasks/{id} instead of steering the
  // bearer token to another endpoint.
  const taskPath = encodeURIComponent(id ?? "");
  const queryClient = useQueryClient();
  const [cancelMessage, setCancelMessage] = useState<string | null>(null);
  // Owned here, not inside the Execução tab's own panel: Tabs unmounts an inactive panel,
  // and the connection (plus everything it already received) must survive a trip through
  // Spec or Custo instead of reconnecting from seq 0 every time Execução is reselected.
  const stream = useTaskEventStream(id);

  const taskQuery = useQuery({
    queryKey: ["task", id],
    queryFn: () => apiFetch<TaskOut>(`/tasks/${taskPath}`),
    enabled: Boolean(id),
    refetchInterval: (query) =>
      query.state.data && TERMINAL_STATUSES.has(query.state.data.status) ? false : POLL_INTERVAL_MS,
  });

  const cancelMutation = useMutation({
    mutationFn: () => apiFetch<TaskOut>(`/tasks/${taskPath}/cancel`, { method: "POST" }),
    onSuccess: (task) => {
      queryClient.setQueryData(["task", id], task);
      // The response body is the same TaskOut either way; only its resulting status tells
      // 200 (already stopped) apart from 202 (marked, still stopping) without needing
      // apiFetch to plumb the raw HTTP status code through for this one caller.
      setCancelMessage(
        task.status === "CANCELLED"
          ? "Tarefa cancelada."
          : "Cancelamento solicitado: a tarefa vai parar em breve.",
      );
    },
    onError: () => setCancelMessage("Falha ao cancelar a tarefa."),
  });

  function handleCancel() {
    if (window.confirm("Cancelar esta tarefa? Essa ação não pode ser desfeita.")) {
      setCancelMessage(null);
      cancelMutation.mutate();
    }
  }

  if (!id) {
    return (
      <div>
        <p role="alert">Id de tarefa inválido.</p>
      </div>
    );
  }

  const task = taskQuery.data;
  const cancelDisabled = cancelMutation.isPending || (task ? TERMINAL_STATUSES.has(task.status) : true);

  return (
    <div>
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

      {task && (
        <>
          <div className="mt-2 flex flex-wrap items-center gap-4">
            <StatusBadge status={task.status} />
            <span className="tabular-nums text-sm text-fg-muted">{formatCost(task.cost_usd)}</span>
            <button
              type="button"
              className="bg-danger-solid text-fg"
              onClick={handleCancel}
              disabled={cancelDisabled}
            >
              {cancelMutation.isPending ? "Cancelando…" : "Cancelar tarefa"}
            </button>
          </div>
          {cancelMessage && <p role="status">{cancelMessage}</p>}

          <div className="mt-6">
            <Tabs
              label="Detalhe da tarefa"
              // DESIGN.md "pronto quando" (semana 4): ver eventos chegando ao vivo sem
              // precisar clicar em nada primeiro.
              defaultTabId="execucao"
              tabs={[
                { id: "spec", label: "Spec", panel: <SpecPanel task={task} /> },
                {
                  id: "execucao",
                  label: "Execução",
                  // events/status/retry come from the page-level stream above, not a hook
                  // call inside this panel: that is what keeps the connection open and the
                  // timeline intact while Spec or Custo is the visible tab.
                  panel: (
                    <ExecucaoPanel
                      events={stream.events}
                      status={stream.status}
                      onRetry={stream.retry}
                    />
                  ),
                },
                { id: "custo", label: "Custo", panel: <CustoPanel task={task} /> },
              ]}
            />
          </div>
        </>
      )}
    </div>
  );
}
