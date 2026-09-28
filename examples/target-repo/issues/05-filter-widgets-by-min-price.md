# Filter widgets by minimum price

**Difficulty:** medium

## Context

The catalog is starting to have enough widgets that "show me everything
worth at least X" is a common ask, and today the only option is to fetch
everything and filter client-side.

## Acceptance criteria

- `GET /widgets` accepts an optional `min_price` query parameter (an
  integer, in cents). When present, only widgets with
  `price_cents >= min_price` are returned.
- `GET /widgets` with no `min_price` behaves exactly as it does today (all
  widgets, unfiltered) — existing callers must not see any change.
- `min_price=0` returns every widget (nothing has a negative price).
- A negative `min_price` (e.g. `min_price=-1`) is a client error: `422`, not
  an empty or unfiltered list.
- A `min_price` that isn't a valid integer (e.g. `min_price=abc`) is `422`,
  which is already FastAPI's default behaviour for a mistyped query
  parameter — just don't break it while adding the new logic.

## Out of scope

- Any other filter (`max_price`, `name`, pagination). One query parameter,
  one behaviour.
- Anything outside `src/` and `tests/`.
