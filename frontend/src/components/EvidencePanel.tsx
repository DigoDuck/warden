import { useQuery } from "@tanstack/react-query";
import { CheckCircle, TimerOff, XCircle } from "lucide-react";
import { apiFetch } from "../api/client";
import type { EvidenceListOut, EvidenceOut } from "../api/types";
import { Badge } from "./Badge";
import { ProvenanceBadge } from "./ProvenanceBadge";

// What verify/runner.py records, as it records it (CommandEvidence / DiffEvidence). The API
// serves the payload as an open dict so a new kind never needs an API change; this panel
// narrows it by `kind` and reads defensively, because a payload that does not match is a
// bug to show, not a reason to crash the tab.
type Payload = Record<string, unknown>;

interface CommandRun {
  argv: string[];
  exit_code: number | null;
  output: string;
  duration_ms: number;
}

interface FileChange {
  path: string;
  change: string;
  additions: number;
  deletions: number;
  binary: boolean;
}

const KIND_LABELS: Record<string, string> = {
  diff: "Diff",
  lint: "Lint",
  types: "Tipos",
  tests: "Testes",
};

const CHANGE_LABELS: Record<string, string> = {
  added: "Adicionado",
  removed: "Removido",
  modified: "Modificado",
};

function asArray<T>(value: unknown): T[] {
  return Array.isArray(value) ? (value as T[]) : [];
}

function asNumber(value: unknown): number {
  return typeof value === "number" ? value : 0;
}

function StatusBadge({ status }: { status: string }) {
  switch (status) {
    case "passed":
      return <Badge tone="ok" icon={CheckCircle} label="Passou" />;
    case "failed":
      return <Badge tone="danger" icon={XCircle} label="Falhou" />;
    case "timeout":
      return <Badge tone="caution" icon={TimerOff} label="Tempo esgotado" />;
    default:
      return <Badge tone="danger" icon={XCircle} label="Erro" />;
  }
}

// `tabIndex=0`: a scrollable region is keyboard-reachable only if it can take focus
// (WCAG 2.1.1); the global :focus-visible rule then draws the ring.
function Output({ label, text }: { label: string; text: string }) {
  return (
    <pre
      tabIndex={0}
      aria-label={label}
      className="mt-2 max-h-64 overflow-auto whitespace-pre-wrap rounded-md bg-surface p-3 font-mono text-xs"
    >
      {text}
    </pre>
  );
}

function CommandBlock({ payload }: { payload: Payload }) {
  const runs = asArray<CommandRun>(payload.commands);
  return (
    <div className="mt-2 flex flex-col gap-3">
      {typeof payload.error === "string" && (
        <p role="alert" className="text-danger">
          {payload.error}
        </p>
      )}
      {runs.map((run, index) => (
        <div key={index}>
          <div className="flex flex-wrap items-center gap-3">
            <code className="font-mono text-xs">{run.argv.join(" ")}</code>
            <span className="tabular-nums text-xs text-fg-muted">
              {run.exit_code === null ? "sem exit code" : `exit ${run.exit_code}`}
              {" · "}
              {run.duration_ms} ms
            </span>
          </div>
          {run.output && <Output label={`Saída de ${run.argv.join(" ")}`} text={run.output} />}
        </div>
      ))}
    </div>
  );
}

function DiffBlock({ payload }: { payload: Payload }) {
  if (payload.status !== "ok") {
    return (
      <p role="alert" className="mt-2 text-danger">
        Não foi possível coletar o diff: {String(payload.error ?? "erro desconhecido")}
      </p>
    );
  }
  const files = asArray<FileChange>(payload.files);
  const patch = typeof payload.patch === "string" ? payload.patch : "";
  return (
    <div className="mt-2 flex flex-col gap-3">
      <p className="tabular-nums text-sm">
        {asNumber(payload.files_changed)} arquivos{" "}
        <span className="text-ok">+{asNumber(payload.additions)}</span>{" "}
        <span className="text-danger">−{asNumber(payload.deletions)}</span>
      </p>
      {files.length > 0 && (
        <ul className="flex flex-col gap-1">
          {files.map((file) => (
            <li key={file.path} className="flex flex-wrap items-center gap-3">
              <span className="text-xs text-fg-muted">
                {CHANGE_LABELS[file.change] ?? file.change}
              </span>
              <code className="font-mono text-xs">{file.path}</code>
              {!file.binary && (
                <span className="tabular-nums text-xs text-fg-muted">
                  +{file.additions} −{file.deletions}
                </span>
              )}
            </li>
          ))}
        </ul>
      )}
      {patch && <Output label="Patch" text={patch} />}
      {payload.patch_truncated === true && (
        <p className="text-xs text-fg-muted">
          Patch truncado: a lista de arquivos e as contagens acima são completas.
        </p>
      )}
    </div>
  );
}

function EvidenceBlock({ row }: { row: EvidenceOut }) {
  const payload = row.payload as Payload;
  const title = KIND_LABELS[row.kind] ?? row.kind;
  const headingId = `evidence-${row.kind}`;
  return (
    <section aria-labelledby={headingId} className="border-b border-border-subtle pb-4">
      <div className="flex flex-wrap items-center gap-3">
        <h3 id={headingId} className="text-sm font-semibold">
          {title}
        </h3>
        {row.kind !== "diff" && typeof payload.status === "string" && (
          <StatusBadge status={payload.status} />
        )}
        <ProvenanceBadge kind="verificado" />
      </div>
      {row.kind === "diff" ? <DiffBlock payload={payload} /> : <CommandBlock payload={payload} />}
    </section>
  );
}

/** Task Detail's Evidência tab: what the control plane's own checks found after the agent
 * finished (ADR-026), one block per row exactly as recorded. Every block is "Verificado":
 * nothing here was written by the model. `live` keeps polling while the task can still
 * produce more (it is still running or verifying). */
export function EvidencePanel({ taskPath, live }: { taskPath: string; live: boolean }) {
  const query = useQuery({
    queryKey: ["task-evidence", taskPath],
    queryFn: () => apiFetch<EvidenceListOut>(`/tasks/${taskPath}/evidence`),
    enabled: Boolean(taskPath),
    refetchInterval: live ? 4000 : false,
  });

  if (query.isLoading) {
    return <p>Carregando evidência…</p>;
  }
  if (query.isError) {
    return (
      <div role="alert" className="flex items-center gap-3">
        <p>Falha ao carregar a evidência.</p>
        <button type="button" className="border-border-control" onClick={() => void query.refetch()}>
          Tentar de novo
        </button>
      </div>
    );
  }

  const rows = query.data?.evidence ?? [];
  if (rows.length === 0) {
    return (
      <p className="text-fg-muted">
        Nenhuma evidência ainda. Os checks rodam depois que o agente termina.
      </p>
    );
  }

  return (
    <div className="flex flex-col gap-4">
      {rows.map((row) => (
        <EvidenceBlock key={row.kind} row={row} />
      ))}
    </div>
  );
}
