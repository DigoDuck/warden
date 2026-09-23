// Mirrors warden/models.py's TASK_STATUSES. The database CHECK constraint is the single
// source of truth for what a task's status can be; this list only drives the frontend's
// status filter <select> and StatusBadge's lookup table.
export const TASK_STATUSES = [
  "QUEUED",
  "RUNNING",
  "WAITING_APPROVAL",
  "SUCCEEDED",
  "FAILED",
  "CANCELLED",
  "TIMED_OUT",
  "BUDGET_EXCEEDED",
] as const;
