# Reject duplicate widget names

**Difficulty:** medium

## Context

Nothing stops two widgets from being created with the same name today, and
the catalog is meant to be looked up by name downstream. Two "bolt" rows
with different ids and prices is already causing confusion in support
tickets.

## Acceptance criteria

- `POST /widgets` with a `name` that already belongs to an existing widget
  returns `409 Conflict` instead of creating a second one. The comparison is
  case-insensitive: `"Bolt"` conflicts with an existing `"bolt"`.
- The error body has a `detail` key describing the conflict (a plain
  string is enough — issue 06 defines the exact shape used everywhere else).
- Creating a widget with a name that is not currently in use still returns
  `201`, unchanged from today.
- Renaming a widget with `PATCH` (issue 02) to a name that collides with
  another widget's name should also be rejected the same way — but only if
  your solution to issue 02 already exists; don't invent a `PATCH` endpoint
  just for this. If `PATCH` doesn't exist yet, only `POST` needs to enforce
  this.

## Out of scope

- Uniqueness on anything other than `name` (ids are already unique by
  construction).
- Anything outside `src/` and `tests/`.
