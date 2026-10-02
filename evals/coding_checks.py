"""Pure, DB-free pieces of the coding capability evals (`coding_v1`): the dataset loader, the
failure classifier, the summary math and the docs/metrics.md writer.

Same split as checks.py/runner.py: the part with branching logic lives here and has fast unit
tests (evals/tests/test_coding_checks.py); evals/coding.py talks to Postgres and Docker.
"""

import pathlib
from collections.abc import Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any


class DatasetError(ValueError):
    """The dataset file is malformed. Raised at load time so a typo never turns into a
    silently weaker eval."""


@dataclass(frozen=True)
class Forbidden:
    tool: str
    path: str | None


@dataclass(frozen=True)
class Item:
    id: str
    issue: pathlib.Path
    hidden_test: pathlib.Path
    kind: str
    expected_files: tuple[str, ...]
    forbidden: tuple[Forbidden, ...]
    fake_script: pathlib.Path | None


def load_items(path: pathlib.Path, *, repo_root: pathlib.Path) -> list[Item]:
    raise NotImplementedError


@dataclass
class RunFacts:
    status: str
    reason: str | None
    verdict_passed: bool | None
    gating: dict[str, str]
    diff_files: list[str] | None
    tool_calls: list[dict[str, Any]] = field(default_factory=list)


KNOWN_TOOLS: frozenset[str] = frozenset()
CATEGORIES: tuple[str, ...] = ()


def gating_green(gating: dict[str, str]) -> bool:
    raise NotImplementedError


def forbidden_attempts(item: Item, tool_calls: Sequence[dict[str, Any]]) -> int:
    raise NotImplementedError


def unknown_tools(tool_calls: Sequence[dict[str, Any]]) -> list[str]:
    raise NotImplementedError


def is_success(status: str, hidden_passed: bool) -> bool:
    raise NotImplementedError


def is_escaped_defect(verdict_passed: bool | None, green: bool, hidden_passed: bool) -> bool:
    raise NotImplementedError


def classify_failure(item: Item, facts: RunFacts, hidden_passed: bool) -> str | None:
    raise NotImplementedError


@dataclass
class ItemResult:
    id: str
    state: str  # "done" | "skipped" | "error"
    detail: str = ""
    status: str = ""
    verdict_passed: bool | None = None
    gating_green: bool = False
    hidden_passed: bool = False
    success: bool = False
    escaped_defect: bool = False
    cost_usd: Decimal = Decimal("0")
    tokens_in: int = 0
    tokens_out: int = 0
    latency_s: float | None = None
    iterations: int = 0
    tool_calls: int = 0
    forbidden_attempts: int = 0
    failure_category: str | None = None


@dataclass(frozen=True)
class Summary:
    executed: int
    skipped: int
    successes: int
    success_rate: float | None
    cost_total: Decimal
    cost_per_task: Decimal | None
    cost_per_success: Decimal | None
    latency_p50: float | None
    latency_p95: float | None
    escaped_defects: int


def percentile(values: Sequence[float], p: int) -> float | None:
    raise NotImplementedError


def summarize_items(results: Sequence[ItemResult]) -> Summary:
    raise NotImplementedError


METRICS_BEGIN = "<!-- evals:coding:begin -->"
METRICS_END = "<!-- evals:coding:end -->"


def write_coding_metrics(
    path: pathlib.Path,
    results: Sequence[ItemResult],
    *,
    provider: str,
    model: str,
    sha: str,
    date: str,
    dataset: str,
) -> None:
    raise NotImplementedError
