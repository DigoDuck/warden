"""`make user-token email=<email> scopes="tasks:write tasks:read audit:read" [role=worker]`.

Mints a user JWT for manual API testing. `POST /auth/login` is week 4, alongside the
frontend that would actually call it (ADR-020); until then this is how a bearer token gets
into a `curl` header at all.

Prints ONLY the token to stdout, nothing else, on purpose: it never logs it, so
`TOKEN=$(make user-token ...)` captures exactly the token and no log noise.
"""

import argparse
import asyncio
import sys

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

# The roles this tool may assign. Scopes decide what a user may ask the API for (ADR-020); the
# role decides what an agent may do on that user's behalf, because the policy judges every call
# against the submitter's role (`user.role` in policies/default.yaml, ADR-030). `worker` is the
# one that may have code changed (`write-source`); `user` may submit tasks that only read.
ROLES = ("user", "worker")


class RoleMismatch(Exception):
    """The email belongs to a user whose role differs from the one asked for."""


async def get_or_create_user(session: AsyncSession, email: str, role: str | None) -> User:
    """`role=None` means "whatever this user already is" (`user` for a new one).

    An existing user's role is never changed here. Promoting `user` to `worker` widens what
    every future task of that user may write, and a tool whose job is to mint a token must not
    do that as a side effect of a typo'd email or a reused one. Use another email instead.
    """
    user = await session.scalar(select(User).where(User.email == email))
    if user is not None:
        if role is not None and user.role != role:
            raise RoleMismatch(
                f"{email} already exists with role {user.role!r}; this tool never changes a "
                f"role. Mint the token without role=, or use another email for a {role!r} user."
            )
        return user
    # "!" can never match a real password hash (same convention as Django's
    # set_unusable_password()): this account has no password, only tokens minted here,
    # because there is no /auth/login yet.
    user = User(email=email, password_hash="!", role=role or "user")
    session.add(user)
    await session.flush()
    return user


async def _issue(email: str, scopes: list[str], ttl_seconds: int, role: str | None) -> str:
    settings = get_settings()
    keys = identity.load_keys(settings)
    session_factory = make_session_factory(make_engine(settings.database_url))
    async with session_factory() as session:
        user = await get_or_create_user(session, email, role)
        token = await identity.issue_user_token(
            session, keys, user, scopes=scopes, ttl_seconds=ttl_seconds
        )
        await session.commit()
    return token


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--email", required=True)
    parser.add_argument("--scopes", nargs="+", required=True)
    parser.add_argument("--ttl-seconds", type=int, default=DEFAULT_TTL_SECONDS)
    parser.add_argument("--role", choices=ROLES, default=None)
    return parser.parse_args(argv)


def main() -> int:
    args = parse_args()

    try:
        token = asyncio.run(_issue(args.email, args.scopes, args.ttl_seconds, args.role))
    except RoleMismatch as exc:
        # stderr, never stdout: `TOKEN=$(make user-token ...)` must capture a token or nothing.
        print(f"user-token: {exc}", file=sys.stderr)
        return 1
    print(token)  # the only line this ever prints
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
