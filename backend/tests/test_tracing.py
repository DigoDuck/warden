"""Tracing through a real Worker and a real sandbox (ADR-033).

What is under test is the shape of what leaves the process: the span tree briefing section 20
promises, one trace per task across claims, and, above all, that nothing a span carries can be a
secret or a file's content. Planted values are real ones (a denied `.env`, a file written with a
token in it, a token in an argument), because a test that only checks the helper would still pass
if a call site bypassed it.
"""

import logging
import os
import pathlib
import socket
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import pytest
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.trace import StatusCode
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests import fake_secrets
from tests.fake_tools import verdict_step
from tests.tracing_support import capture_spans, names
from warden import telemetry
from warden.core import approvals, queue
from warden.core.worker import Worker
from warden.identity import broker
from warden.identity.jwt import KeyPair
from warden.models import Approval, Task, User
from warden.policy.engine import Effect, Policy, Rule, load_policy
from warden.providers.base import ToolCall as ProviderToolCall
from warden.providers.fake import FakeProvider, ScriptStep
from warden.verify.runner import KINDS

pytestmark = pytest.mark.sandbox

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
DEFAULT_POLICY = REPO_ROOT / "policies" / "default.yaml"

# A string that exists only in a file the agent is allowed to read. If it shows up in a span,
# file content got out.
CONTENT_MARKER = "CONTENT_MARKER_7f3a91"

_WIPE = text("TRUNCATE audit_log, tasks, users RESTART IDENTITY CASCADE")


@pytest.fixture(scope="module")
def docker_available() -> None:
    """Same pattern as the other sandbox tests: skip locally without Docker, never in CI."""
    import docker

    try:
        docker.from_env().ping()
    except Exception as exc:  # noqa: BLE001 - any failure to reach the daemon counts
        if os.environ.get("CI"):
            raise RuntimeError(f"CI requires a working Docker daemon: {exc}") from exc
        pytest.skip(f"Docker is not available on this machine: {exc}")


def _make_workspace(root: pathlib.Path) -> pathlib.Path:
    """A repo the control plane's checks accept, plus a `.env` holding a secret."""
    (root / "src").mkdir(parents=True)
    (root / "src" / "app.py").write_text(
        f'MARKER = "{CONTENT_MARKER}"\n', encoding="utf-8", newline="\n"
    )
    (root / "tests").mkdir()
    (root / "tests" / "test_ok.py").write_text(
        "def test_ok() -> None:\n    assert True\n", encoding="utf-8", newline="\n"
    )
    (root / ".env").write_text(
        f"ANTHROPIC_API_KEY={fake_secrets.anthropic_key()}\n", encoding="utf-8", newline="\n"
    )
    return root


def _call(name: str, **arguments: object) -> ScriptStep:
    call = ProviderToolCall(
        id=f"call-{name}-{uuid.uuid4().hex[:6]}", name=name, arguments=arguments
    )
    return ScriptStep(tool_calls=[call])


async def _enqueue(
    session_factory: async_sessionmaker[AsyncSession],
) -> tuple[uuid.UUID, uuid.UUID]:
    async with session_factory() as setup:
        user = User(email=f"{uuid.uuid4()}@warden.test", password_hash="x", role="worker")
        setup.add(user)
        await setup.flush()
        task = await queue.enqueue(
            setup, user_id=user.id, spec="read the code", idempotency_key=str(uuid.uuid4())
        )
        await setup.commit()
        return user.id, task.id


@dataclass
class TracedRun:
    spans: list[ReadableSpan]
    status: str
    trace_id_column: str | None
    state: dict[str, Any]
    planted: list[str]


@pytest.fixture(scope="module")
async def planted_run(
    keys: KeyPair,
    docker_available: None,
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path_factory: pytest.TempPathFactory,
) -> AsyncIterator[TracedRun]:
    """One task that touches every kind of thing a span must not leak, run once for the module."""
    workspace = _make_workspace(tmp_path_factory.mktemp("repo"))
    token = fake_secrets.github_pat()
    steps = [
        _call("read_file", path=".env"),  # denied by never-read-secrets
        _call("read_file", path="src/app.py"),  # allowed; its content must stay out of spans
        _call("write_file", path="src/leak.py", content=f'TOKEN = "{token}"\n'),
        _call("run_command", cmd=f"pytest -q -k {token}"),  # a token in an argument
        _call("finish", summary="done"),
        verdict_step(),
    ]
    _, task_id = await _enqueue(session_factory)

    with capture_spans() as exporter:
        worker = Worker(
            session_factory,
            lambda: FakeProvider(steps),
            load_policy(DEFAULT_POLICY),
            workspace,
            keys,
        )
        result = await worker.run_once()
        spans = list(exporter.get_finished_spans())

    assert result is not None
    async with session_factory() as probe:
        row = await probe.get(Task, task_id)
        assert row is not None
        run = TracedRun(
            spans=spans,
            status=result.status,
            trace_id_column=row.trace_id,
            state=dict(row.state),
            planted=[token, CONTENT_MARKER, fake_secrets.anthropic_key()],
        )
    yield run
    async with session_factory() as cleanup:
        await cleanup.execute(_WIPE)
        await cleanup.commit()


def _by_name(spans: list[ReadableSpan], name: str) -> list[ReadableSpan]:
    return [s for s in spans if s.name == name]


def _attrs(span: ReadableSpan) -> dict[str, Any]:
    return dict(span.attributes or {})


def _span_id(span: ReadableSpan) -> int:
    assert span.context is not None
    return span.context.span_id


def _trace_id(span: ReadableSpan) -> int:
    assert span.context is not None
    return span.context.trace_id


def _parent_name(span: ReadableSpan, spans: list[ReadableSpan]) -> str | None:
    if span.parent is None:
        return None
    return next(s.name for s in spans if _span_id(s) == span.parent.span_id)


# --- the tree --------------------------------------------------------------------------------


async def test_the_planted_task_succeeds(planted_run: TracedRun) -> None:
    assert planted_run.status == "SUCCEEDED"


async def test_one_task_is_one_trace_with_a_single_root(planted_run: TracedRun) -> None:
    spans = planted_run.spans
    assert spans, "no spans were exported"
    assert {_trace_id(s) for s in spans} == {_trace_id(spans[0])}
    roots = [s for s in spans if s.parent is None]
    assert names(roots) == ["task.run"]
    assert planted_run.trace_id_column == format(_trace_id(roots[0]), "032x")
    assert planted_run.state["root_span_id"] == format(_span_id(roots[0]), "016x")


async def test_the_span_tree_follows_briefing_section_20(planted_run: TracedRun) -> None:
    spans = planted_run.spans
    # verify.run and review sit under the iteration that finished the task, or under task.run when
    # a resumed claim goes straight to verification.
    expected_parents = {
        "planner.plan": {"task.run"},
        "agent.iteration": {"task.run"},
        "model.generate": {"agent.iteration"},
        "policy.evaluate": {"agent.iteration"},
        "tool.execute": {"agent.iteration"},
        "sandbox.exec": {"tool.execute", "verify.run", "policy.evaluate"},
        "verify.run": {"task.run", "agent.iteration"},
        "review": {"task.run", "agent.iteration"},
    }
    for name, parents in expected_parents.items():
        found = _by_name(spans, name)
        assert found, f"no {name} span"
        assert {_parent_name(s, spans) for s in found} <= parents, name


async def test_every_decided_call_has_a_policy_span_and_only_executed_ones_a_tool_span(
    planted_run: TracedRun,
) -> None:
    """Four calls were decided; the `.env` read was denied and therefore never executed."""
    spans = planted_run.spans
    decisions = [_attrs(s) for s in _by_name(spans, "policy.evaluate")]
    assert [d["warden.tool"] for d in decisions] == [
        "read_file",
        "read_file",
        "write_file",
        "run_command",
    ]
    assert [d["warden.effect"] for d in decisions] == ["deny", "allow", "allow", "allow"]
    assert "never-read-secrets" in str(decisions[0]["warden.rules"])
    executed = [_attrs(s) for s in _by_name(spans, "tool.execute")]
    assert [e["warden.tool"] for e in executed] == ["read_file", "write_file", "run_command"]
    assert all(len(str(e["warden.args_hash"])) == 64 for e in executed)


async def test_the_model_spans_carry_tokens_cost_and_model(planted_run: TracedRun) -> None:
    generate = _by_name(planted_run.spans, "model.generate")
    assert len(generate) == 5  # four tool turns and the finish
    for s in generate:
        attrs = _attrs(s)
        assert attrs["warden.model"] == "fake-model"
        assert attrs["warden.provider"] == "fake"
        assert {"warden.tokens_in", "warden.tokens_out", "warden.cost_usd"} <= set(attrs)


async def test_the_sandbox_and_verification_spans_say_what_ran_not_what_it_said(
    planted_run: TracedRun,
) -> None:
    execs = _by_name(planted_run.spans, "sandbox.exec")
    assert execs
    for s in execs:
        attrs = _attrs(s)
        assert attrs["warden.image"] == "warden-sandbox:dev"
        assert attrs["warden.container_id"]
        assert set(attrs) <= {"warden.image", "warden.container_id", "warden.status"}
    kinds = [_attrs(s)["warden.kind"] for s in _by_name(planted_run.spans, "verify.run")]
    assert kinds == list(KINDS)
    assert _attrs(_by_name(planted_run.spans, "review")[0])["warden.passed"] is True


# --- what a span must never carry ------------------------------------------------------------


def _everything_exported(spans: list[ReadableSpan]) -> list[str]:
    """Every string that left the process for a span: names, attribute keys and values, status
    text, event names and event attributes, and the resource."""
    out: list[str] = []
    for s in spans:
        out.append(s.name)
        out += [str(k) for k in (s.attributes or {})]
        out += [str(v) for v in (s.attributes or {}).values()]
        out.append(str(s.status.description or ""))
        for event in s.events:
            out.append(event.name)
            out += [str(v) for v in (event.attributes or {}).values()]
        out += [str(v) for v in s.resource.attributes.values()]
    return out


async def test_no_span_carries_a_planted_secret_or_file_content(planted_run: TracedRun) -> None:
    exported = _everything_exported(planted_run.spans)
    assert len(exported) > 50  # a run with no spans would pass the loop below vacuously
    for planted in planted_run.planted:
        assert not [t for t in exported if planted in t], planted


async def test_no_span_attribute_matches_a_secret_pattern(planted_run: TracedRun) -> None:
    exported = _everything_exported(planted_run.spans)
    assert exported
    for exported_text in exported:
        for pattern in broker.SECRET_PATTERNS:
            assert not pattern.search(exported_text), (pattern.pattern, exported_text)


async def test_every_attribute_key_is_on_the_allow_list(planted_run: TracedRun) -> None:
    assert planted_run.spans
    keys = {key for s in planted_run.spans for key in (s.attributes or {})}
    assert keys <= telemetry.ALLOWED_ATTRIBUTES
    assert not [s for s in planted_run.spans if s.events]  # no exception events either


# --- one trace across claims -----------------------------------------------------------------


def _approval_policy() -> Policy:
    return Policy(
        [
            Rule(
                id="shell-needs-human",
                effect=Effect.REQUIRE_APPROVAL,
                reason="a shell command needs a human's sign-off",
                scopes=["shell:run"],
                when={"tool": "run_command"},
            )
        ],
        default=Effect.DENY,
        policy_hash="test",
    )


async def test_a_task_resumed_after_approval_continues_the_same_trace(
    keys: KeyPair,
    docker_available: None,
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: pathlib.Path,
) -> None:
    workspace = _make_workspace(tmp_path / "repo")
    user_id, task_id = await _enqueue(session_factory)
    steps = [
        _call("run_command", cmd="echo traced"),
        _call("finish", summary="done"),
        verdict_step(),
    ]

    def make_worker() -> Worker:
        # resume_aware: each claim builds a fresh provider, which must continue the script.
        return Worker(
            session_factory,
            lambda: FakeProvider(steps, resume_aware=True),
            _approval_policy(),
            workspace,
            keys,
        )

    with capture_spans() as exporter:
        parked = await make_worker().run_once()
        assert parked is not None and parked.status == "WAITING_APPROVAL"

        async with session_factory() as decide:
            approval = (
                await decide.scalars(select(Approval).where(Approval.task_id == task_id))
            ).one()
            await approvals.decide_approval(
                decide, approval.id, approve=True, user_id=user_id, note=None
            )
            await decide.commit()

        finished = await make_worker().run_once()
        assert finished is not None and finished.status == "SUCCEEDED", finished
        spans = list(exporter.get_finished_spans())

    runs = _by_name(spans, "task.run")
    assert len(runs) == 2
    assert len({_trace_id(s) for s in spans}) == 1
    root, resumed = sorted(runs, key=lambda s: s.start_time or 0)
    assert root.parent is None
    assert resumed.parent is not None and resumed.parent.span_id == _span_id(root)
    assert _attrs(resumed)["warden.resumed"] is True
    assert _attrs(root)["warden.resumed"] is False
    # Parking for a human is not a failure.
    assert root.status.status_code is not StatusCode.ERROR

    async with session_factory() as probe:
        row = await probe.get(Task, task_id)
        assert row is not None
        assert row.trace_id == format(_trace_id(root), "032x")
    async with session_factory() as cleanup:
        await cleanup.execute(_WIPE)
        await cleanup.commit()


# --- an exporter that cannot reach its collector ---------------------------------------------


def _closed_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


async def test_a_collector_that_is_down_does_not_change_the_task(
    keys: KeyPair,
    docker_available: None,
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The OTLP exporter is pointed at a port nothing listens on and a real task runs through it:
    the failure is caused, not simulated. The export happens (and fails) in the exporter's own
    thread; the task must finish exactly as it would have with no tracing at all."""
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", f"http://127.0.0.1:{_closed_port()}")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_TIMEOUT", "2")
    workspace = _make_workspace(tmp_path / "repo")
    _, task_id = await _enqueue(session_factory)
    steps = [_call("read_file", path="src/app.py"), _call("finish", summary="done"), verdict_step()]

    # The session fixture runs Alembic, whose env.py calls logging.fileConfig and thereby DISABLES
    # every logger that already exists, the exporter's included (verified: its record never
    # reaches caplog otherwise). Re-enable it so the failure can be observed.
    exporter_log = logging.getLogger("opentelemetry.exporter.otlp.proto.http.trace_exporter")
    exporter_log.disabled = False
    provider = telemetry.setup_tracing("warden-closed-port-test")
    assert provider is not None
    try:
        with caplog.at_level("WARNING"):
            worker = Worker(
                session_factory,
                lambda: FakeProvider(steps),
                load_policy(DEFAULT_POLICY),
                workspace,
                keys,
            )
            result = await worker.run_once()
            provider.force_flush(timeout_millis=15000)
    finally:
        telemetry.configure(None)
        provider.shutdown()

    assert result is not None and result.status == "SUCCEEDED", result
    # The exporter really tried and really failed (this is what makes the test a test).
    assert any(
        "export" in record.getMessage().lower() and record.levelname in ("WARNING", "ERROR")
        for record in caplog.records
    ), [r.getMessage() for r in caplog.records]
    async with session_factory() as probe:
        row = await probe.get(Task, task_id)
        assert row is not None
        assert row.status == "SUCCEEDED"
        assert row.trace_id is not None  # tracing was on; only the export failed
    async with session_factory() as cleanup:
        await cleanup.execute(_WIPE)
        await cleanup.commit()
