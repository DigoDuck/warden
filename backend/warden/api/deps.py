"""FastAPI dependencies: one DB session per request, and bearer-token auth.

api's whole authorization job (briefing §10) is deciding who is calling and which scope
they carry, then handing the route a `Claims` object `identity.verify()` already produced.
It never decides *what* a caller may do beyond that scope check, that stays in `policy`.
"""

from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Annotated

from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from warden import identity
from warden.identity import Claims, InsufficientScope, InvalidToken, KeyPair

# auto_error=False: HTTPBearer's own default raises 403 on a missing header, and the
# briefing/spec here want 401 (this is authentication, not authorization) with a
# WWW-Authenticate header. Handled by hand below instead.
_bearer = HTTPBearer(auto_error=False)

_UNAUTHENTICATED_HEADERS = {"WWW-Authenticate": "Bearer"}


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    """One session per request, from the factory `create_app` was given.

    Never a session shared across requests: a handler that raises mid-way must not leave a
    half-written transaction for the next request on the same worker to inherit.
    """
    async with request.app.state.session_factory() as session:
        yield session


def get_keys(request: Request) -> KeyPair:
    key_pair: KeyPair = request.app.state.keys
    return key_pair


SessionDep = Annotated[AsyncSession, Depends(get_session)]
KeysDep = Annotated[KeyPair, Depends(get_keys)]


def require_scope(scope: str) -> Callable[..., Awaitable[Claims]]:
    """Build a dependency that authenticates the bearer token and demands `scope`.

    Only `typ == "user"` tokens are accepted: an agent token is minted for one run of one
    task (`identity/jwt.py`), never for a person driving this API. Rejected as 403, not 401:
    the signature and claims already checked out (this is a live, valid credential), it is
    just the wrong *kind* of credential for this audience, the same authorization-not-
    authentication distinction `identity.verify()` itself draws for a missing scope. See
    ADR-020 for the alternative considered and why 401 was rejected.
    """

    async def dependency(
        session: SessionDep,
        keys: KeysDep,
        credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
    ) -> Claims:
        if credentials is None:
            raise HTTPException(401, "missing bearer token", headers=_UNAUTHENTICATED_HEADERS)
        try:
            claims = await identity.verify(
                session, keys, credentials.credentials, required_scope=scope
            )
        except InvalidToken as exc:
            # Detail is static, never the exception's own message: identity.verify()'s
            # messages are meant for logs, not response bodies, and never include the token.
            raise HTTPException(
                401, "invalid or expired token", headers=_UNAUTHENTICATED_HEADERS
            ) from exc
        except InsufficientScope as exc:
            raise HTTPException(403, "token lacks required scope") from exc
        if claims.typ != "user":
            raise HTTPException(403, "only user tokens are accepted here")
        return claims

    return dependency
