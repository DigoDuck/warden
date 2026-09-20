"""The policy engine, proven two ways.

The table in `policy_cases.yaml` pins the behaviour of `policies/default.yaml`: what the
rules currently decide. The property tests below pin the semantics of the engine itself:
what ADR-003 claims is true regardless of which rules are loaded.
"""

import pathlib
import random
from typing import Any

import pytest
import yaml

from warden.policy.engine import (
    Decision,
    Effect,
    Policy,
    PolicyContext,
    Rule,
    UserRef,
    combine,
    load_policy,
)
from warden.policy.matchers import UnknownFieldError

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
DEFAULT_POLICY = REPO_ROOT / "policies" / "default.yaml"
CASES_FILE = pathlib.Path(__file__).parent / "policy_cases.yaml"

_CASES: list[dict[str, Any]] = yaml.safe_load(CASES_FILE.read_text(encoding="utf-8"))["cases"]


def _context(raw: dict[str, Any]) -> PolicyContext:
    return PolicyContext(
        tool=raw["tool"],
        args=raw.get("args", {}),
        path=raw.get("path"),
        user=UserRef(role=raw.get("user_role", "worker")),
    )


@pytest.mark.parametrize("case", _CASES, ids=[case["name"] for case in _CASES])
def test_default_policy_table(case: dict[str, Any]) -> None:
    decision = load_policy(DEFAULT_POLICY).evaluate(_context(case["context"]))

    assert decision.effect == Effect(case["expect"]["effect"])
    if "rules" in case["expect"]:
        assert decision.matched_rules == sorted(case["expect"]["rules"])


def test_the_table_covers_enough_ground() -> None:
    """Week 2 asks for 30+ cases. A shrinking table should fail, not pass quietly."""
    assert len(_CASES) >= 30


# --- semantics of the engine, independent of which rules are loaded -----------------------


def _policy(*rules: Rule, default: Effect = Effect.DENY) -> Policy:
    return Policy(list(rules), default=default, policy_hash="test")


ALLOW_READ = Rule(id="allow-read", effect=Effect.ALLOW, when={"tool": "read_file"})
DENY_SECRET = Rule(id="deny-secret", effect=Effect.DENY, when={"path": ["**/.env"]})
APPROVE_READ = Rule(id="approve-read", effect=Effect.REQUIRE_APPROVAL, when={"tool": "read_file"})
READ_ENV = PolicyContext(tool="read_file", path=".env", user=UserRef(role="worker"))


def test_nothing_matching_means_deny() -> None:
    decision = _policy(ALLOW_READ).evaluate(
        PolicyContext(tool="delete_everything", user=UserRef(role="worker"))
    )
    assert decision.effect == Effect.DENY
    assert decision.matched_rules == []
    assert "default" in decision.reason


def test_deny_beats_allow() -> None:
    assert _policy(ALLOW_READ, DENY_SECRET).evaluate(READ_ENV).effect == Effect.DENY


def test_approval_beats_allow() -> None:
    assert _policy(ALLOW_READ, APPROVE_READ).evaluate(READ_ENV).effect == Effect.REQUIRE_APPROVAL


def test_deny_beats_approval() -> None:
    policy = _policy(APPROVE_READ, DENY_SECRET)
    assert policy.evaluate(READ_ENV).effect == Effect.DENY


def test_every_matching_rule_is_reported_not_just_the_deciding_one() -> None:
    """`matched_rules` is the audit trail; keeping only the winner would hide the conflict."""
    decision = _policy(ALLOW_READ, DENY_SECRET).evaluate(READ_ENV)
    assert decision.matched_rules == ["allow-read", "deny-secret"]


def test_rule_order_never_changes_a_decision() -> None:
    """The claim in ADR-003 that ordering is irrelevant, checked rather than asserted."""
    rules = [ALLOW_READ, DENY_SECRET, APPROVE_READ]
    contexts = [_context(case["context"]) for case in _CASES[:12]]

    baseline = [_policy(*rules).evaluate(ctx) for ctx in contexts]
    shuffler = random.Random(20260920)
    for _ in range(20):
        shuffled = rules[:]
        shuffler.shuffle(shuffled)
        for expected, context in zip(baseline, contexts, strict=True):
            actual = _policy(*shuffled).evaluate(context)
            assert actual.effect == expected.effect
            assert actual.matched_rules == expected.matched_rules


def test_an_allow_rule_contributes_its_scopes() -> None:
    rule = Rule(id="r", effect=Effect.ALLOW, when={"tool": "read_file"}, scopes=["repo:read"])
    assert _policy(rule).evaluate(READ_ENV).scopes == ["repo:read"]


def test_scopes_come_only_from_the_rules_that_decided() -> None:
    """A denied call must not hand out the scopes of the allow rule it also matched."""
    allow = Rule(id="a", effect=Effect.ALLOW, when={"tool": "read_file"}, scopes=["repo:read"])
    decision = _policy(allow, DENY_SECRET).evaluate(READ_ENV)
    assert decision.effect == Effect.DENY
    assert decision.scopes == []


# --- combine(): folding a multi-path call's decisions into one ----------------------------


def _decision(effect: Effect, *, rules: list[str], scopes: list[str] | None = None) -> Decision:
    return Decision(
        effect=effect,
        matched_rules=rules,
        reason=f"test: {effect.value}",
        scopes=scopes or [],
        policy_hash="test",
    )


def test_combine_of_a_single_decision_is_that_decision() -> None:
    solo = _decision(Effect.ALLOW, rules=["write-source"], scopes=["repo:write"])
    assert combine([solo]) == solo


def test_combine_the_most_restrictive_effect_wins() -> None:
    allowed = _decision(Effect.ALLOW, rules=["write-source"])
    denied = _decision(Effect.DENY, rules=["never-read-secrets"])
    assert combine([allowed, denied]).effect == Effect.DENY


def test_combine_require_approval_beats_allow_but_not_deny() -> None:
    allowed = _decision(Effect.ALLOW, rules=["write-source"])
    approval = _decision(Effect.REQUIRE_APPROVAL, rules=["needs-human"])
    denied = _decision(Effect.DENY, rules=["never-read-secrets"])
    assert combine([allowed, approval]).effect == Effect.REQUIRE_APPROVAL
    assert combine([allowed, approval, denied]).effect == Effect.DENY


def test_combine_matched_rules_is_the_sorted_union_of_every_decision() -> None:
    """Not just the winning decision's rules: a rename touching two allowed paths under
    different rules must show both in the audit trail, same as one rule and another
    conflicting one both show up within a single evaluate()."""
    a = _decision(Effect.ALLOW, rules=["write-source"])
    b = _decision(Effect.ALLOW, rules=["list-workspace"])
    assert combine([a, b]).matched_rules == ["list-workspace", "write-source"]


def test_combine_scopes_only_when_every_decision_is_allow() -> None:
    """The case ADR-017 exists for: a patch touching src/a.py (allow) and .github/ci.yml
    (deny) must not hand out repo:write just because most of the paths were fine."""
    allowed = _decision(Effect.ALLOW, rules=["write-source"], scopes=["repo:write"])
    denied = _decision(Effect.DENY, rules=["no-ci-writes"])
    combined = combine([allowed, denied])
    assert combined.effect == Effect.DENY
    assert combined.scopes == []


def test_combine_scopes_are_the_union_when_every_decision_allows() -> None:
    a = _decision(Effect.ALLOW, rules=["write-source"], scopes=["repo:write"])
    b = _decision(Effect.ALLOW, rules=["list-workspace"], scopes=["repo:read"])
    assert combine([a, b]).scopes == ["repo:read", "repo:write"]


def test_combine_of_no_decisions_raises() -> None:
    with pytest.raises(ValueError, match="at least one"):
        combine([])


# --- loading ------------------------------------------------------------------------------


def test_a_rule_naming_an_unknown_field_fails_at_load(tmp_path: pathlib.Path) -> None:
    """The worst failure mode in a policy engine is a deny rule that silently never matches."""
    broken = tmp_path / "broken.yaml"
    broken.write_text(
        "default: deny\nrules:\n  - {id: typo, effect: deny, when: {agent.trustlevel: low}}\n",
        encoding="utf-8",
    )
    with pytest.raises(UnknownFieldError, match="agent.trustlevel"):
        load_policy(broken)


def test_duplicate_rule_ids_fail_at_load(tmp_path: pathlib.Path) -> None:
    """Two rules with one id make matched_rules ambiguous in the audit log."""
    duped = tmp_path / "duped.yaml"
    duped.write_text(
        "default: deny\nrules:\n"
        "  - {id: same, effect: allow, when: {tool: read_file}}\n"
        "  - {id: same, effect: deny, when: {tool: write_file}}\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="same"):
        load_policy(duped)


def test_policy_hash_ignores_comments(tmp_path: pathlib.Path) -> None:
    """Rewording a comment must not look like a policy change in the audit log."""
    body = "default: deny\nrules:\n  - {id: r, effect: allow, when: {tool: read_file}}\n"
    plain, commented = tmp_path / "a.yaml", tmp_path / "b.yaml"
    plain.write_text(body, encoding="utf-8")
    commented.write_text("# a thorough explanation\n" + body, encoding="utf-8")

    assert load_policy(plain).policy_hash == load_policy(commented).policy_hash


def test_policy_hash_changes_when_a_rule_changes(tmp_path: pathlib.Path) -> None:
    before, after = tmp_path / "before.yaml", tmp_path / "after.yaml"
    before.write_text(
        "default: deny\nrules:\n  - {id: r, effect: allow, when: {tool: read_file}}\n",
        encoding="utf-8",
    )
    after.write_text(
        "default: deny\nrules:\n  - {id: r, effect: deny, when: {tool: read_file}}\n",
        encoding="utf-8",
    )

    assert load_policy(before).policy_hash != load_policy(after).policy_hash


def test_the_shipped_policy_loads() -> None:
    policy = load_policy(DEFAULT_POLICY)
    assert policy.default == Effect.DENY
    assert len(policy.rules) >= 6
