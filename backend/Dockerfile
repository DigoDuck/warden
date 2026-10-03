# One image for api, worker, migrate and keys. Build context is the REPOSITORY ROOT, not
# backend/: worker.py and identity/jwt.py find examples/ and policies/ via
# Path(__file__).parents[3], so the image must keep the repo layout
# (/app/backend/warden, /app/examples, /app/policies).
FROM python:3.13-slim

COPY --from=ghcr.io/astral-sh/uv:0.9.5 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy PATH="/app/backend/.venv/bin:$PATH"

WORKDIR /app/backend

# Dependencies first: this layer only rebuilds when the lockfile changes.
COPY backend/pyproject.toml backend/uv.lock ./
RUN uv sync --locked --no-dev --no-install-project

COPY backend/ ./
RUN uv sync --locked --no-dev

COPY examples/ /app/examples/
COPY policies/ /app/policies/
# A build context from a Windows host arrives with every file marked executable, and ruff's
# EXE002 (executable without shebang) then fails the verifier's lint check on the target repo
# inside the sandbox. Git tracks no executable file under examples/ (mode 100755 count: 0),
# so normalising to 0644 restores what the repository actually says.
RUN find /app/examples /app/policies -type f -exec chmod 644 {} +
