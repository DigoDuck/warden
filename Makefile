.PHONY: db-up db-down migrate revision test lint fmt

# --directory avoids "cd backend &&", which breaks when the Windows make picks
# cmd.exe instead of sh. Each recipe stays a single command.
UV := uv run --directory backend

db-up:
	docker compose up -d db

db-down:
	docker compose down

migrate:
	$(UV) alembic upgrade head

revision:
	$(UV) alembic revision --autogenerate -m "$(m)"

test:
	$(UV) pytest

lint:
	$(UV) ruff check
	$(UV) ruff format --check
	$(UV) mypy

fmt:
	$(UV) ruff format
	$(UV) ruff check --fix
