# Widget Service

A small FastAPI service. It exists to be worked on by Warden's agents: read, patched and
turned into pull requests. Keeping it small is the point, so that a capability eval can
assert on its whole behaviour.

## Layout

```
src/app.py        six endpoints over an in-memory store
tests/test_app.py five tests
issues/           ten issues written as specs, for an agent to pick up
```

## Running the tests

```bash
uv sync
uv run pytest
```

## CI

`.github/workflows/ci.yml` runs `uv sync`, `ruff check`, `ruff format --check`
and `pytest` on every push and pull request — including a pull request opened
by an agent working one of the issues in `issues/`.
