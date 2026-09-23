import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { Link } from "react-router-dom";
import { ApiError, apiFetch } from "../api/client";
import type { ApprovalOut } from "../api/types";

const APPROVALS_QUERY_KEY = ["approvals", "pending"];
// The sidebar badge (AppShell) polls the exact same key, so both share one cache entry
// and one interval instead of two independent pollers disagreeing about the count.
const POLL_INTERVAL_MS = 5000;

function ApprovalCard({ approval }: { approval: ApprovalOut }) {
  const queryClient = useQueryClient();
  const [note, setNote] = useState("");
  const [conflict, setConflict] = useState(false);

  const decide = useMutation({
    mutationFn: (approve: boolean) =>
      apiFetch<ApprovalOut>(`/approvals/${approval.id}/${approve ? "approve" : "reject"}`, {
        method: "POST",
        json: { note: note.trim() || null },
      }),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: APPROVALS_QUERY_KEY });
    },
    onError: (error) => {
      if (error instanceof ApiError && error.status === 409) {
        // Another reviewer got there first. Not an error the person can retry: refresh the
        // list so the card that is no longer pending disappears on its own.
        setConflict(true);
        void queryClient.invalidateQueries({ queryKey: APPROVALS_QUERY_KEY });
      }
    },
  });

  const rejectDisabled = decide.isPending || note.trim().length === 0;

  return (
    <li className="border-b border-border-subtle py-4">
      <p>
        <Link to={`/tarefas/${approval.task_id}`}>Tarefa {approval.task_id}</Link>
      </p>
      <p className="mt-1 font-mono text-xs">{approval.tool}</p>
      <pre className="mt-1 whitespace-pre-wrap font-mono text-xs text-fg-muted">
        {JSON.stringify(approval.args_safe)}
      </pre>
      {approval.matched_rules.length > 0 && (
        <p className="mt-1 font-mono text-xs">{approval.matched_rules.join(", ")}</p>
      )}
      <p className="mt-1 text-sm">{approval.reason}</p>

      {conflict && <p role="alert">Esta aprovação já foi decidida por outra pessoa.</p>}

      <div className="mt-3 flex flex-col gap-2">
        <button
          type="button"
          className="self-start bg-accent text-bg"
          onClick={() => decide.mutate(true)}
          disabled={decide.isPending}
        >
          Aprovar
        </button>

        <label htmlFor={`note-${approval.id}`} className="text-sm text-fg-muted">
          Nota (obrigatória para rejeitar)
        </label>
        <textarea
          id={`note-${approval.id}`}
          value={note}
          onChange={(event) => setNote(event.target.value)}
        />
        {rejectDisabled && !decide.isPending && (
          <p className="text-xs text-fg-muted">Adicione uma nota para rejeitar.</p>
        )}
        <button
          type="button"
          className="self-start bg-danger-solid text-fg"
          onClick={() => decide.mutate(false)}
          disabled={rejectDisabled}
        >
          Rejeitar
        </button>
      </div>
    </li>
  );
}

export function Approvals() {
  const approvalsQuery = useQuery({
    queryKey: APPROVALS_QUERY_KEY,
    queryFn: () => apiFetch<ApprovalOut[]>("/approvals?status=pending"),
    refetchInterval: POLL_INTERVAL_MS,
  });

  return (
    <div>
      <h1>Aprovações</h1>

      {approvalsQuery.isLoading && <p>Carregando aprovações…</p>}

      {approvalsQuery.isError && (
        <div role="alert">
          <p>Falha ao carregar as aprovações.</p>
          <button type="button" onClick={() => approvalsQuery.refetch()}>
            Tentar de novo
          </button>
        </div>
      )}

      {approvalsQuery.data && approvalsQuery.data.length === 0 && (
        <p>Nenhuma aprovação pendente.</p>
      )}

      {approvalsQuery.data && approvalsQuery.data.length > 0 && (
        <ul>
          {approvalsQuery.data.map((approval) => (
            <ApprovalCard key={approval.id} approval={approval} />
          ))}
        </ul>
      )}
    </div>
  );
}
