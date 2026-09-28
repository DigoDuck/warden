# `/widgets/stats` doesn't report an average

**Difficulty:** easy

## Context

`GET /widgets/stats` is supposed to give a quick read of the catalog's
pricing: `{"average_price_cents": <mean price across all widgets>}`. The
numbers it returns don't look like an average once there's more than one
widget in the store — they look like they scale with how many widgets exist,
not with how expensive they are.

## Acceptance criteria

- `GET /widgets/stats` returns `{"average_price_cents": <mean>}`, where
  `<mean>` is the arithmetic mean of `price_cents` across every widget
  currently in the store (sum divided by count).
- With the two seed widgets only (`250` and `80` cents), the endpoint
  returns `165`.
- After creating a third widget at `300` cents, it returns `210`
  (`(250 + 80 + 300) / 3`).
- The response stays valid JSON with the same key; don't rename
  `average_price_cents` or change the endpoint path.

## Out of scope

- Deciding what the endpoint should return with zero widgets in the store —
  the store always ships with two seed widgets and nothing in this repo
  empties it, so that case doesn't need handling here.
- Any other statistic (median, min, max).
- Anything outside `src/` and `tests/`.
