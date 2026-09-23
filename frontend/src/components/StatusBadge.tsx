import { Clock } from "lucide-react";

/** The only place that turns a task status (warden/models.py TASK_STATUSES) into color,
 * icon and text (DESIGN.md's status table). */
export function StatusBadge({ status }: { status: string }) {
  // TODO(skeleton): renders only an icon with an aria-label, no visible text. Filled in
  // next commit once the red proves DESIGN.md's "cor nunca sozinha" rule is being checked.
  return <Clock size={16} aria-label={status} />;
}
