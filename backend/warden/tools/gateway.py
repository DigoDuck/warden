"""Skeleton so tests/test_gateway.py imports. Real verification lands in the next commit."""

from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from warden.identity.jwt import KeyPair, verify
from warden.tools.registry import ToolContext, ToolRegistry


class GatewayError(Exception):
    pass


async def execute(
    session: AsyncSession,
    keys: KeyPair,
    registry: ToolRegistry,
    name: str,
    arguments: dict[str, Any],
    *,
    token: str,
    task_id: UUID,
) -> str:
    claims = await verify(session, keys, token)
    context = ToolContext(claims=claims, session=session)
    return await registry.execute(name, arguments, context=context)
