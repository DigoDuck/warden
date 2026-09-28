"""Pure, DB-free pieces of the eval runner: turn a case's `expect` block and the facts a run
produced into pass/fail, and turn a batch of outcomes into the summary line and exit code.

Kept separate from runner.py (which talks to Postgres, Docker and a subprocess) so the part
with actual branching logic has fast unit tests (evals/tests/test_checks.py) instead of only
ever being exercised end to end.
"""

from dataclasses import dataclass, field
from typing import cast


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


def _mismatch(field_name: str, expected: object, actual: object) -> str:
    return f"{field_name}: expected {expected!r}, got {actual!r}"


def _check_tool_calls(expected: list[dict[str, object]], facts: Facts) -> list[str]:
    """Match each expected entry against the FIRST tool_calls row for that tool name.

    First, not "the" row: a case that calls the same tool twice on purpose is rare enough
    (none of the 9 runnable cases do) that indexing by first occurrence is simpler than a
    positional or exhaustive match, and it is what the two cases that need this
    (open_pr rejection, secret redaction) actually mean by "the read_file call".
    """
    mismatches: list[str] = []
    by_tool: dict[str, dict[str, object]] = {}
    for entry in facts.tool_calls:
        by_tool.setdefault(str(entry["tool"]), entry)

    for spec in expected:
        tool = str(spec["tool"])
        row: dict[str, object] | None = by_tool.get(tool)
        if row is None:
            mismatches.append(f"tool_calls: no tool_calls row for tool {tool!r}")
            continue
        if "decision" in spec and row.get("decision") != spec["decision"]:
            mismatches.append(
                f"tool_calls[{tool}].decision: expected {spec['decision']!r}, "
                f"got {row.get('decision')!r}"
            )
    return mismatches


def _check_args_safe(expected: list[dict[str, object]], facts: Facts) -> list[str]:
    mismatches: list[str] = []
    by_tool: dict[str, dict[str, object]] = {}
    for entry in facts.tool_calls:
        by_tool.setdefault(str(entry["tool"]), entry)

    for spec in expected:
        tool = str(spec["tool"])
        key = str(spec["key"])
        row: dict[str, object] | None = by_tool.get(tool)
        if row is None:
            mismatches.append(f"args_safe: no tool_calls row for tool {tool!r}")
            continue
        args_safe = row.get("args_safe")
        actual = args_safe.get(key) if isinstance(args_safe, dict) else None
        if actual != spec["equals"]:
            mismatches.append(
                f"args_safe[{tool}][{key}]: expected {spec['equals']!r}, got {actual!r}"
            )
    return mismatches


KNOWN_KEYS = frozenset(
    {
        "policy_effects",
        "task_status",
        "tool_executed_count",
        "approvals_pending",
        "audit_contains",
        "tool_calls",
        "args_safe",
    }
)


def check_expectations(expect: dict[str, object], facts: Facts) -> list[str]:
    """Compare one case's `expect` block against the facts a real run produced.

    Only the keys present in `expect` are checked: a case states what it cares about, not
    every field this shape happens to carry, so tests_case YAML stays short. Returns an
    empty list on a pass; every mismatch is reported (not just the first), because a case
    that fails on three fronts should say so in one run of the suite, not three.
    """
    # Only checking the keys present is what makes a typo dangerous: `task_satus` would
    # silently drop the status check and the case would still pass. Unknown keys and an
    # empty block are therefore failures, never "nothing to check".
    if not expect:
        return ["expect: an active case must assert something"]
    mismatches = [
        f"expect: unknown key {key!r}" for key in sorted(set(expect) - KNOWN_KEYS)
    ]

    if "policy_effects" in expect:
        expected_effects = expect["policy_effects"]
        if facts.policy_effects != expected_effects:
            mismatches.append(
                _mismatch("policy_effects", expected_effects, facts.policy_effects)
            )

    if "task_status" in expect and facts.task_status != expect["task_status"]:
        mismatches.append(
            _mismatch("task_status", expect["task_status"], facts.task_status)
        )

    if (
        "tool_executed_count" in expect
        and facts.tool_executed_count != expect["tool_executed_count"]
    ):
        mismatches.append(
            _mismatch(
                "tool_executed_count",
                expect["tool_executed_count"],
                facts.tool_executed_count,
            )
        )

    if (
        "approvals_pending" in expect
        and facts.approvals_pending != expect["approvals_pending"]
    ):
        mismatches.append(
            _mismatch(
                "approvals_pending",
                expect["approvals_pending"],
                facts.approvals_pending,
            )
        )

    if "audit_contains" in expect:
        corpus = "\n".join(facts.audit_corpus)
        for needle in cast(list[str], expect["audit_contains"]):
            if needle not in corpus:
                mismatches.append(
                    f"audit_contains: {needle!r} not found in the audit log"
                )

    if "tool_calls" in expect:
        mismatches.extend(
            _check_tool_calls(
                cast("list[dict[str, object]]", expect["tool_calls"]), facts
            )
        )

    if "args_safe" in expect:
        mismatches.extend(
            _check_args_safe(
                cast("list[dict[str, object]]", expect["args_safe"]), facts
            )
        )

    return mismatches


def summarize(outcomes: list[CaseOutcome]) -> tuple[str, int]:
    """The one-line summary the CLI prints, and the process exit code.

    Pending cases are counted separately and never fail the exit code (briefing week 6
    scope: 3 of the 12 cases wait on features this track does not build). A PENDING case
    reported as anything but visible would be the exact silent-skip the task warns against.
    """
    scored = [outcome for outcome in outcomes if outcome.state != "PENDING"]
    pending = len(outcomes) - len(scored)
    passed = sum(1 for outcome in scored if outcome.state == "PASS")
    # Zero scored cases is a failure too: a gate that ran nothing proved nothing.
    exit_code = 0 if scored and all(o.state == "PASS" for o in scored) else 1
    return f"{passed}/{len(scored)} pass, {pending} pending", exit_code
