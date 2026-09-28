"""tools/gateway.py: the seam between an ALLOW decision and a tool actually running (ADR-025).

Every token here comes from a real `identity.issue_agent_token` -> database round trip, same
convention as test_broker.py: a hand-built `Claims` would only ever exercise `identity.verify`
itself, not this module. Failure is caused for real (an expired row, a revoked row, a token
minted for a different task), never simulated by monkeypatching `identity.verify`.
"""

import uuid
from datetime import UTC, datetime

import pytest
from pydantic import BaseModel
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from warden import identity
from warden.identity.jwt import KeyPair
from warden.models import AuditLog, IssuedToken, Task, User
from warden.tools import gateway
from warden.tools.registry import ToolContext, ToolError, ToolRegistry

FAKE_TOKEN = "not-a-real-secret-abcdefghijklmnop"


async def _user(session: AsyncSession) -> User:
    row = User(email=f"{uuid.uuid4()}@warden.test", password_hash="x", role="submitter")
    session.add(row)
    await session.flush()
    return row


async def _task(session: AsyncSession) -> Task:
    row = Task(
        user_id=(await _user(session)).id, spec="open a pr", idempotency_key=str(uuid.uuid4())
    )
    session.add(row)
    await session.flush()
    return row


class _Recorder:
    """Proves whether the tool underneath the gateway actually ran."""

    def __init__(self) -> None:
        self.calls: list[ToolContext | None] = []

    async def plain(self, args: object) -> str:
        self.calls.append(None)
        return "plain ok"

    async def scoped(self, args: object, context: ToolContext) -> str:
        self.calls.append(context)
        return f"scoped ok as {context.claims.sub}"


class _NoArgs(BaseModel):
    pass


def _registry(recorder: _Recorder) -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(
        name="plain_tool",
        description="",
        args_model=_NoArgs,
        execute=recorder.plain,
    )
    registry.register(
        name="scoped_tool",
        description="",
        args_model=_NoArgs,
        execute=recorder.scoped,
        required_scope="github:pr:open",
        needs_identity=True,
    )
    return registry


async def _audit_rows(session: AsyncSession, action: str, task_id: uuid.UUID) -> list[AuditLog]:
    rows = await session.scalars(
        select(AuditLog)
        .where(AuditLog.action == action, AuditLog.target_id == str(task_id))
        .order_by(AuditLog.id)
    )
    return list(rows.all())


# --- the happy path -------------------------------------------------------------------------


async def test_a_live_task_bound_token_runs_the_tool(session: AsyncSession, keys: KeyPair) -> None:
    task = await _task(session)
    recorder = _Recorder()
    token = await identity.issue_agent_token(
        session, keys, task_id=task.id, scopes=["tool:plain_tool"]
    )

    output = await gateway.execute(
        session, keys, _registry(recorder), "plain_tool", {}, token=token, task_id=task.id
    )

    assert output == "plain ok"
    assert recorder.calls == [None]


async def test_a_tool_that_needs_identity_receives_the_verified_claims(
    session: AsyncSession, keys: KeyPair
) -> None:
    task = await _task(session)
    recorder = _Recorder()
    token = await identity.issue_agent_token(
        session, keys, task_id=task.id, scopes=["github:pr:open"]
    )

    output = await gateway.execute(
        session, keys, _registry(recorder), "scoped_tool", {}, token=token, task_id=task.id
    )

    assert output == f"scoped ok as agent:task:{task.id}"
    assert len(recorder.calls) == 1
    context = recorder.calls[0]
    assert context is not None
    assert context.claims.scopes == ("github:pr:open",)
    assert context.session is session


# --- 401: the token itself does not hold up ------------------------------------------------


async def test_an_expired_token_is_never_executed_and_is_audited(
    session: AsyncSession, keys: KeyPair
) -> None:
    task = await _task(session)
    recorder = _Recorder()
    token = await identity.issue_agent_token(
        session, keys, task_id=task.id, scopes=["tool:plain_tool"]
    )
    # Cause the real expiry, in the database, rather than mocking `identity.verify`.
    await session.execute(
        update(IssuedToken)
        .where(IssuedToken.task_id == task.id)
        .values(expires_at=datetime(2000, 1, 1, tzinfo=UTC))
    )
    await session.flush()

    with pytest.raises(ToolError, match="401"):
        await gateway.execute(
            session, keys, _registry(recorder), "plain_tool", {}, token=token, task_id=task.id
        )

    assert recorder.calls == []
    rows = await _audit_rows(session, "tool.auth_failed", task.id)
    assert len(rows) == 1
    assert rows[0].details["tool"] == "plain_tool"
    assert FAKE_TOKEN not in str(rows[0].details)


async def test_a_revoked_token_is_never_executed_and_is_audited(
    session: AsyncSession, keys: KeyPair
) -> None:
    task = await _task(session)
    recorder = _Recorder()
    token = await identity.issue_agent_token(
        session, keys, task_id=task.id, scopes=["tool:plain_tool"]
    )
    claims = await identity.verify(session, keys, token)
    await identity.revoke(session, claims.jti)

    with pytest.raises(ToolError, match="401"):
        await gateway.execute(
            session, keys, _registry(recorder), "plain_tool", {}, token=token, task_id=task.id
        )

    assert recorder.calls == []
    rows = await _audit_rows(session, "tool.auth_failed", task.id)
    assert len(rows) == 1


async def test_a_token_minted_for_another_task_is_never_executed(
    session: AsyncSession, keys: KeyPair
) -> None:
    minted_for = await _task(session)
    running_task = await _task(session)
    recorder = _Recorder()
    token = await identity.issue_agent_token(
        session, keys, task_id=minted_for.id, scopes=["tool:plain_tool"]
    )

    with pytest.raises(ToolError, match="401"):
        await gateway.execute(
            session,
            keys,
            _registry(recorder),
            "plain_tool",
            {},
            token=token,
            task_id=running_task.id,
        )

    assert recorder.calls == []
    rows = await _audit_rows(session, "tool.auth_failed", running_task.id)
    assert len(rows) == 1
    assert rows[0].details["reason"]


# --- 403: the token is live but under-scoped ------------------------------------------------


async def test_a_token_missing_the_required_scope_is_never_executed(
    session: AsyncSession, keys: KeyPair
) -> None:
    task = await _task(session)
    recorder = _Recorder()
    # A live, task-bound token, just not carrying the scope `scoped_tool` requires.
    token = await identity.issue_agent_token(session, keys, task_id=task.id, scopes=["repo:read"])

    with pytest.raises(ToolError, match="403"):
        await gateway.execute(
            session, keys, _registry(recorder), "scoped_tool", {}, token=token, task_id=task.id
        )

    assert recorder.calls == []
    rows = await _audit_rows(session, "tool.forbidden", task.id)
    assert len(rows) == 1
    assert rows[0].details["tool"] == "scoped_tool"


async def test_a_tool_with_no_required_scope_is_not_scope_checked(
    session: AsyncSession, keys: KeyPair
) -> None:
    """`plain_tool` declares no `required_scope`: any live, task-bound token runs it,
    whatever scopes it happens to carry."""
    task = await _task(session)
    recorder = _Recorder()
    token = await identity.issue_agent_token(session, keys, task_id=task.id, scopes=["repo:read"])

    output = await gateway.execute(
        session, keys, _registry(recorder), "plain_tool", {}, token=token, task_id=task.id
    )

    assert output == "plain ok"
