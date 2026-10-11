"""Tracing without a container: the publication phase through the loop, and the API field.

The sandbox-backed run in test_tracing.py has no GitHub configured, so the `publish` span and a
task parking in the middle of one are covered here with the same doubles as test_publish.py.
"""

import pathlib
import uuid
from collections.abc import AsyncIterator, Sequence

import pytest
from httpx import ASGITransport, AsyncClient
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.trace import StatusCode
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.test_publish import (
    SUMMARY,
    _decide,
    _OpenPr,
    _resume,
    _Reviewer,
    _run,
    _Verifier,
)
from tests.tracing_support import capture_spans
from warden import identity
from warden.api.app import create_app
from warden.identity.jwt import KeyPair
from warden.models import Task, User


@pytest.fixture(autouse=True)
async def _clean(session: AsyncSession) -> AsyncIterator[None]:
    wipe = text(
        "TRUNCATE audit_log, issued_tokens, task_events, model_calls, tasks, users "
        "RESTART IDENTITY CASCADE"
    )
    await session.execute(wipe)
    await session.commit()
    yield
    await session.execute(wipe)
    await session.commit()


def _named(spans: Sequence[ReadableSpan], name: str) -> list[ReadableSpan]:
    return [s for s in spans if s.name == name]


def _parent(span: ReadableSpan, spans: Sequence[ReadableSpan]) -> ReadableSpan | None:
    if span.parent is None:
        return None
    return next(
        s for s in spans if s.context is not None and s.context.span_id == span.parent.span_id
    )


async def test_publication_is_one_span_that_holds_its_policy_and_tool_spans(
    session: AsyncSession, keys: KeyPair, tmp_path: pathlib.Path
) -> None:
    """Park for a human, then resume and open the pull request. The first publish span ends
    while the task is parked, and parking is control flow, not an error."""
    verifier, reviewer, publisher = _Verifier(), _Reviewer(), _OpenPr()
    with capture_spans() as exporter:
        task, parked = await _run(
            session, keys, tmp_path, verifier=verifier, reviewer=reviewer, publisher=publisher
        )
        assert parked.status == "WAITING_APPROVAL"
        await _decide(session, task, approve=True, note=None)
        finished = await _resume(
            session, keys, tmp_path, task, verifier=verifier, reviewer=reviewer, publisher=publisher
        )
        assert finished.status == "SUCCEEDED"
        spans = list(exporter.get_finished_spans())

    publishes = _named(spans, "publish")
    assert len(publishes) == 2  # one per claim
    assert all(p.status.status_code is not StatusCode.ERROR for p in publishes)
    for name in ("policy.evaluate", "tool.execute"):
        inside = [s for s in _named(spans, name) if (_parent(s, spans) or s).name == "publish"]
        assert inside, f"no {name} under publish"
    executed = [
        s
        for s in _named(spans, "tool.execute")
        if dict(s.attributes or {})["warden.tool"] == "github.open_pr"
    ]
    assert len(executed) == 1  # approved once, opened once
    # The pull request's title, branch and body are arguments: none of them is on a span.
    assert SUMMARY not in str([dict(s.attributes or {}) for s in spans])
    assert len(_named(spans, "review")) == 1


# --- the API field ---------------------------------------------------------------------------


@pytest.fixture
async def client(
    session_factory: async_sessionmaker[AsyncSession], keys: KeyPair
) -> AsyncIterator[AsyncClient]:
    app = create_app(session_factory=session_factory, keys=keys)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac


async def _user_and_token(
    session_factory: async_sessionmaker[AsyncSession], keys: KeyPair
) -> tuple[uuid.UUID, dict[str, str]]:
    async with session_factory() as session:
        user = User(email=f"{uuid.uuid4()}@warden.test", password_hash="!", role="user")
        session.add(user)
        await session.flush()
        token = await identity.issue_user_token(
            session, keys, user, scopes=["tasks:read", "tasks:write"]
        )
        await session.commit()
        return user.id, {"Authorization": f"Bearer {token}"}


async def test_the_task_endpoint_returns_its_trace_id_or_null(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession], keys: KeyPair
) -> None:
    _, headers = await _user_and_token(session_factory, keys)
    ids: list[str] = []
    for _ in range(2):
        created = await client.post(
            "/tasks", json={"spec": "x"}, headers={**headers, "Idempotency-Key": str(uuid.uuid4())}
        )
        assert created.status_code == 201
        ids.append(created.json()["id"])
    trace_id = "4bf92f3577b34da6a3ce929d0e0e4736"
    async with session_factory() as session:
        row = await session.get(Task, uuid.UUID(ids[0]))
        assert row is not None
        row.trace_id = trace_id
        await session.commit()

    traced = await client.get(f"/tasks/{ids[0]}", headers=headers)
    untraced = await client.get(f"/tasks/{ids[1]}", headers=headers)

    assert traced.json()["trace_id"] == trace_id
    assert untraced.json()["trace_id"] is None
