# Build context is the repository root (the OpenAPI types need the backend).

# 1. The frontend's generated types come from the backend's live OpenAPI schema
#    (frontend/scripts/gen-api.mjs does this with the host's uv; here there is no host).
FROM python:3.13-slim AS openapi
COPY --from=ghcr.io/astral-sh/uv:0.9.5 /uv /usr/local/bin/uv
WORKDIR /app/backend
COPY backend/pyproject.toml backend/uv.lock ./
RUN uv sync --locked --no-dev --no-install-project
COPY backend/ ./
RUN uv sync --locked --no-dev && uv run --no-sync python -m warden.api.openapi_export > /openapi.json

# 2. Types + static build. `vite build` directly: `npm run build` would re-run gen:api (uv).
FROM node:22-alpine AS build
WORKDIR /app/frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
COPY --from=openapi /openapi.json /tmp/openapi.json
RUN npx openapi-typescript /tmp/openapi.json -o src/api/schema.d.ts && npx vite build

# 3. Static files + reverse proxy for /api (same job as the Vite dev proxy).
FROM caddy:2-alpine
COPY frontend/Caddyfile /etc/caddy/Caddyfile
COPY --from=build /app/frontend/dist /srv
