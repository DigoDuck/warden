// Mirrors warden/models.py's TASK_STATUSES. The database CHECK constraint is the single
// source of truth for what a task's status can be; this list only drives the frontend's
// status filter <select> and StatusBadge's lookup table.
export const TASK_STATUSES = [
  "QUEUED",
  "RUNNING",
  "VERIFYING",
  "WAITING_APPROVAL",
  "SUCCEEDED",
  "FAILED",
  "CANCELLED",
  "TIMED_OUT",
  "BUDGET_EXCEEDED",
] as const;

// Statuses a task never leaves (core/worker.py::TERMINAL_STATUSES): the worker never picks
// such a task up again, so polling stops, cancelling is meaningless (the API answers 409)
// and a missing verdict will never arrive.
export const TERMINAL_STATUSES: ReadonlySet<string> = new Set([
  "SUCCEEDED",
  "FAILED",
  "CANCELLED",
  "TIMED_OUT",
  "BUDGET_EXCEEDED",
]);
