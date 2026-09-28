"""The event log is what the task actually is, so its ordering and redaction get tested."""

import uuid

import pytest
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession

from warden.config import Settings
from warden.core.events import append_event, read_events, record_tool_call, redact_args
from warden.models import Task, User
from warden.providers.base import ToolCall as ProviderToolCall

# Deliberately not GitHub- or OpenAI-token-shaped: a string a secret scanner could flag as a
# real leaked credential defeats the point of a fixture. Same convention as test_broker.py.
FAKE_TOKEN = "not-a-real-secret-abcdefghijklmnop"


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


# --- broker.redact() as the event log's one choke point (ADR-025, deliverable 4) ------------


def _with_fake_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """Point broker.redact() at a known, fixed secret instead of whatever GITHUB_TOKEN this
    developer's own .env happens to have (or not have) configured, so the assertion is
    deterministic. `identity.broker.get_settings` is what redact() calls when no `settings`
    is passed in, so that is the one seam to patch.
    """
    monkeypatch.setattr(
        "warden.identity.broker.get_settings",
        lambda: Settings(github_token=SecretStr(FAKE_TOKEN)),
    )


async def test_append_event_redacts_a_secret_in_a_string_field(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A tool's own output can echo a secret back (github.open_pr's response body, a run_command
    that greps its own environment); the event log must not become a second place it leaks."""
    _with_fake_token(monkeypatch)
    task = await _a_task(session)

    await append_event(
        session,
        task.id,
        "tool.executed",
        {"tool": "github.open_pr", "output": f"opened PR using token {FAKE_TOKEN} ok"},
    )

    payload = (await read_events(session, task.id))[0].payload
    assert FAKE_TOKEN not in payload["output"]
    assert payload["output"] == "opened PR using token [redacted] ok"
    assert payload["tool"] == "github.open_pr"  # untouched: not the secret


async def test_append_event_redacts_a_secret_nested_inside_a_list(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The choke point walks the whole payload, not just its top-level string values:
    policy.decided's `paths` is a list, and a path is still a string a leak could hide in."""
    _with_fake_token(monkeypatch)
    task = await _a_task(session)

    await append_event(
        session, task.id, "policy.decided", {"paths": ["src/app.py", f"leak-{FAKE_TOKEN}.py"]}
    )

    payload = (await read_events(session, task.id))[0].payload
    assert all(FAKE_TOKEN not in path for path in payload["paths"])
    assert payload["paths"][0] == "src/app.py"


async def test_record_tool_call_redacts_result_summary_and_error(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """tool_calls.result_summary/error are the other place a tool's raw text is stored,
    outside task_events entirely; they need the same choke point."""
    _with_fake_token(monkeypatch)
    task = await _a_task(session)
    call = ProviderToolCall(id="c1", name="run_command", arguments={})

    row = await record_tool_call(
        session,
        task.id,
        1,
        call,
        decision="allow",
        result_summary=f"leaked {FAKE_TOKEN}",
        error=f"boom {FAKE_TOKEN}",
    )

    assert FAKE_TOKEN not in (row.result_summary or "")
    assert FAKE_TOKEN not in (row.error or "")
