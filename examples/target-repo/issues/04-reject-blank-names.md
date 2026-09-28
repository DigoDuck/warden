# Reject blank widget names

**Difficulty:** easy

## Context

`POST /widgets` with `{"name": "   ", "price_cents": 100}` succeeds today: it
creates a widget whose name is three spaces. It shows up in `GET /widgets`
as a blank row and nobody can find it by name afterwards.

## Acceptance criteria

- `POST /widgets` with a `name` that is empty or made only of whitespace
  (after stripping leading/trailing spaces) returns `422`, not `201`.
- A name with meaningful content and incidental surrounding whitespace, e.g.
  `"  bolt  "`, is accepted, and the widget is stored with the surrounding
  whitespace stripped (stored name: `"bolt"`).
- A one-character name like `"a"` still works, exactly as it does today —
  don't raise the minimum length.

## Out of scope

- Trimming or validating anything other than `name`.
- Rejecting names with internal whitespace (`"steel bolt"` is fine).
- Anything outside `src/` and `tests/`.
