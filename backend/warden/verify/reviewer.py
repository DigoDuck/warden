"""The independent reviewer and the definition of success (ADR-010).

Skeleton: signatures only, filled in by the commits that follow.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from pydantic import BaseModel

from warden.providers.base import Completion, ModelProvider

VERDICT_TOOL = "submit_verdict"
# The evidence kinds that can fail a task. `diff` is a fact, not a check, so it never gates.
GATING_KINDS = ("lint", "types", "tests")


class VerdictPayload(BaseModel):
    passed: bool
    findings: list[str]


@dataclass(frozen=True)
class Review:
    completion: Completion
    verdict: VerdictPayload | None
    malformed_reason: str | None


@dataclass(frozen=True)
class Resolution:
    status: str
    reason: str | None


class Reviewer(Protocol):
    async def review(self, spec: str, evidence: Mapping[str, Mapping[str, Any]]) -> Review: ...


class ProviderReviewer:
    def __init__(self, provider: ModelProvider) -> None:
        self._provider = provider

    async def review(self, spec: str, evidence: Mapping[str, Mapping[str, Any]]) -> Review:
        raise NotImplementedError


def decide(
    passed: bool, malformed_reason: str | None, evidence: Mapping[str, Mapping[str, Any]]
) -> Resolution:
    raise NotImplementedError
