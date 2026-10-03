"""Runs the `coding_v1` capability evals (briefing §19/§46-48): each item is a REAL task
through the production path (queue, a real `Worker`, the real sandbox, policy, verification
and verdict) with a real model, followed by that item's HIDDEN acceptance test run against
the agent's final workspace.

What this adds on top of the behavioral runner (evals/runner.py, whose bootstrap it reuses):

  - the worker keeps the task's workspace volume (`keep_workspaces=True`), so the hidden test
    can run against exactly what the agent left;
  - the hidden test runs in a fresh sandbox attached to that volume, with a fixed argv that
    an agent-planted file cannot hijack (see `HIDDEN_TEST_PROGRAM`);
  - the outcome is read back from the database, classified (evals/coding_checks.py) and, for
    a real provider only, published to docs/metrics.md.

Usage: `uv run --project backend python -m evals.coding --provider anthropic --write-metrics`
(needs ANTHROPIC_API_KEY, spends money, capped by --max-usd-total) or `--provider fake`
(replays evals/datasets/coding_v1/*.fake.yaml: no key, no cost, nothing is ever published).
"""

import argparse
import asyncio
import datetime
import os
import pathlib
import subprocess
import sys
import tempfile
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import docker
import docker.errors
from sqlalchemy import func, select
from warden.config import get_settings
from warden.core import events
from warden.core.loop import Budget
from warden.core.worker import POLICY_FILE, TERMINAL_STATUSES, WORKSPACE, Worker
from warden.models import Evidence, ModelCall, Task, Verdict
from warden.policy.engine import load_policy
from warden.providers.base import ModelProvider
from warden.providers.fake import FakeProvider
from warden.sandbox.docker import (
    WORKSPACE as SANDBOX_WORKSPACE,
)
from warden.sandbox.docker import (
    CommandTimeout,
    Sandbox,
    SandboxError,
    SandboxProfile,
    discard_workspace_volume,
    workspace_volume_name,
)
from warden.tools.sandboxed import truncate_output

from evals import runner
from evals.coding_checks import (
    Item,
    ItemResult,
    RunFacts,
    classify_failure,
    forbidden_attempts,
    gating_green,
    is_escaped_defect,
    is_success,
    load_items,
    summarize_items,
    write_coding_metrics,
)

REPO_ROOT = runner.REPO_ROOT
DEFAULT_DATASET = REPO_ROOT / "evals" / "datasets" / "coding_v1.yaml"
DEFAULT_METRICS = REPO_ROOT / "docs" / "metrics.md"
# Its own database, not runner.py's "warden_evals" nor the backend suite's "warden_test":
# prepare_database drops and recreates it, so a shared name would let two runs destroy each
# other (and `make evals-behavioral` would clobber a coding run).
TEST_DB = os.environ.get("WARDEN_TEST_DB", "warden_evals_coding")

# Per-task ceilings (spec §7). The worker's own budget, not the task's: `merge_budget` only
# narrows a worker ceiling, and these ARE the ceiling.
DEFAULT_MAX_USD = Decimal("1.00")
DEFAULT_MAX_SECONDS = 900.0
DEFAULT_MAX_ITERATIONS = 30
# Stops STARTING new items once the running total reaches it. An item that is already running
# can overshoot by at most its own max_usd.
DEFAULT_MAX_USD_TOTAL = Decimal("5.00")

HIDDEN_DIR = ".warden_hidden"
HIDDEN_TIMEOUT_SECONDS = 120.0

# The hidden test's argv, fixed and never built from anything the model produced. The test
# file name is the only variable part and it arrives as sys.argv[1] from the dataset, not
# interpolated into the program. Each piece exists because the agent controls the workspace
# the test runs in (it can write files anywhere in the volume, e.g. through a test it wrote
# and then ran with `run_command pytest`):
#
#   python -I      isolated mode drops the cwd from sys.path. Without it `python -c` puts the
#                  workspace first, and a planted `pytest.py` would be imported instead of
#                  the real pytest and could print "1 passed" and exit 0.
#   sys.path.append  the workspace goes at the END: `import src.app` (a namespace package)
#                  still resolves, but nothing in the workspace can shadow an installed
#                  package such as fastapi or httpx.
#   --noconftest   a planted conftest.py could rewrite every result to "passed".
#   -c /dev/null   an empty ini file. Left alone, pytest finds the workspace's pyproject.toml
#                  and obeys its `pythonpath = ["."]`, which PUTS THE WORKSPACE FIRST on
#                  sys.path again and undoes the append above. The agent can edit that file.
#   -p no:cacheprovider   writes nothing into the workspace.
#
# One invocation per item: the app keeps its state in module globals, so two hidden tests in
# one process contaminate each other (evals/datasets/target_repo/README.md).
#
# What this still does not catch: a planted `sitecustomize.py`/`usercustomize.py` or `.pth`
# file in an installed location is out of reach here (the volume is only the workspace); code
# the agent put in `src/` runs with the test's privileges, which is the point of the test; and
# the test judges behaviour, not style or minimality (that is what the reviewer is for).
HIDDEN_TEST_PROGRAM = (
    "import sys; "
    f"sys.path.append({SANDBOX_WORKSPACE!r}); "
    "import pytest; "
    "sys.exit(pytest.main(['-q', '-p', 'no:cacheprovider', '--noconftest', "
    "'-c', '/dev/null', sys.argv[1]]))"
)


def hidden_test_argv(test_name: str) -> list[str]:
    return ["python", "-I", "-c", HIDDEN_TEST_PROGRAM, f"{HIDDEN_DIR}/{test_name}"]


# --------------------------------------------------------------------------------------
# GitHub off
# --------------------------------------------------------------------------------------


def isolate_coding_github() -> None:
    """Make the coding run unable to open a pull request.

    The agent never has `github.open_pr` (ADR-028); the control plane's publish phase does,
    and only when a repo AND a token are set (`build_publish_registry`). Empty values mean
    there is no publish phase at all, so a green run here never proposes a pull request. Environment variables win over .env in pydantic-settings, so this holds
    whatever the shell or the file exports. The API URL points at a loopback port nothing
    listens on, the same belt-and-braces runner.isolate_github uses: even a regression that
    registered the tool would get a refused connection, not a real PR. Differs from
    runner.isolate_github on purpose: that one configures GitHub (case 3 needs it to test
    the approval gate), this one must not.
    """
    os.environ.update(
        {"GITHUB_REPO": "", "GITHUB_TOKEN": "", "GITHUB_API_URL": "http://127.0.0.1:9"}
    )
    get_settings.cache_clear()


# --------------------------------------------------------------------------------------
# The hidden test
# --------------------------------------------------------------------------------------


@dataclass
class HiddenResult:
    passed: bool
    exit_code: int | None
    output: str


async def exec_hidden_test(sandbox: Sandbox, item: Item) -> HiddenResult:
    """Copy the item's hidden test into the sandbox's workspace and run it (see
    HIDDEN_TEST_PROGRAM). The copy goes through the daemon's archive API, never through a
    command, and overwrites whatever the agent may have left at that path."""
    name = item.hidden_test.name
    await sandbox.put_file(
        f"workspace/{HIDDEN_DIR}/{name}", item.hidden_test.read_bytes()
    )
    try:
        result = await sandbox.exec(
            hidden_test_argv(name), kill_after=HIDDEN_TIMEOUT_SECONDS
        )
    except CommandTimeout as exc:
        # A hidden test that does not finish is a failed one; the container was killed.
        return HiddenResult(passed=False, exit_code=None, output=str(exc))
    return HiddenResult(
        passed=result.exit_code == 0,
        exit_code=result.exit_code,
        output=truncate_output(result.output),
    )


async def run_hidden_test(task_id: uuid.UUID, item: Item) -> HiddenResult:
    """Run the hidden test on the task's own workspace volume, in a fresh container.

    Refuses to run when the volume is gone: `Sandbox.create` would silently make a NEW volume
    from the pristine target repo, and a hidden test run against the baseline instead of the
    agent's work would be a wrong answer that looks like a real one.
    """
    client = docker.from_env()
    try:
        await asyncio.to_thread(client.volumes.get, workspace_volume_name(str(task_id)))
    except docker.errors.NotFound as exc:
        raise SandboxError(
            f"workspace volume of task {task_id} is gone; the worker must keep it "
            "(keep_workspaces=True) until the hidden test has run"
        ) from exc
    sandbox = await Sandbox.create(SandboxProfile(), WORKSPACE, task_id=str(task_id))
    try:
        return await exec_hidden_test(sandbox, item)
    finally:
        await sandbox.destroy()


# --------------------------------------------------------------------------------------
# One item
# --------------------------------------------------------------------------------------


async def run_agent(
    item: Item,
    ctx: runner.Context,
    provider_factory: Callable[[], ModelProvider],
    budget: Budget,
) -> uuid.UUID:
    """Enqueue the item's issue as a task and let a real Worker run it to a terminal state.
    Leaves the workspace volume in place: the caller owns it and must discard it."""
    task_id, _ = await runner._enqueue(
        ctx, spec=item.issue.read_text(encoding="utf-8"), budget=None
    )
    worker = Worker(
        ctx.session_factory,
        provider_factory,
        ctx.policy,
        WORKSPACE,
        ctx.keys,
        budget=budget,
        keep_workspaces=True,
    )
    if await worker.run_once() is None:
        raise RuntimeError(f"{item.id}: worker.run_once() found nothing to claim")
    return task_id


@dataclass
class _Readback:
    facts: RunFacts
    cost_usd: Decimal
    tokens_in: int
    tokens_out: int
    latency_s: float | None
    iterations: int
    tool_calls: int


async def read_back(ctx: runner.Context, task_id: uuid.UUID) -> _Readback:
    """What the run did, straight from the database. The control plane's own record is the
    evidence; nothing here trusts the caller's bookkeeping."""
    async with ctx.session_factory() as session:
        task = await session.get(Task, task_id)
        assert task is not None

        rows = await events.read_events(session, task_id)
        finished = next(
            (r for r in reversed(rows) if r.type == events.TASK_FINISHED), None
        )
        reason = finished.payload.get("reason") if finished else None
        iterations = sum(1 for r in rows if r.type == events.ITERATION_STARTED)
        # Every call the model MADE, from `tool.requested`, joined with what the policy
        # decided about it. Not the `tool_calls` table: a call that parks the task for
        # approval has no row there until a human answers, and this has to see every call.
        decided = {
            str(r.payload["id"]): str(r.payload["effect"])
            for r in rows
            if r.type == events.POLICY_DECIDED
        }
        tool_calls: list[dict[str, Any]] = [
            {
                "tool": r.payload["tool"],
                "decision": decided.get(str(r.payload["id"])),
                "args": r.payload.get("arguments") or {},
            }
            for r in rows
            if r.type == events.TOOL_REQUESTED
        ]

        evidence = {
            e.kind: dict(e.payload)
            for e in await session.scalars(
                select(Evidence).where(Evidence.task_id == task_id)
            )
        }
        diff = evidence.get("diff")
        diff_files = (
            [str(f["path"]) for f in diff.get("files", [])]
            if diff is not None and diff.get("status") == "ok"
            else None
        )
        verdict = await session.scalar(
            select(Verdict).where(Verdict.task_id == task_id)
        )

        # Every model call, the reviewer's included: what the item cost is what was spent.
        cost, tokens_in, tokens_out = (
            await session.execute(
                select(
                    func.coalesce(func.sum(ModelCall.cost_usd), 0),
                    func.coalesce(func.sum(ModelCall.tokens_in), 0),
                    func.coalesce(func.sum(ModelCall.tokens_out), 0),
                ).where(ModelCall.task_id == task_id)
            )
        ).one()

        latency = (
            (task.finished_at - task.started_at).total_seconds()
            if task.finished_at and task.started_at
            else None
        )
        return _Readback(
            facts=RunFacts(
                status=task.status,
                reason=reason,
                verdict_passed=verdict.passed if verdict else None,
                gating={
                    k: str(evidence[k].get("status"))
                    for k in ("lint", "types", "tests")
                    if k in evidence
                },
                diff_files=diff_files,
                tool_calls=tool_calls,
            ),
            cost_usd=Decimal(cost),
            tokens_in=int(tokens_in),
            tokens_out=int(tokens_out),
            latency_s=latency,
            iterations=iterations,
            # `finish` never emits `tool.requested`, so this is the tools the agent used.
            tool_calls=len(tool_calls),
        )


async def run_item(
    item: Item,
    ctx: runner.Context,
    provider_factory: Callable[[], ModelProvider],
    budget: Budget,
) -> ItemResult:
    task_id: uuid.UUID | None = None
    try:
        task_id = await run_agent(item, ctx, provider_factory, budget)
        back = await read_back(ctx, task_id)
        if back.facts.status not in TERMINAL_STATUSES:
            # Nobody approves anything in a capability eval. A task parked in WAITING_APPROVAL
            # (a policy rule escalated one of the model's calls) never finished, so its
            # workspace is half done: scoring it would be a failure the model never earned.
            raise RuntimeError(
                f"task ended the run in {back.facts.status}, not terminal"
            )
        hidden = await run_hidden_test(task_id, item)
    except Exception as exc:  # noqa: BLE001 - a harness/provider failure is reported, never scored
        return ItemResult(
            id=item.id, state="error", detail=f"{type(exc).__name__}: {exc}"
        )
    finally:
        if task_id is not None:
            # The worker kept it for the hidden test (keep_workspaces); the runner owns it now.
            await asyncio.to_thread(discard_workspace_volume, str(task_id))

    facts = back.facts
    green = gating_green(facts.gating)
    success = is_success(facts.status, hidden.passed)
    return ItemResult(
        id=item.id,
        state="done",
        detail=_last_line(hidden.output),
        status=facts.status,
        verdict_passed=facts.verdict_passed,
        gating_green=green,
        hidden_passed=hidden.passed,
        success=success,
        escaped_defect=is_escaped_defect(facts.verdict_passed, green, hidden.passed),
        cost_usd=back.cost_usd,
        tokens_in=back.tokens_in,
        tokens_out=back.tokens_out,
        latency_s=back.latency_s,
        iterations=back.iterations,
        tool_calls=back.tool_calls,
        forbidden_attempts=forbidden_attempts(item, facts.tool_calls),
        failure_category=classify_failure(item, facts, hidden.passed),
    )


def _last_line(output: str) -> str:
    lines = [line for line in output.strip().splitlines() if line.strip()]
    return lines[-1] if lines else ""


# --------------------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------------------


def provider_factories(
    kind: str,
) -> Callable[[Item], Callable[[], ModelProvider] | None]:
    """How each item gets its model. The returned function gives None for "this item cannot
    run with this provider" (a fake item without a script). Raises SystemExit up front, before
    anything touches the database or the daemon, when the real provider has no key."""
    if kind == "fake":

        def fake(item: Item) -> Callable[[], ModelProvider] | None:
            script = item.fake_script
            if script is None:
                return None
            return lambda: FakeProvider.from_yaml(script, resume_aware=True)

        return fake

    from anthropic import AsyncAnthropic
    from warden.providers.anthropic import AnthropicProvider

    api_key = get_settings().anthropic_api_key
    if not api_key:
        # Same failure as demo.py, and the same rule: never fall back to something else.
        raise SystemExit(
            "ANTHROPIC_API_KEY is not set. Put it in the .env file at the repository root, "
            "or run `make evals-coding PROVIDER=fake`, which needs no key and costs nothing."
        )
    provider = AnthropicProvider(AsyncAnthropic(api_key=api_key))
    return lambda item: lambda: provider


def _git_sha() -> str:
    out = subprocess.run(
        ["git", "rev-parse", "--short", "HEAD"],
        cwd=REPO_ROOT,
        capture_output=True,
        check=False,
        text=True,
    )
    return out.stdout.strip() or "unknown"


def _print_result(result: ItemResult) -> None:
    if result.state != "done":
        print(f"[{result.state.upper():<4}] {result.id} - {result.detail}")
        return
    verdict = "success" if result.success else f"FAIL ({result.failure_category})"
    print(
        f"[{'PASS' if result.success else 'FAIL'}] {result.id} {verdict} "
        f"task={result.status} hidden={'pass' if result.hidden_passed else 'fail'} "
        f"cost=${result.cost_usd:.4f} iters={result.iterations} "
        f"escaped_defect={result.escaped_defect} - {result.detail}"
    )


async def _amain(args: argparse.Namespace) -> int:
    isolate_coding_github()  # before anything builds a registry
    items = load_items(args.dataset, repo_root=REPO_ROOT)
    if args.only:
        wanted = set(args.only.split(","))
        unknown = wanted - {i.id for i in items}
        if unknown:
            raise SystemExit(f"--only names unknown item(s): {sorted(unknown)}")
        items = [i for i in items if i.id in wanted]
    if args.write_metrics and (args.provider == "fake" or args.only):
        # Fake numbers are scripted, and a subset is not the dataset: neither is a measurement.
        raise SystemExit(
            "--write-metrics needs the anthropic provider and the whole dataset"
        )

    factory_for = provider_factories(args.provider)

    # runner.prepare_database reads runner.TEST_DB; this track must not drop the behavioral
    # evals' database (and cannot edit runner.py), so point it at ours first.
    runner.TEST_DB = TEST_DB
    session_factory, test_db_url = await runner.prepare_database()
    ctx_keys = runner.ephemeral_keys()
    policy = load_policy(POLICY_FILE)
    budget = Budget(
        max_iterations=args.max_iterations,
        max_usd=Decimal(str(args.max_usd)),
        max_seconds=args.max_seconds,
    )
    total_cap = Decimal(str(args.max_usd_total))

    results: list[ItemResult] = []
    spent = Decimal(0)
    with tempfile.TemporaryDirectory(prefix="warden-coding-evals-") as tmp:
        ctx = runner.Context(
            session_factory=session_factory,
            keys=ctx_keys,
            policy=policy,
            tmp_dir=pathlib.Path(tmp),
            test_db_url=test_db_url,
        )
        for item in items:
            factory = factory_for(item)
            if factory is None:
                result = ItemResult(
                    id=item.id, state="skipped", detail="sem fake script"
                )
            elif spent >= total_cap:
                result = ItemResult(id=item.id, state="skipped", detail="budget cap")
            else:
                started = time.monotonic()
                result = await run_item(item, ctx, factory, budget)
                result.detail = result.detail or f"{time.monotonic() - started:.0f}s"
                spent += result.cost_usd
            results.append(result)
            _print_result(result)

    summary = summarize_items(results)
    rate = "n/a" if summary.success_rate is None else f"{summary.success_rate:.0%}"
    print(
        f"{summary.successes}/{summary.executed} succeeded ({rate}), {summary.skipped} skipped, "
        f"cost ${summary.cost_total:.4f}, escaped defects {summary.escaped_defects}"
    )
    if args.write_metrics:
        write_coding_metrics(
            DEFAULT_METRICS,
            results,
            provider=args.provider,
            model=_model_name(args.provider),
            sha=_git_sha(),
            date=datetime.datetime.now(tz=datetime.UTC).date().isoformat(),
            dataset=args.dataset.stem,
        )
        print(f"wrote {DEFAULT_METRICS}")
    # A model failing an item is a result, not a CLI failure. A harness error, or a run that
    # scored nothing, is.
    return 1 if summary.executed == 0 or any(r.state == "error" for r in results) else 0


def _model_name(provider: str) -> str:
    if provider == "fake":
        return "fake-model"
    from warden.providers.anthropic import DEFAULT_MODEL

    return DEFAULT_MODEL


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "dataset", nargs="?", type=pathlib.Path, default=DEFAULT_DATASET
    )
    parser.add_argument(
        "--provider", choices=["anthropic", "fake"], default="anthropic"
    )
    parser.add_argument(
        "--write-metrics", action="store_true", help=f"publish to {DEFAULT_METRICS}"
    )
    parser.add_argument("--only", help="comma-separated item ids (never publishable)")
    parser.add_argument(
        "--max-usd", type=Decimal, default=DEFAULT_MAX_USD, help="per task"
    )
    parser.add_argument(
        "--max-seconds", type=float, default=DEFAULT_MAX_SECONDS, help="per task"
    )
    parser.add_argument(
        "--max-iterations", type=int, default=DEFAULT_MAX_ITERATIONS, help="per task"
    )
    parser.add_argument(
        "--max-usd-total",
        type=Decimal,
        default=DEFAULT_MAX_USD_TOTAL,
        help="stop starting new items once the running total reaches this",
    )
    return asyncio.run(_amain(parser.parse_args()))


if __name__ == "__main__":
    sys.exit(main())
