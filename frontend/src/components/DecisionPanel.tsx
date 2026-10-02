import { CheckCircle, XCircle } from "lucide-react";
import type { TaskOut } from "../api/types";
import { Badge } from "./Badge";
import { ProvenanceBadge } from "./ProvenanceBadge";
import { StatusBadge } from "./StatusBadge";
import { TERMINAL_STATUSES } from "../lib/taskStatuses";

function Verdict({ task }: { task: TaskOut }) {
  const verdict = task.verdict;
  if (!verdict) {
    return (
      <p className="text-fg-muted">
        {TERMINAL_STATUSES.has(task.status)
          ? "Esta tarefa terminou sem revisão independente."
          : "Ainda sem veredito."}
      </p>
    );
  }
  if (verdict.malformed_reason) {
    // A malformed answer is recorded as not passed, and never shown as an approval.
    return (
      <p role="alert" className="text-danger">
        Veredito malformado: {verdict.malformed_reason}
      </p>
    );
  }
  return (
    <div className="flex flex-col gap-2">
      {verdict.passed ? (
        <Badge tone="ok" icon={CheckCircle} label="Aprovado" />
      ) : (
        <Badge tone="danger" icon={XCircle} label="Reprovado" />
      )}
      {verdict.findings.length > 0 && (
        <ul className="list-disc pl-5">
          {verdict.findings.map((finding, index) => (
            <li key={index}>{finding}</li>
          ))}
        </ul>
      )}
    </div>
  );
}

/** Task Detail's Decisão tab (ADR-010): what the coder said, what the independent reviewer
 * said, and what the control plane decided, kept apart. The first two are model output and
 * both carry "Gerado"; only the third is the control plane's own (the status chip), computed
 * from the evidence and the verdict, never from the coder's summary. */
export function DecisionPanel({ task }: { task: TaskOut }) {
  const reviewerApprovedButFailed = task.status === "FAILED" && task.verdict?.passed === true;
  return (
    <div className="flex flex-col gap-6">
      <div className="grid gap-6 md:grid-cols-2">
        <section aria-labelledby="decision-summary" className="flex flex-col gap-2">
          <div className="flex flex-wrap items-center gap-3">
            <h3 id="decision-summary" className="text-sm font-semibold">
              Relato do agente
            </h3>
            <ProvenanceBadge kind="gerado" />
          </div>
          <p className="whitespace-pre-wrap">{task.summary ?? "Ainda sem relato."}</p>
        </section>

        <section aria-labelledby="decision-verdict" className="flex flex-col gap-2">
          <div className="flex flex-wrap items-center gap-3">
            <h3 id="decision-verdict" className="text-sm font-semibold">
              Veredito do revisor
            </h3>
            <ProvenanceBadge kind="gerado" detail="revisor independente" />
          </div>
          <Verdict task={task} />
        </section>
      </div>

      <section aria-labelledby="decision-final" className="flex flex-col gap-2">
        <div className="flex flex-wrap items-center gap-3">
          <h3 id="decision-final" className="text-sm font-semibold">
            Decisão do control plane
          </h3>
          <StatusBadge status={task.status} />
        </div>
        <p className="text-sm text-fg-muted">
          O status sai dos checks determinísticos e do veredito, nunca do relato do agente.
        </p>
        {reviewerApprovedButFailed && (
          <p className="text-sm">
            O revisor aprovou, mas ao menos um check falhou: o modelo não sobrepõe um check
            vermelho. Veja a aba Evidência.
          </p>
        )}
      </section>
    </div>
  );
}
