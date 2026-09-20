"""Runs one task end to end and prints the events the database recorded.

Two providers, one code path. `--provider fake` replays a script and costs nothing;
`--provider anthropic` calls the real API and spends money, which is why the budget
ceiling below is low and explicit.

Week 1 runs the loop in this process. Week 2 moves it behind the queue and a worker.
"""

import argparse
import asyncio
import pathlib
import sys
from decimal import Decimal
from uuid import uuid4

from warden.config import get_settings
from warden.core import events
from warden.core.loop import Budget, run_task
from warden.db import make_engine, make_session_factory
from warden.models import Task as TaskRow
from warden.models import User
from warden.policy.engine import load_policy
from warden.providers.base import ModelProvider
from warden.tools.local import build_registry

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
WORKSPACE = REPO_ROOT / "examples" / "target-repo"
FAKE_SCRIPT = REPO_ROOT / "examples" / "demo-script.yaml"
POLICY = REPO_ROOT / "policies" / "default.yaml"

SPEC = "List the files in this repository and summarise what the project does."

# Deliberately low. The demo task against a four-endpoint repo should cost well under a
# cent; if a run gets near this ceiling something is wrong, and the ceiling is what turns
# that into a visible failure rather than an invoice.
DEMO_BUDGET = Budget(max_iterations=8, max_usd=Decimal("0.25"))


def build_provider(kind: str) -> ModelProvider:
    if kind == "fake":
        from warden.providers.fake import FakeProvider

        return FakeProvider.from_yaml(FAKE_SCRIPT)

    from anthropic import AsyncAnthropic

    from warden.providers.anthropic import AnthropicProvider

    api_key = get_settings().anthropic_api_key
    if not api_key:
        raise SystemExit(
            "ANTHROPIC_API_KEY is not set. Put it in the .env file at the repository root, "
            "or run `make demo-fake`, which needs no key and costs nothing."
        )
    return AnthropicProvider(AsyncAnthropic(api_key=api_key))


async def main(kind: str) -> int:
    provider = build_provider(kind)
    engine = make_engine(get_settings().database_url)
    session_factory = make_session_factory(engine)

    async with session_factory() as session:
        # A demo user per run keeps the unique email constraint honest without needing
        # fixtures or a login flow, neither of which exists yet.
        user = User(email=f"demo-{uuid4()}@warden.local", password_hash="demo", role="submitter")
        session.add(user)
        await session.flush()

        task = TaskRow(
            idempotency_key=f"demo-{uuid4()}",
            user_id=user.id,
            spec=SPEC,
            target_repo=str(WORKSPACE),
        )
        session.add(task)
        await session.flush()

        registry = build_registry(WORKSPACE)
        policy = load_policy(POLICY)
        print(f"task {task.id}  provider={provider.name}  workspace={WORKSPACE}")
        print(f"policy {POLICY.name}  hash={policy.policy_hash[:12]}\n")

        result = await run_task(
            session, task, provider, registry, policy, workspace=WORKSPACE, budget=DEMO_BUDGET
        )
        await session.commit()

        # Read back from the database rather than from memory: the point of the demo is
        # showing that the event log is what happened, not that the loop says so.
        recorded = await events.read_events(session, task.id)
        print("events recorded in the database:")
        for event in recorded:
            print(f"  {event.seq:>3}  {event.type:<20} {event.payload}")

        print(f"\nstatus     {result.status}")
        print(f"iterations {result.iterations}")
        print(f"cost       ${result.cost_usd}")
        if result.summary:
            print(f"summary    {result.summary.strip()[:500]}")
        if result.reason:
            print(f"reason     {result.reason}")

    await engine.dispose()
    return 0 if result.status == "SUCCEEDED" else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provider", choices=["fake", "anthropic"], default="fake")
    sys.exit(asyncio.run(main(parser.parse_args().provider)))
