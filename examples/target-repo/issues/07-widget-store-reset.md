# Give tests a real reset instead of a shared global

**Difficulty:** hard

## Context

`_WIDGETS` and `_NEXT_ID` are two loose module-level globals. That's fine for
a four-line demo, but it has a real cost now that the service is growing: the
in-memory state is created once, at import time, and lives for the lifetime
of the Python process. Any code that creates or deletes a widget — a test, a
script, a future integration test — permanently mutates that shared state for
everything else that imports `src.app` in the same process. There is no
supported way to get back to a clean slate without restarting the process.

This issue asks for two things that go together: a small `WidgetStore` class
that owns the widgets and the id counter as its own state (instead of two
module-level names mutated with `global`), and a way to reset that state
through the API so tests (and anyone else) can start clean on demand.

## Acceptance criteria

- `POST /widgets/reset` restores the store to its exact starting state: the
  two seed widgets (`id=1, name="bolt", price_cents=250` and
  `id=2, name="washer", price_cents=80`), nothing else, and the next created
  widget gets `id=3` again — regardless of how many widgets were created or
  deleted before the reset. Returns `204 No Content`.
- After calling reset, `GET /widgets` returns exactly the two seed widgets,
  in their original state, even if one of them was patched or deleted
  beforehand.
- The widget storage (the dict of widgets and the next-id counter) is
  encapsulated in a class with its own methods (e.g. `list`, `get`, `create`,
  `reset`) rather than read and written through `global` statements scattered
  across the endpoint functions. How the endpoints reach the single shared
  instance (a module-level instance of the class, `app.state`, a dependency)
  is your call — the acceptance criteria are the observable behaviour above,
  not the wiring.
- All five original tests in `tests/test_app.py` keep passing.

## Out of scope

- Persisting anything to disk or a real database — still in-memory, on
  purpose (see the module docstring).
- Making the store thread-safe against concurrent requests — out of scope
  for this issue, even though a real service would eventually need it.
- Anything outside `src/` and `tests/`.
