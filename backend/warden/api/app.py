"""The app factory (briefing §14).

`session_factory` and `keys` are injected rather than built here so tests can pass an
ephemeral `KeyPair` and the test database's session factory (see tests/test_api.py), the
same way tests/test_identity.py never calls `identity.load_keys()`. Production wires the
real ones in `warden.api.main`, which is what `make api` runs.
"""

from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from warden.api.routes_audit import router as audit_router
from warden.api.routes_tasks import router as tasks_router
from warden.identity import KeyPair


def create_app(*, session_factory: async_sessionmaker[AsyncSession], keys: KeyPair) -> FastAPI:
    app = FastAPI(title="Warden API")
    app.state.session_factory = session_factory
    app.state.keys = keys

    app.include_router(tasks_router)
    app.include_router(audit_router)

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    return app
