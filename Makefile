.PHONY: db-up db-down migrate revision test lint fmt

# --directory evita "cd backend &&", que quebra quando o make do Windows escolhe
# cmd.exe em vez de sh. Cada receita vira um comando so.
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
