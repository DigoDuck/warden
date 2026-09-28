"""Pure, DB-free pieces of the eval runner: turn a case's `expect` block and the facts a run
produced into pass/fail, and turn a batch of outcomes into the summary line and exit code.

Kept separate from runner.py (which talks to Postgres, Docker and a subprocess) so the part
with actual branching logic has fast unit tests (evals/tests/test_checks.py) instead of only
ever being exercised end to end.
"""

from dataclasses import dataclass, field


@dataclass
class Facts:
    """What a case run looked like, read back from the database. runner.py builds one of
    these per case; check_expectations only ever sees this shape, never a session."""

    policy_effects: list[str] = field(default_factory=list)
    task_status: str = ""
    tool_executed_count: int = 0
    approvals_pending: int = 0
    audit_corpus: list[str] = field(default_factory=list)
    # One dict per tool_calls row, in iteration order: {"tool", "decision", "args_safe"}.
    tool_calls: list[dict[str, object]] = field(default_factory=list)


@dataclass
class CaseOutcome:
    key: str
    state: str  # "PASS" | "FAIL" | "PENDING"
    detail: str


# skeleton: not yet implemented (TDD red step).
def check_expectations(expect: dict[str, object], facts: Facts) -> list[str]:
    raise NotImplementedError


def summarize(outcomes: list[CaseOutcome]) -> tuple[str, int]:
    raise NotImplementedError
