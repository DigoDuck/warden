// Thin, readable aliases over the generated openapi-typescript shapes (src/api/schema.d.ts).
// Pages import from here instead of reaching into `components["schemas"][...]` everywhere.
import type { components } from "./schema";

export type TaskCreate = components["schemas"]["TaskCreate"];
export type TaskOut = components["schemas"]["TaskOut"];
export type TaskListItemOut = components["schemas"]["TaskListItemOut"];
export type TaskListOut = components["schemas"]["TaskListOut"];
export type TaskEventOut = components["schemas"]["TaskEventOut"];
export type TaskEventPage = components["schemas"]["TaskEventPage"];
export type ApprovalOut = components["schemas"]["ApprovalOut"];
export type ApprovalDecisionIn = components["schemas"]["ApprovalDecisionIn"];
export type ValidationError = components["schemas"]["ValidationError"];
export type HTTPValidationError = components["schemas"]["HTTPValidationError"];
