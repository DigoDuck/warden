# Partially update a widget

**Difficulty:** medium

## Context

Fixing a typo in a widget's name, or repricing it, currently means deleting
it and creating it again under a new id, which breaks anything that held a
reference to the old id. Callers need to update a widget in place.

## Acceptance criteria

- `PATCH /widgets/{widget_id}` accepts a JSON body with `name`,
  `price_cents`, or both, all optional.
- Only the fields present in the body change; a field left out keeps its
  current value. `PATCH` with `{}` is a no-op that still returns `200` and
  the unchanged widget.
- The response is `200` with the full, updated widget (same shape as
  `GET /widgets/{widget_id}`), and the widget's `id` never changes.
- The same validation as creation applies: `name` non-empty (1–64 chars),
  `price_cents >= 0`. An invalid value in either field is `422` and leaves
  the stored widget untouched.
- Patching an id that doesn't exist returns `404`, same shape as
  `GET /widgets/{widget_id}` for a missing widget.

## Out of scope

- `PUT` (full replace) — this issue is `PATCH` only.
- Changing more fields than `name` and `price_cents` (there are no others).
- Anything outside `src/` and `tests/`.
