"""`make user-token`: the role a minted user gets, and the role it never silently changes.

The policy reads the submitter's role (`user.role` in `policies/default.yaml`; ADR-030), so the
role this dev tool assigns decides what an agent may do on that user's behalf. Two promises:
a new user gets the role asked for, and an existing user's role is never rewritten by a tool
whose job is to mint a token.
"""

import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from warden.api import user_token
from warden.models import User


def _email() -> str:
    return f"{uuid.uuid4()}@warden.test"


async def _role(session_factory: async_sessionmaker[AsyncSession], email: str) -> str:
    async with session_factory() as session:
        role = await session.scalar(select(User.role).where(User.email == email))
        assert role is not None
        return role


async def test_a_new_user_gets_the_role_it_asked_for(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    email = _email()
    async with session_factory() as session:
        await user_token.get_or_create_user(session, email, role="worker")
        await session.commit()
    assert await _role(session_factory, email) == "worker"


async def test_without_a_role_a_new_user_is_a_plain_user(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    email = _email()
    async with session_factory() as session:
        await user_token.get_or_create_user(session, email, role=None)
        await session.commit()
    assert await _role(session_factory, email) == "user"


async def test_without_a_role_an_existing_user_keeps_its_own(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    email = _email()
    async with session_factory() as session:
        await user_token.get_or_create_user(session, email, role="worker")
        await session.commit()
    async with session_factory() as session:
        user = await user_token.get_or_create_user(session, email, role=None)
        await session.commit()
    assert user.role == "worker"


async def test_an_existing_users_role_is_never_rewritten(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    # A token tool that quietly promoted `user` to `worker` would be a privilege change
    # nobody asked for by name. It refuses and says how to get a worker instead.
    email = _email()
    async with session_factory() as session:
        await user_token.get_or_create_user(session, email, role=None)
        await session.commit()
    async with session_factory() as session:
        with pytest.raises(user_token.RoleMismatch, match="already exists with role 'user'"):
            await user_token.get_or_create_user(session, email, role="worker")
    assert await _role(session_factory, email) == "user"


def test_only_known_roles_are_accepted() -> None:
    with pytest.raises(SystemExit):
        user_token.parse_args(
            ["--email", "x@warden.test", "--scopes", "tasks:read", "--role", "admin"]
        )
    args = user_token.parse_args(
        ["--email", "x@warden.test", "--scopes", "tasks:read", "--role", "worker"]
    )
    assert args.role == "worker"
