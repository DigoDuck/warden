"""The planner: an advisory plan before the coder starts (ADR-031).

SKELETON: the shapes exist so the tests can fail on behaviour, not on imports.
"""

from dataclasses import dataclass
from typing import Any, Protocol

from pydantic import BaseModel

from warden.providers.base import Completion, ModelProvider, Usage

PLAN_TOOL = "submit_plan"
PLANNER_SYSTEM_PROMPT = ""


class PlanPayload(BaseModel):
    steps: list[str]
    likely_files: list[str]
    risks: list[str]
    tests_to_add: list[str]


@dataclass(frozen=True)
class PlanResult:
    completion: Completion
    plan: PlanPayload | None
    malformed_reason: str | None


class Planner(Protocol):
    async def plan(self, spec: str) -> PlanResult: ...


def plan_message(plan: dict[str, Any]) -> str:
    return ""


class ProviderPlanner:
    def __init__(self, provider: ModelProvider) -> None:
        self._provider = provider

    async def plan(self, spec: str) -> PlanResult:
        completion = Completion(
            provider="skeleton", model="skeleton", stop_reason="end_turn", usage=Usage()
        )
        return PlanResult(completion, None, "not implemented")
