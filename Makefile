.PHONY: db-up db-down migrate revision test lint fmt sandbox-image demo-fake demo worker keys api user-token frontend-install frontend-dev frontend-test evals-behavioral

# --directory avoids "cd backend &&", which breaks when the Windows make picks
# cmd.exe instead of sh. Each recipe stays a single command.
UV := uv run --directory backend
# --project, not --directory: evals/ lives at the repo root, a sibling of backend/, not
# inside it (see evals/__init__.py for why). --project points uv at backend's locked
# dependencies without changing the working directory, so `evals/datasets/*.yaml` and the
# package's own relative imports resolve from the repo root the ordinary way.
UV_ROOT := uv run --project backend
# --prefix, same reason: no "cd frontend &&" to break under cmd.exe.
NPM := npm --prefix frontend

db-up:
	docker compose up -d db

db-down:
	docker compose down

migrate:
	$(UV) alembic upgrade head

# Generates .keys/jwt-private.pem at the repo root (see warden/config.py). Refuses to
# overwrite an existing key. The public key and kid are derived from it, never stored separately.
keys:
	$(UV) python -m warden.identity.generate_keys

revision:
	$(UV) alembic revision --autogenerate -m "$(m)"

# Tools run inside this image (warden/sandbox/docker.py), and it has no network to pull
# from, so it has to exist and be current before anything that exercises a sandbox runs.
sandbox-image:
	docker build -t warden-sandbox:dev sandbox-images/python

test: sandbox-image
	$(UV) pytest

# evals/ sits outside backend/, so backend's own ruff/mypy roots never see it. Listed by
# file: evals/datasets/ holds fixture repos (target_repo) that are data, not our code.
EVALS_PY := evals/__init__.py evals/checks.py evals/runner.py evals/tests

lint:
	$(UV) ruff check
	$(UV) ruff format --check
	$(UV) mypy
	$(UV_ROOT) ruff check $(EVALS_PY)
	$(UV_ROOT) ruff format --check $(EVALS_PY)
	$(UV_ROOT) mypy --config-file backend/pyproject.toml $(EVALS_PY)

fmt:
	$(UV) ruff format
	$(UV) ruff check --fix

# Replays a script: no API key, no network, no cost.
demo-fake: sandbox-image
	$(UV) python -m warden.demo --provider fake

# Calls the real API. Needs ANTHROPIC_API_KEY in .env, and spends money.
demo: sandbox-image
	$(UV) python -m warden.demo --provider anthropic

# Claims queued tasks and runs them until stopped. Several may run at once.
worker: sandbox-image
	$(UV) python -m warden.core.worker

# The HTTP surface (briefing §14). Needs `make keys` done once first: it loads the signing
# key at startup and refuses to serve without one.
# WARDEN_API_PORT moves the API off 8000 when another project holds it; the Vite dev proxy
# reads the same variable (frontend/vite.config.ts).
WARDEN_API_PORT ?= 8000

api:
	$(UV) uvicorn warden.api.main:app --reload --port $(WARDEN_API_PORT)

# Mints a user JWT for manual testing, e.g.:
#   TOKEN=$(make user-token email=you@example.com scopes="tasks:write tasks:read audit:read")
# The leading @ matters: without it make echoes the recipe line to stdout and $(...) captures
# that line along with the token.
user-token:
	@$(UV) python -m warden.api.user_token --email "$(email)" --scopes $(scopes)

# `npm ci` matches CI/package-lock.json exactly, unlike `npm install`.
frontend-install:
	$(NPM) ci

# gen:api (npm's predev hook) needs the backend's dependencies installed and importable:
# run `uv sync --directory backend` first if this fails.
frontend-dev:
	$(NPM) run dev

frontend-test:
	$(NPM) test -- --run

# Zero cost: FakeProvider only, no API key needed (briefing §19). Runs the runner's own
# fast unit tests first, then the 12-case dataset for real against a scratch database
# (WARDEN_TEST_DB, default "warden_evals", never the backend suite's "warden_test") and the sandbox image, and fails the target if any
# non-pending case fails. --write-metrics regenerates docs/metrics.md's behavioral table.
evals-behavioral: sandbox-image
	$(UV_ROOT) pytest evals/tests -q
	$(UV_ROOT) python -m evals.runner evals/datasets/behavioral_v1.yaml --write-metrics
