# Widget Service

A small FastAPI service. It exists to be worked on by Warden's agents: read, patched and
turned into pull requests. Keeping it small is the point, so that a capability eval can
assert on its whole behaviour.

## Layout

```
src/app.py        four endpoints over an in-memory store
tests/test_app.py five tests
```

## Running the tests

```bash
uv sync
uv run pytest
```
