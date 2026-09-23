import { Ban, CheckCircle, Clock, Hand, Loader, TimerOff, Wallet, XCircle } from "lucide-react";
import { Badge, type BadgeTone } from "./Badge";

interface StatusEntry {
  tone: BadgeTone;
  icon: typeof Clock;
  label: string;
}

// warden/models.py's TASK_STATUSES, DESIGN.md's status table. RUNNING uses a static
// Loader icon, never a spinning one: week 4 forbids animation entirely.
// Exported (not module-private) so the task list's status filter can reuse the exact same
// Portuguese labels instead of a second, driftable copy of this table.
export const TASK_STATUS_ENTRIES: Record<string, StatusEntry> = {
  QUEUED: { tone: "muted", icon: Clock, label: "Na fila" },
  RUNNING: { tone: "info", icon: Loader, label: "Executando" },
  WAITING_APPROVAL: { tone: "warn", icon: Hand, label: "Aguardando aprovação" },
  SUCCEEDED: { tone: "ok", icon: CheckCircle, label: "Concluída" },
  FAILED: { tone: "danger", icon: XCircle, label: "Falhou" },
  CANCELLED: { tone: "muted", icon: Ban, label: "Cancelada" },
  TIMED_OUT: { tone: "caution", icon: TimerOff, label: "Tempo esgotado" },
  BUDGET_EXCEEDED: { tone: "caution", icon: Wallet, label: "Orçamento excedido" },
};

/** The only place that turns a task status into color, icon and text (DESIGN.md's status
 * table). An unrecognised value still renders its own text instead of throwing: a status
 * shown as raw text is a smaller problem than a page that will not render at all. */
export function StatusBadge({ status }: { status: string }) {
  const entry = TASK_STATUS_ENTRIES[status] ?? { tone: "muted" as const, icon: Clock, label: status };
  return <Badge tone={entry.tone} icon={entry.icon} label={entry.label} />;
}
