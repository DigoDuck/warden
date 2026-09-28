# `/widgets/search` ignores case sensibly, except it doesn't

**Difficulty:** easy

## Context

`GET /widgets/search?q=...` is meant to help a user find a widget by name
without remembering its exact capitalization. Right now it does a plain
substring match, so `q=BOLT` finds nothing even though a widget literally
named `bolt` exists.

## Acceptance criteria

- `GET /widgets/search?q=<text>` returns every widget whose name contains
  `<text>` as a substring, ignoring case in both directions: `q=BOLT`,
  `q=bolt` and `q=Bolt` all match a widget named `bolt`, and a widget named
  `Steel Bolt` matches `q=bolt` too.
- `q=` (empty string, or the parameter omitted) still returns every widget,
  unchanged from today.
- A query that matches nothing returns an empty list with `200`, not an
  error.

## Out of scope

- Fuzzy matching, ranking, or matching on anything other than `name`.
- Anything outside `src/` and `tests/`.
