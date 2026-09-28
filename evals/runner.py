"""Runs the behavioral evals dataset (briefing §19/§46) as real tasks against the real
control plane: `queue.enqueue`/`queue.claim`, a real `Worker`, the real sandbox, the real
policy engine, the real gateway and the real audit log. The only thing that is not real is
the model, which is a resume-aware `FakeProvider` replaying each case's script.

Nine of the twelve cases from briefing §46 run for real; three (4, 9, 12) are recorded as
`status: pending` in the dataset because they need features this track does not build
(capability manifest + incidents, agent lifecycle/revocation, stateful policy) — see each
case's `reason` in evals/datasets/behavioral_v1.yaml. A pending case is reported, counted,
and never fails the exit code; it is never silently skipped.

Two cases (7, 8) do not fit the generic "one Worker.run_once(), then read the database back"
shape, and get their own function instead of a generic action-interpreter for actions only
one case ever needs (ponytail: no DSL for two call sites):

  - Case 7 (resume after a crash) needs a REAL OS process to kill, not a caught exception,
    so it shells out to `python -m warden.core.worker` and SIGKILLs it, the same technique
    backend/tests/test_durability.py already uses.
  - Case 8 (an expired token is rejected at the gateway) cannot be provoked through the
    live agent loop at all: `core/loop.py` mints a token and spends it in the same
    uninterruptible instant (one synchronous branch between the mint's checkpoint and
    `gateway.execute`), so an external process racing that window is not a deterministic
    test, it is a coin flip. This calls `tools/gateway.execute` directly instead, against a
    token this runner deliberately let expire, which exercises the exact same
    verify-then-run boundary without needing to win an unwinnable race.

Usage: `uv run --project backend python -m evals.runner evals/datasets/behavioral_v1.yaml`
(see evals/__init__.py for why `--project backend` from the repo root, not `--directory`).
"""

from __future__ import annotations

import argparse
import asyncio
import os
import pathlib
import subprocess
import sys
import tempfile
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import yaml
from alembic import command
from alembic.config import Config
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from sqlalchemy import func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from warden import identity
from warden.config import get_settings
from warden.core import approvals, cancel, events, queue
from warden.core.worker import POLICY_FILE, WORKSPACE, Worker
from warden.db import make_session_factory, with_database
from warden.identity.jwt import KeyPair, _kid_for
from warden.models import Approval, AuditLog, Task, User
from warden.models import ToolCall as ToolCallRow
from warden.policy.engine import Policy, load_policy
from warden.providers.base import Completion, Usage
from warden.providers.fake import FakeProvider
from warden.tools import gateway
from warden.tools.registry import ToolRegistry

from evals.checks import CaseOutcome, Facts, check_expectations, summarize

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
BACKEND_DIR = REPO_ROOT / "backend"
DEFAULT_DATASET = REPO_ROOT / "evals" / "datasets" / "behavioral_v1.yaml"
DEFAULT_METRICS = REPO_ROOT / "docs" / "metrics.md"
TEST_DB = os.environ.get("WARDEN_TEST_DB", "warden_test")

# github.open_pr is only registered by build_registry() when both of these are set
# (ADR-025's "absent, not refusing" shape). Case 3 needs the tool to exist so the policy
# engine's own require_approval rule pauses the task; it never actually reaches GitHub,
# because a REQUIRE_APPROVAL decision returns before core/loop.py ever calls gateway.execute,
# and the rejection path in _run_tools answers the call without executing it either. A
# placeholder repo/token is therefore honest, not a workaround: nothing here ever makes an
# HTTP request.
os.environ.setdefault("GITHUB_REPO", "evals/unused-placeholder-repo")
os.environ.setdefault("GITHUB_TOKEN", "eval-placeholder-token")


# --------------------------------------------------------------------------------------
# Dataset
# --------------------------------------------------------------------------------------


@dataclass
class Case:
    id: int
    key: str
    status: str  # "active" | "pending"
    reason: str | None
    script: list[dict[str, Any]]
    budget: dict[str, Any] | None
    expect: dict[str, Any]


def load_cases(path: pathlib.Path) -> list[Case]:
    document = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    cases = []
    for raw in document.get("cases", []):
        cases.append(
            Case(
                id=int(raw["id"]),
                key=str(raw["key"]),
                status=str(raw.get("status", "active")),
                reason=raw.get("reason"),
                script=raw.get("script") or [],
                budget=raw.get("budget"),
                expect=raw.get("expect") or {},
            )
        )
    return cases


# --------------------------------------------------------------------------------------
# Database bootstrap (same recipe as backend/tests/conftest.py::session_factory: drop,
# create, migrate for real rather than metadata.create_all, so a case runs against the
# schema that ships).
# --------------------------------------------------------------------------------------


async def prepare_database() -> tuple[async_sessionmaker[AsyncSession], str]:
    base_url = get_settings().database_url
    test_url = with_database(base_url, TEST_DB)

    admin = create_async_engine(
        with_database(base_url, "postgres"), isolation_level="AUTOCOMMIT"
    )
    async with admin.connect() as conn:
        await conn.execute(text(f'DROP DATABASE IF EXISTS "{TEST_DB}" WITH (FORCE)'))
        await conn.execute(text(f'CREATE DATABASE "{TEST_DB}"'))
    await admin.dispose()

    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    cfg.set_main_option("sqlalchemy.url", test_url)
    await asyncio.to_thread(command.upgrade, cfg, "head")

    engine = create_async_engine(test_url, pool_pre_ping=True)
    return make_session_factory(engine), test_url


def ephemeral_keys() -> KeyPair:
    """Same recipe as backend/tests/conftest.py::keys. Never `identity.load_keys()`: the
    hard rule for this track is no dependency on a machine-local .keys file, and CI has
    none."""
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_key = private_key.public_key()
    return KeyPair(
        private_key=private_key, public_key=public_key, kid=_kid_for(public_key)
    )


# --------------------------------------------------------------------------------------
# Shared helpers
# --------------------------------------------------------------------------------------


@dataclass
class Context:
    session_factory: async_sessionmaker[AsyncSession]
    keys: KeyPair
    policy: Policy
    tmp_dir: pathlib.Path
    test_db_url: str


async def _enqueue(
    ctx: Context, *, spec: str, budget: dict[str, Any] | None
) -> tuple[uuid.UUID, uuid.UUID]:
    async with ctx.session_factory() as session:
        user = User(
            email=f"eval-{uuid.uuid4()}@warden.test", password_hash="x", role="worker"
        )
        session.add(user)
        await session.flush()
        task = await queue.enqueue(
            session,
            user_id=user.id,
            spec=spec,
            idempotency_key=f"eval-{uuid.uuid4()}",
            budget=budget,
        )
        await session.commit()
        return task.id, user.id


def _write_script_yaml(
    tmp_dir: pathlib.Path, script: list[dict[str, Any]]
) -> pathlib.Path:
    """Write a case's `script` list as the YAML FakeProvider.from_yaml() already knows how
    to read, instead of poking its private step parser: one parser for this shape, used by
    the worker, the demo and every eval case alike."""
    path = tmp_dir / f"script-{uuid.uuid4().hex}.yaml"
    path.write_text(
        yaml.safe_dump({"script": script}, sort_keys=False), encoding="utf-8"
    )
    return path


async def gather_facts(ctx: Context, task_id: uuid.UUID) -> Facts:
    """Read back what a case run actually did, straight from the database. Never trusts
    whatever runner.py's own orchestration thinks happened: the point of a behavioral eval
    is that the control plane's own record is the evidence, not the caller's bookkeeping.
    """
    async with ctx.session_factory() as session:
        task = await session.get(Task, task_id)
        assert task is not None

        rows = await events.read_events(session, task_id)
        policy_effects = [
            str(row.payload["effect"])
            for row in rows
            if row.type == events.POLICY_DECIDED
        ]
        tool_executed_count = sum(1 for row in rows if row.type == events.TOOL_EXECUTED)

        approvals_pending = (
            await session.scalar(
                select(func.count())
                .select_from(Approval)
                .where(Approval.task_id == task_id, Approval.status == "pending")
            )
        ) or 0

        tool_call_rows = (
            await session.scalars(
                select(ToolCallRow)
                .where(ToolCallRow.task_id == task_id)
                .order_by(ToolCallRow.iteration)
            )
        ).all()
        tool_calls: list[dict[str, Any]] = [
            {
                "tool": row.tool_name,
                "decision": row.decision,
                "args_safe": row.args_safe,
            }
            for row in tool_call_rows
        ]

        audit_rows = (
            await session.scalars(
                select(AuditLog)
                .where(
                    or_(
                        AuditLog.target_id == str(task_id),
                        AuditLog.details["task_id"].astext == str(task_id),
                    )
                )
                .order_by(AuditLog.id)
            )
        ).all()
        audit_corpus: list[str] = []
        for row in audit_rows:
            audit_corpus.append(row.action)
            details = row.details or {}
            for key in ("tool", "reason", "status", "jti", "subject"):
                value = details.get(key)
                if value:
                    audit_corpus.append(f"{row.action}:{value}")
            for rule in details.get("matched_rules") or []:
                audit_corpus.append(f"{row.action}:{rule}")

        return Facts(
            policy_effects=policy_effects,
            task_status=task.status,
            tool_executed_count=tool_executed_count,
            approvals_pending=approvals_pending,
            audit_corpus=audit_corpus,
            tool_calls=tool_calls,
        )


# --------------------------------------------------------------------------------------
# Provider wrappers. Both compose a plain FakeProvider rather than subclassing it: neither
# behaviour (pausing, reporting a real model's cost) belongs on the class every other case
# uses unmodified.
# --------------------------------------------------------------------------------------


class _PausingProvider:
    """Wraps a FakeProvider so the caller can act between "the model was asked for a turn"
    and "the loop saw the answer". Same technique as
    backend/tests/test_durability.py::_PausingProvider: standing in for what a slow model
    call would do, with the pause point exact and no sleep in the test.
    """

    def __init__(
        self, inner: FakeProvider, paused: asyncio.Event, release: asyncio.Event
    ) -> None:
        self._inner = inner
        self._paused = paused
        self._release = release
        self.name = inner.name

    async def generate(self, *args: Any, **kwargs: Any) -> Completion:
        self._paused.set()
        await self._release.wait()
        return await self._inner.generate(*args, **kwargs)


class _CostedProvider:
    """Reports a real, priced model instead of FakeProvider's own zeroed usage.

    FakeProvider always answers with `Usage()` (zero tokens) on purpose: a scripted run
    costs nothing (providers/fake.py's own docstring). That makes `max_usd` untestable
    through it unmodified, so case 6 wraps it to report `claude-haiku-4-5` and a fixed
    token count instead, which exercises the SAME `providers.pricing.cost_usd` and the
    SAME `spent > budget.max_usd` check in core/loop.py that a real provider would.
    """

    def __init__(
        self, inner: FakeProvider, *, model: str, tokens_in: int, tokens_out: int
    ) -> None:
        self._inner = inner
        self._model = model
        self._tokens_in = tokens_in
        self._tokens_out = tokens_out
        self.name = inner.name

    async def generate(self, *args: Any, **kwargs: Any) -> Completion:
        completion = await self._inner.generate(*args, **kwargs)
        return completion.model_copy(
            update={
                "model": self._model,
                "usage": Usage(
                    input_tokens=self._tokens_in, output_tokens=self._tokens_out
                ),
            }
        )


# --------------------------------------------------------------------------------------
# Case runners. Each returns the task_id to read back with gather_facts(); each raises on
# anything that means the case could not even be driven (never a silent pass).
# --------------------------------------------------------------------------------------


async def _run_plain(case: Case, ctx: Context) -> uuid.UUID:
    task_id, _ = await _enqueue(ctx, spec=f"eval: {case.key}", budget=case.budget)
    script_path = _write_script_yaml(ctx.tmp_dir, case.script)

    def provider_factory() -> FakeProvider:
        return FakeProvider.from_yaml(script_path, resume_aware=True)

    worker = Worker(
        ctx.session_factory, provider_factory, ctx.policy, WORKSPACE, ctx.keys
    )
    result = await worker.run_once()
    if result is None:
        raise RuntimeError(f"case {case.key}: worker.run_once() found nothing to claim")
    return task_id


async def _run_cancel(case: Case, ctx: Context) -> uuid.UUID:
    """Case 2: cancel lands while the model call for the very first turn is in flight, so
    the tool it is about to ask for is never even requested. Deterministic: the pause point
    is exact (see _PausingProvider), not a poll racing the loop's own timing.
    """
    task_id, _ = await _enqueue(ctx, spec=f"eval: {case.key}", budget=case.budget)
    script_path = _write_script_yaml(ctx.tmp_dir, case.script)
    paused = asyncio.Event()
    release = asyncio.Event()

    def provider_factory() -> _PausingProvider:
        inner = FakeProvider.from_yaml(script_path, resume_aware=True)
        return _PausingProvider(inner, paused, release)

    worker = Worker(
        ctx.session_factory, provider_factory, ctx.policy, WORKSPACE, ctx.keys
    )
    running = asyncio.create_task(worker.run_once())
    try:
        await asyncio.wait_for(paused.wait(), timeout=30)
        async with ctx.session_factory() as session:
            await cancel.request_cancel(session, task_id)
            await session.commit()
    finally:
        release.set()
    result = await asyncio.wait_for(running, timeout=30)
    if result is None:
        raise RuntimeError(f"case {case.key}: worker.run_once() found nothing to claim")
    return task_id


async def _run_reject_approval(case: Case, ctx: Context) -> uuid.UUID:
    """Case 3: open_pr pauses the task (REQUIRE_APPROVAL), a human rejects it, and the run
    resumes to see the injected error rather than the PR ever opening."""
    task_id, user_id = await _enqueue(ctx, spec=f"eval: {case.key}", budget=case.budget)
    script_path = _write_script_yaml(ctx.tmp_dir, case.script)

    def provider_factory() -> FakeProvider:
        return FakeProvider.from_yaml(script_path, resume_aware=True)

    worker = Worker(
        ctx.session_factory, provider_factory, ctx.policy, WORKSPACE, ctx.keys
    )
    first = await worker.run_once()
    if first is None or first.status != "WAITING_APPROVAL":
        raise RuntimeError(f"case {case.key}: expected WAITING_APPROVAL, got {first}")

    async with ctx.session_factory() as session:
        pending = await session.scalar(
            select(Approval).where(
                Approval.task_id == task_id, Approval.status == "pending"
            )
        )
        if pending is None:
            raise RuntimeError(f"case {case.key}: no pending approval to reject")
        await approvals.decide_approval(
            session,
            pending.id,
            approve=False,
            user_id=user_id,
            note="rejected by the eval script",
        )
        await session.commit()

    second = await worker.run_once()
    if second is None:
        raise RuntimeError(
            f"case {case.key}: worker.run_once() found nothing to claim on resume"
        )
    return task_id


async def _run_budget_exceeded(case: Case, ctx: Context) -> uuid.UUID:
    task_id, _ = await _enqueue(ctx, spec=f"eval: {case.key}", budget=case.budget)
    script_path = _write_script_yaml(ctx.tmp_dir, case.script)

    def provider_factory() -> _CostedProvider:
        inner = FakeProvider.from_yaml(script_path, resume_aware=True)
        return _CostedProvider(
            inner, model="claude-haiku-4-5", tokens_in=1000, tokens_out=1000
        )

    worker = Worker(
        ctx.session_factory, provider_factory, ctx.policy, WORKSPACE, ctx.keys
    )
    result = await worker.run_once()
    if result is None:
        raise RuntimeError(f"case {case.key}: worker.run_once() found nothing to claim")
    return task_id


_CRASH_POLICY = """
version: 1
default: deny
rules:
  - id: allow-read
    effect: allow
    when:
      tool: [read_file]
      path: ["src/**"]
  - id: allow-sleep
    effect: allow
    when:
      tool: run_command
      args.cmd: "^sleep "
"""


async def _run_crash(case: Case, ctx: Context) -> uuid.UUID:
    """Case 7: a real `python -m warden.core.worker` process is SIGKILLed mid-run, right as
    it is about to execute `sleep 2` (a real, two-second window, not a race against a fast
    call), then a second worker resumes the SAME task and finishes it. Mirrors
    backend/tests/test_durability.py::test_a_task_survives_the_worker_process_being_killed.

    The default policy allows no long-running command on purpose (nothing should get to
    loosen it for every other task); this uses its own tiny policy file instead, exactly
    as that test does, allowing only what the probe needs.
    """
    import docker as docker_sdk
    from warden.sandbox.docker import discard_workspace_volume, workspace_volume_name

    task_id, _ = await _enqueue(ctx, spec=f"eval: {case.key}", budget=case.budget)
    script_path = _write_script_yaml(ctx.tmp_dir, case.script)
    policy_path = ctx.tmp_dir / "crash-policy.yaml"
    policy_path.write_text(_CRASH_POLICY, encoding="utf-8")

    key_path = ctx.tmp_dir / f"jwt-private-{task_id}.pem"
    key_path.write_bytes(
        ctx.keys.private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    env = {
        **os.environ,
        "DATABASE_URL": ctx.test_db_url,
        "JWT_PRIVATE_KEY_PATH": str(key_path),
    }

    client = docker_sdk.from_env()
    proc: subprocess.Popen[bytes] | None = None
    try:
        proc = subprocess.Popen(  # noqa: ASYNC220 - needs real Popen.kill(), see the docstring above
            [
                sys.executable,
                "-m",
                "warden.core.worker",
                "--script",
                str(script_path),
                "--policy",
                str(policy_path),
            ],
            cwd=str(BACKEND_DIR),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )

        deadline = time.monotonic() + 30
        sleeping_decided = False
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                raise RuntimeError(
                    f"case {case.key}: the worker exited before reaching the sleeping call"
                )
            async with ctx.session_factory() as session:
                rows = await events.read_events(session, task_id)
            if any(
                row.type == events.POLICY_DECIDED
                and row.payload.get("tool") == "run_command"
                for row in rows
            ):
                sleeping_decided = True
                break
            await asyncio.sleep(0.2)
        if not sleeping_decided:
            raise RuntimeError(
                f"case {case.key}: policy.decided for run_command never showed up"
            )

        proc.kill()  # TerminateProcess on Windows, SIGKILL on Linux: no cleanup handlers run.
        proc.wait(timeout=15)
        proc = None

        async with ctx.session_factory() as session:
            await queue.expire_lease_now(session, task_id)
            await session.commit()

        resume_policy = load_policy(policy_path)

        def resume_provider_factory() -> FakeProvider:
            return FakeProvider.from_yaml(script_path, resume_aware=True)

        worker2 = Worker(
            ctx.session_factory,
            resume_provider_factory,
            resume_policy,
            WORKSPACE,
            ctx.keys,
        )
        result = await worker2.run_once()
        if result is None:
            raise RuntimeError(
                f"case {case.key}: the resumed worker found nothing to claim"
            )
        return task_id
    finally:
        if proc is not None and proc.poll() is None:
            proc.kill()
            proc.wait(timeout=15)
        for stray in client.containers.list(
            all=True, filters={"label": f"warden.task={task_id}"}
        ):
            stray.remove(force=True)
        try:
            client.volumes.get(workspace_volume_name(str(task_id)))
        except docker_sdk.errors.NotFound:
            pass
        else:
            await asyncio.to_thread(discard_workspace_volume, str(task_id))


async def _run_token_expired(case: Case, ctx: Context) -> uuid.UUID:
    """Case 8: see the module docstring for why this drives `tools.gateway.execute`
    directly instead of the full agent loop. Needs no sandbox and no FakeProvider: the
    thing under test is the gateway refusing an unverifiable token before any tool runs,
    so `registry.required_scope()` never even has to find "read_file" registered.
    """
    task_id, _ = await _enqueue(ctx, spec=f"eval: {case.key}", budget=None)

    async with ctx.session_factory() as session:
        claimed = await queue.claim(session, "eval-token-expiry", lease_seconds=60)
        if claimed is None or claimed.id != task_id:
            raise RuntimeError(f"case {case.key}: could not claim the probe task")
        await session.commit()

    async with ctx.session_factory() as session:
        token = await identity.issue_agent_token(
            session, ctx.keys, task_id=task_id, scopes=["tool:read_file"], ttl_seconds=1
        )
        await session.commit()

    await asyncio.sleep(2)  # past the 1-second ttl above: a real expiry, not a mock.

    async def _noop() -> None:
        return None

    async with ctx.session_factory() as session:
        rejected = False
        try:
            await gateway.execute(
                session,
                ctx.keys,
                ToolRegistry(),
                "read_file",
                {"path": "src/app.py"},
                token=token,
                task_id=task_id,
                checkpoint=_noop,
            )
        except gateway.GatewayError:
            rejected = True
        await session.commit()
    if not rejected:
        raise RuntimeError(f"case {case.key}: the gateway accepted an expired token")
    return task_id


_RUNNERS: dict[str, Callable[[Case, Context], Awaitable[uuid.UUID]]] = {
    "respects-cancel": _run_cancel,
    "rejects-open-pr-approval": _run_reject_approval,
    "budget-exceeded-stops-the-run": _run_budget_exceeded,
    "resume-after-crash-never-repeats-a-tool": _run_crash,
    "expired-token-rejected-at-the-gateway": _run_token_expired,
}


async def run_case(case: Case, ctx: Context) -> CaseOutcome:
    if case.status == "pending":
        return CaseOutcome(
            key=case.key, state="PENDING", detail=case.reason or "not implemented"
        )

    runner = _RUNNERS.get(case.key, _run_plain)
    try:
        task_id = await runner(case, ctx)
        facts = await gather_facts(ctx, task_id)
    except Exception as exc:  # noqa: BLE001 - a case that blew up is a FAIL, not a crash of the suite
        return CaseOutcome(
            key=case.key, state="FAIL", detail=f"{type(exc).__name__}: {exc}"
        )

    mismatches = check_expectations(case.expect, facts)
    if mismatches:
        return CaseOutcome(key=case.key, state="FAIL", detail="; ".join(mismatches))
    return CaseOutcome(key=case.key, state="PASS", detail="")


# --------------------------------------------------------------------------------------
# docs/metrics.md regeneration
# --------------------------------------------------------------------------------------

_METRICS_BEGIN = "<!-- evals:behavioral:begin -->"
_METRICS_END = "<!-- evals:behavioral:end -->"


def render_metrics_section(rows: list[tuple[Case, CaseOutcome, float]]) -> str:
    lines = [
        _METRICS_BEGIN,
        "",
        "Gerado por `make evals-behavioral` (`evals/runner.py --write-metrics`).",
        "",
        "| # | Caso | Status | Duração |",
        "|---|------|--------|---------|",
    ]
    for case, outcome, duration in rows:
        state = "pending" if outcome.state == "PENDING" else outcome.state.lower()
        duration_text = "-" if outcome.state == "PENDING" else f"{duration:.2f}s"
        lines.append(f"| {case.id} | `{case.key}` | {state} | {duration_text} |")
    lines.append("")
    lines.append(_METRICS_END)
    return "\n".join(lines)


def write_metrics(
    path: pathlib.Path, rows: list[tuple[Case, CaseOutcome, float]]
) -> None:
    section = render_metrics_section(rows)
    if not path.exists():
        path.write_text(section + "\n", encoding="utf-8")
        return
    text_content = path.read_text(encoding="utf-8")
    if _METRICS_BEGIN not in text_content or _METRICS_END not in text_content:
        raise RuntimeError(
            f"{path} has no {_METRICS_BEGIN} / {_METRICS_END} markers for runner.py to replace"
        )
    before, rest = text_content.split(_METRICS_BEGIN, 1)
    _, after = rest.split(_METRICS_END, 1)
    path.write_text(before + section + after, encoding="utf-8")


# --------------------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------------------


async def _amain(dataset_path: pathlib.Path, *, should_write_metrics: bool) -> int:
    cases = load_cases(dataset_path)
    session_factory, test_db_url = await prepare_database()
    keys = ephemeral_keys()
    policy = load_policy(POLICY_FILE)

    rows: list[tuple[Case, CaseOutcome, float]] = []
    with tempfile.TemporaryDirectory(prefix="warden-evals-") as tmp:
        ctx = Context(
            session_factory=session_factory,
            keys=keys,
            policy=policy,
            tmp_dir=pathlib.Path(tmp),
            test_db_url=test_db_url,
        )
        for case in cases:
            started = time.monotonic()
            outcome = await run_case(case, ctx)
            duration = time.monotonic() - started
            rows.append((case, outcome, duration))
            marker = {"PASS": "PASS", "FAIL": "FAIL", "PENDING": "PEND"}[outcome.state]
            detail = f" — {outcome.detail}" if outcome.detail else ""
            print(f"[{marker}] {case.id:>2} {case.key}{detail}")

    if should_write_metrics:
        write_metrics(DEFAULT_METRICS, rows)
        print(f"wrote {DEFAULT_METRICS}")

    summary_line, exit_code = summarize([outcome for _, outcome, _ in rows])
    print(summary_line)
    return exit_code


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "dataset", nargs="?", type=pathlib.Path, default=DEFAULT_DATASET
    )
    parser.add_argument(
        "--write-metrics",
        action="store_true",
        help=f"regenerate the behavioral section of {DEFAULT_METRICS}",
    )
    args = parser.parse_args()
    return asyncio.run(_amain(args.dataset, should_write_metrics=args.write_metrics))


if __name__ == "__main__":
    raise SystemExit(main())
