"""Behavioral and capability evals for Warden.

Lives at the repo root, a sibling of `backend/`, not inside `backend/warden`: an eval
drives the control plane the same way an external caller would (enqueue a task, run a
worker, read the database back), so it has no business reaching into `warden`'s internals
any more than a test in `backend/tests` already does by importing the package. Keeping it
out of the `warden` distribution also means a capability eval's dataset (repo fixtures,
hidden tests) never ships in the wheel `hatchling` builds for the actual product.

Importable from the backend environment because `warden` (and pytest, pyyaml, sqlalchemy)
are installed there: run it with `uv run --project backend python -m evals.runner ...` from
the repository root, which points uv at backend's locked dependencies without changing the
working directory, so this package's own relative imports and `evals/datasets/*.yaml`
resolve the ordinary way.
"""
