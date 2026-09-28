"""Fast, DB-free unit tests for the eval runner's own logic: the expectation checker and
the pending accounting. Everything that needs Postgres/Docker lives in runner.py and is
exercised by actually running the dataset (`make evals-behavioral`), not here.
"""

from evals.checks import CaseOutcome, Facts, check_expectations, summarize


def _facts(**overrides: object) -> Facts:
    base = Facts(
        policy_effects=["deny"],
        task_status="SUCCEEDED",
        tool_executed_count=0,
        approvals_pending=0,
        audit_corpus=["policy.deny:never-read-secrets"],
        tool_calls=[
            {"tool": "read_file", "decision": "deny", "args_safe": {"path": ".env"}}
        ],
    )
    for key, value in overrides.items():
        setattr(base, key, value)
    return base


def test_matching_expectations_produce_no_mismatches() -> None:
    expect = {
        "policy_effects": ["deny"],
        "task_status": "SUCCEEDED",
        "tool_executed_count": 0,
        "audit_contains": ["policy.deny:never-read-secrets"],
    }
    assert check_expectations(expect, _facts()) == []


def test_wrong_task_status_is_reported() -> None:
    expect = {"task_status": "FAILED"}
    mismatches = check_expectations(expect, _facts())
    assert len(mismatches) == 1
    assert "task_status" in mismatches[0]
    assert "FAILED" in mismatches[0]
    assert "SUCCEEDED" in mismatches[0]


def test_wrong_policy_effects_order_is_reported() -> None:
    expect = {"policy_effects": ["allow", "deny"]}
    mismatches = check_expectations(expect, _facts(policy_effects=["deny", "allow"]))
    assert any("policy_effects" in m for m in mismatches)


def test_missing_audit_substring_is_reported() -> None:
    expect = {"audit_contains": ["policy.deny:no-destructive-commands"]}
    mismatches = check_expectations(expect, _facts())
    assert any("audit" in m and "no-destructive-commands" in m for m in mismatches)


def test_approvals_pending_mismatch_is_reported() -> None:
    expect = {"approvals_pending": 1}
    mismatches = check_expectations(expect, _facts(approvals_pending=0))
    assert any("approvals_pending" in m for m in mismatches)


def test_tool_executed_count_mismatch_is_reported() -> None:
    expect = {"tool_executed_count": 2}
    mismatches = check_expectations(expect, _facts(tool_executed_count=0))
    assert any("tool_executed_count" in m for m in mismatches)


def test_args_safe_expectation_matches_redacted_value() -> None:
    expect = {
        "args_safe": [
            {"tool": "read_file", "key": "auth_token", "equals": "[redacted]"}
        ]
    }
    facts = _facts(
        tool_calls=[
            {
                "tool": "read_file",
                "decision": "allow",
                "args_safe": {"path": "src/app.py", "auth_token": "[redacted]"},
            }
        ]
    )
    assert check_expectations(expect, facts) == []


def test_args_safe_expectation_catches_a_leaked_secret() -> None:
    expect = {
        "args_safe": [
            {"tool": "read_file", "key": "auth_token", "equals": "[redacted]"}
        ]
    }
    facts = _facts(
        tool_calls=[
            {
                "tool": "read_file",
                "decision": "allow",
                "args_safe": {"path": "src/app.py", "auth_token": "super-secret-value"},
            }
        ]
    )
    mismatches = check_expectations(expect, facts)
    assert any("args_safe" in m for m in mismatches)


def test_tool_calls_decision_expectation() -> None:
    expect = {"tool_calls": [{"tool": "github.open_pr", "decision": "rejected"}]}
    facts = _facts(
        tool_calls=[{"tool": "github.open_pr", "decision": "rejected", "args_safe": {}}]
    )
    assert check_expectations(expect, facts) == []

    wrong = _facts(
        tool_calls=[{"tool": "github.open_pr", "decision": "allow", "args_safe": {}}]
    )
    assert check_expectations(expect, wrong) != []


def test_unknown_tool_in_tool_calls_expectation_is_a_mismatch() -> None:
    expect = {"tool_calls": [{"tool": "github.open_pr", "decision": "rejected"}]}
    mismatches = check_expectations(expect, _facts(tool_calls=[]))
    assert any("github.open_pr" in m for m in mismatches)


def test_summarize_counts_pass_fail_and_pending_and_sets_exit_code() -> None:
    outcomes = [
        CaseOutcome(key="a", state="PASS", detail=""),
        CaseOutcome(key="b", state="PASS", detail=""),
        CaseOutcome(key="c", state="PENDING", detail="not implemented yet"),
    ]
    line, code = summarize(outcomes)
    assert line == "2/2 pass, 1 pending"
    assert code == 0


def test_summarize_fails_the_exit_code_on_any_failure_but_still_counts_pending() -> (
    None
):
    outcomes = [
        CaseOutcome(key="a", state="PASS", detail=""),
        CaseOutcome(
            key="b", state="FAIL", detail="task_status: expected FAILED, got SUCCEEDED"
        ),
        CaseOutcome(key="c", state="PENDING", detail="not implemented yet"),
    ]
    line, code = summarize(outcomes)
    assert line == "1/2 pass, 1 pending"
    assert code == 1


def test_summarize_with_no_cases_is_a_pass_with_a_zero_over_zero_line() -> None:
    line, code = summarize([])
    assert line == "0/0 pass, 0 pending"
    assert code == 0
