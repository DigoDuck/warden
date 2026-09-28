# One error shape for every 4xx

**Difficulty:** medium

## Context

A missing widget currently answers with `{"detail": "widget not found"}` —
`detail` is a plain string. A bad request body (say, a negative
`price_cents`) answers with `{"detail": [{"type": "greater_than_equal", ...}, ...]}`
— `detail` is a list of structured error objects, FastAPI's default shape
for a `RequestValidationError`. Every client integrating against this
service has to branch on which shape it got back before it can show an error
message, which defeats the point of a single `detail` key.

## Acceptance criteria

- Every error response this service returns (404s, 409s, 422s) has the
  shape `{"detail": "<human-readable string>"}` — `detail` is always a
  string, never a list or nested object.
- For a validation error (currently a `RequestValidationError` with a list
  of field errors), the string names the offending field(s), e.g.
  `"price_cents: input should be greater than or equal to 0"`. It doesn't
  need to reproduce FastAPI's exact wording, just be a plain string that
  mentions the field.
- The status codes already in use (404, 409, 422) don't change — only the
  body shape of the ones that were a list becomes a string.
- All five original tests in `tests/test_app.py` keep passing unmodified
  (none of them inspect the body of a 422, only the status code).

## Out of scope

- Changing status codes.
- A generic "problem details" (RFC 7807) format — a flat `{"detail": str}`
  is all that's asked for here.
- Anything outside `src/` and `tests/`.
