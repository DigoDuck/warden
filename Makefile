.PHONY: db-up db-down migrate revision test lint fmt sandbox-image demo-fake demo worker keys api user-token

# --directory avoids "cd backend &&", which breaks when the Windows make picks
# cmd.exe instead of sh. Each recipe stays a single command.
UV := uv run --directory backend

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

lint:
	$(UV) ruff check
	$(UV) ruff format --check
	$(UV) mypy

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
api:
	$(UV) uvicorn warden.api.main:app --reload

# Mints a user JWT for manual testing, e.g.:
#   TOKEN=$(make user-token email=you@example.com scopes="tasks:write tasks:read audit:read")
user-token:
	$(UV) python -m warden.api.user_token --email "$(email)" --scopes $(scopes)
