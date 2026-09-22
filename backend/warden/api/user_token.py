"""`make user-token email=<email> scopes="tasks:write tasks:read audit:read"`.

Mints a user JWT for manual API testing. `POST /auth/login` is week 4, alongside the
frontend that would actually call it (ADR-020); until then this is how a bearer token gets
into a `curl` header at all.

Prints ONLY the token to stdout, nothing else, on purpose: it never logs it, so
`TOKEN=$(make user-token ...)` captures exactly the token and no log noise.
"""

import argparse
import asyncio

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from warden import identity
from warden.config import get_settings
from warden.db import make_engine, make_session_factory
from warden.models import User

# Short enough to limit what a leaked dev token is worth, long enough that a manual test
# session does not expire mid-curl. identity.USER_TTL_CAP_SECONDS (one day) is the ceiling
# this still has to respect, not the default.
DEFAULT_TTL_SECONDS = 3600


async def _get_or_create_user(session: AsyncSession, email: str) -> User:
    user = await session.scalar(select(User).where(User.email == email))
    if user is not None:
        return user
    # "!" can never match a real password hash (same convention as Django's
    # set_unusable_password()): this account has no password, only tokens minted here,
    # because there is no /auth/login yet.
    user = User(email=email, password_hash="!", role="user")
    session.add(user)
    await session.flush()
    return user


async def _issue(email: str, scopes: list[str], ttl_seconds: int) -> str:
    settings = get_settings()
    keys = identity.load_keys(settings)
    session_factory = make_session_factory(make_engine(settings.database_url))
    async with session_factory() as session:
        user = await _get_or_create_user(session, email)
        token = await identity.issue_user_token(
            session, keys, user, scopes=scopes, ttl_seconds=ttl_seconds
        )
        await session.commit()
    return token


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--email", required=True)
    parser.add_argument("--scopes", nargs="+", required=True)
    parser.add_argument("--ttl-seconds", type=int, default=DEFAULT_TTL_SECONDS)
    args = parser.parse_args()

    token = asyncio.run(_issue(args.email, args.scopes, args.ttl_seconds))
    print(token)  # the only line this ever prints
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
