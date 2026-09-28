# Delete a widget

**Difficulty:** easy

## Context

Widgets can be listed, fetched and created, but once one exists there is no
way to get rid of it short of restarting the process. Support keeps asking
for a way to remove a widget that was created by mistake.

## Acceptance criteria

- `DELETE /widgets/{widget_id}` removes the widget with that id and returns
  `204 No Content` with an empty body.
- Deleting an id that doesn't exist returns `404` with the same error shape
  the service already uses for a missing widget on `GET /widgets/{widget_id}`.
- After a successful delete, that id no longer appears in `GET /widgets` and
  `GET /widgets/{widget_id}` for it returns `404`.
- Deleting the same id twice: the first call is `204`, the second is `404`
  (it's already gone).

## Out of scope

- Soft-delete, undo, or an audit trail of what was deleted.
- Bulk delete.
- Anything outside `src/` and `tests/`.
