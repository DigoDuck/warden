"""The event log is what the task actually is, so its ordering and redaction get tested."""

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from warden.core.events import append_event, read_events, redact_args
from warden.models import Task, User


async def _a_task(session: AsyncSession) -> Task:
    user = User(email=f"{uuid.uuid4()}@warden.test", password_hash="x", role="submitter")
    session.add(user)
    await session.flush()
    task = Task(idempotency_key=str(uuid.uuid4()), user_id=user.id, spec="anything")
    session.add(task)
    await session.flush()
    return task


async def test_seq_is_allocated_in_order(session: AsyncSession) -> None:
    task = await _a_task(session)
    for kind in ("task.created", "iteration.started", "task.finished"):
        await append_event(session, task.id, kind)

    assert [event.seq for event in await read_events(session, task.id)] == [1, 2, 3]


async def test_events_read_back_in_the_order_written(session: AsyncSession) -> None:
    task = await _a_task(session)
    written = ["task.created", "model.called", "tool.requested", "tool.executed"]
    for kind in written:
        await append_event(session, task.id, kind)

    assert [event.type for event in await read_events(session, task.id)] == written


async def test_seq_restarts_per_task(session: AsyncSession) -> None:
    """Two tasks running at once must not share a counter."""
    first, second = await _a_task(session), await _a_task(session)
    await append_event(session, first.id, "task.created")
    await append_event(session, second.id, "task.created")

    assert (await read_events(session, second.id))[0].seq == 1


async def test_payload_survives_the_round_trip(session: AsyncSession) -> None:
    task = await _a_task(session)
    await append_event(session, task.id, "model.called", {"tokens_in": 10, "cost_usd": "0.0001"})

    assert (await read_events(session, task.id))[0].payload == {
        "tokens_in": 10,
        "cost_usd": "0.0001",
    }


def test_redaction_masks_sensitive_keys() -> None:
    """The column is called args_safe; writing raw arguments into it would be a quiet lie."""
    safe = redact_args({"path": "src/app.py", "github_token": "ghp_realsecret"})
    assert safe["path"] == "src/app.py"
    assert "ghp_realsecret" not in str(safe)
    assert safe["github_token"] == "[redacted]"


def test_redaction_is_case_insensitive_about_key_names() -> None:
    assert redact_args({"API_KEY": "sk-x"})["API_KEY"] == "[redacted]"
    assert redact_args({"Authorization": "Bearer x"})["Authorization"] == "[redacted]"


def test_redaction_truncates_long_values() -> None:
    """Whole file contents must not land in telemetry through a tool argument."""
    safe = redact_args({"content": "y" * 10_000})
    assert "truncated" in safe["content"]
    assert len(safe["content"]) < 10_000


def test_redaction_leaves_ordinary_arguments_alone() -> None:
    original = {"path": "src/app.py", "pattern": "**/*.py", "limit": 10}
    assert redact_args(original) == original
