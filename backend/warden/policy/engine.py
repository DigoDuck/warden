"""Deterministic authorisation: given a context, allow, deny or ask a human.

The semantics are the project's argument, recorded in ADR-003:

- **default deny**: no rule matched means deny
- **every matching rule is collected**, with no early exit
- **the most restrictive effect wins**: deny > require_approval > allow
- **no order dependence**: shuffling the rule file changes nothing
- **there is no exception to a deny**: you write the more specific deny instead

This module decides. It executes nothing, reads no files and never treats model output as
instruction. That is what makes the guarantee hold even when the model is compromised.
"""

import hashlib
import json
import pathlib
from collections.abc import Callable
from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

import yaml
from pydantic import BaseModel, Field

from warden.policy.matchers import Matcher, build_matcher, resolve_field


class Effect(StrEnum):
    ALLOW = "allow"
    REQUIRE_APPROVAL = "require_approval"
    DENY = "deny"


# Higher wins when several rules match. The ordering is the whole semantics in one line.
_SEVERITY: dict[Effect, int] = {Effect.ALLOW: 0, Effect.REQUIRE_APPROVAL: 1, Effect.DENY: 2}


class UserRef(BaseModel):
    id: UUID | None = None
    role: str


class PolicyContext(BaseModel):
    """What a decision is made about.

    Only fields the control plane can actually populate today. `agent` and `risk` arrive
    with the registry, `env` with real deployments. Filling them with a fixed value now
    would be fiction, and rules written against fiction are worse than no rules.
    """

    tool: str
    args: dict[str, Any] = Field(default_factory=dict)
    # Already normalised by the caller: workspace-relative, POSIX separators. The engine
    # never resolves a path itself, because resolving means touching the filesystem.
    path: str | None = None
    user: UserRef
    task_id: UUID | None = None
    now: datetime | None = None


class Decision(BaseModel):
    effect: Effect
    matched_rules: list[str]
    reason: str
    scopes: list[str] = Field(default_factory=list)
    policy_hash: str


class Rule(BaseModel):
    id: str
    effect: Effect
    reason: str = ""
    when: dict[str, Any] = Field(default_factory=dict)
    scopes: list[str] = Field(default_factory=list)


class Policy:
    def __init__(self, rules: list[Rule], *, default: Effect, policy_hash: str) -> None:
        self.rules = rules
        self.default = default
        self.policy_hash = policy_hash
        # Compiled once at load. A rule naming an unknown field raises here rather than
        # silently never matching at evaluation time.
        self._matchers: dict[str, list[Matcher]] = {
            rule.id: [build_matcher(field, spec) for field, spec in rule.when.items()]
            for rule in rules
        }

    def _matching(self, context: PolicyContext) -> list[Rule]:
        return [
            rule
            for rule in self.rules
            if all(
                matcher.matches(resolve_field(context, matcher.field))
                for matcher in self._matchers[rule.id]
            )
        ]

    def explicitly_denies(self, context: PolicyContext) -> bool:
        """True when a deny RULE matched, as opposed to the default deny merely applying.

        The distinction matters to whoever populates a sandbox. A file nobody wrote an allow
        rule for (a CI config, say) is unreadable through `read_file` but is still a normal
        part of the project that the test suite may need on disk. A file a rule explicitly
        denies is one the policy author decided the agent must never see, and that intent
        can only be honoured by the file not being there at all (ADR-018).
        """
        return any(rule.effect is Effect.DENY for rule in self._matching(context))

    def evaluate(self, context: PolicyContext) -> Decision:
        matched = self._matching(context)

        if not matched:
            return Decision(
                effect=self.default,
                matched_rules=[],
                reason=f"no rule matched; the default is {self.default.value}",
                policy_hash=self.policy_hash,
            )

        effect = max((rule.effect for rule in matched), key=lambda e: _SEVERITY[e])
        deciding = [rule for rule in matched if rule.effect == effect]
        reason = next(
            (rule.reason for rule in deciding if rule.reason),
            f"{effect.value} by {', '.join(rule.id for rule in deciding)}",
        )
        # Sorted, so the recorded list does not depend on the order of the rule file.
        # Union of scopes, and only from the rules that actually decided.
        return Decision(
            effect=effect,
            matched_rules=sorted(rule.id for rule in matched),
            reason=reason,
            scopes=sorted({scope for rule in deciding for scope in rule.scopes}),
            policy_hash=self.policy_hash,
        )


def combine(decisions: list[Decision]) -> Decision:
    """Fold the per-path decisions of one multi-path call into a single verdict.

    Same ADR-003 semantics as `Policy.evaluate`, one level up: there the inputs are the
    rules that matched one context, here they are the decisions already made for each path
    a call touches (`apply_patch` on a diff that renames a file into `.github/` is one call
    but two paths). The most restrictive effect still wins, `matched_rules` is still the
    full union, and scopes still come only from the decisions that decided.

    `scopes` is the one place this is stricter than a single evaluate(): they are handed
    out only when *every* path was itself allowed. A call spanning N files is authorised as
    one action, so a call where 9 paths are allowed and 1 is denied must not leak the
    scopes of the 9, because `effect` on the combined decision is deny and those scopes
    would otherwise look like they belonged to a call the control plane actually approved.
    Since ALLOW is the least severe effect, "every decision is allow" and "the combined
    effect is allow" are the same condition, checked once below.
    """
    if not decisions:
        raise ValueError("combine() requires at least one decision")

    effect = max((decision.effect for decision in decisions), key=lambda e: _SEVERITY[e])
    deciding = [decision for decision in decisions if decision.effect == effect]
    reason = next(
        (decision.reason for decision in deciding if decision.reason),
        f"{effect.value} across {len(decisions)} path(s)",
    )

    return Decision(
        effect=effect,
        matched_rules=sorted({rule for decision in decisions for rule in decision.matched_rules}),
        reason=reason,
        scopes=(
            sorted({scope for decision in decisions for scope in decision.scopes})
            if effect is Effect.ALLOW
            else []
        ),
        # Every decision folded here came from evaluating the same loaded Policy, so they
        # all carry the same hash; the first is as good as any other.
        policy_hash=decisions[0].policy_hash,
    )


def load_policy(path: str | pathlib.Path) -> Policy:
    document = yaml.safe_load(pathlib.Path(path).read_text(encoding="utf-8")) or {}
    rules = [Rule.model_validate(raw) for raw in document.get("rules", [])]

    ids = [rule.id for rule in rules]
    duplicates = {rule_id for rule_id in ids if ids.count(rule_id) > 1}
    if duplicates:
        raise ValueError(f"duplicate rule ids in {path}: {', '.join(sorted(duplicates))}")

    return Policy(
        rules=rules,
        default=Effect(document.get("default", Effect.DENY.value)),
        # Hash of the parsed document, not of the file bytes: a reworded comment must not
        # look like a policy change in the audit log, while a reordered or edited rule must.
        policy_hash=hashlib.sha256(
            json.dumps(document, sort_keys=True, default=str).encode()
        ).hexdigest(),
    )


def never_readable(policy: Policy) -> Callable[[str], bool]:
    """A predicate over workspace-relative paths: must this file stay out of the sandbox?

    Path-based denies do not survive code execution. An agent allowed to write a test and to
    run the test runner can have that test open any file in the workspace and print it, and
    the output comes back as a tool result: `never-read-secrets` would be advisory. What is
    not in the container cannot be read by anything running in it, so every file a deny rule
    names is filtered out when the workspace is copied in (ADR-018).

    Judged as `read_file` for the worker role. A deny rule conditioned on some other role
    would not be seen here; none exists, and one that did would deserve its own thought.
    """
    user = UserRef(role="worker")

    def excluded(relative_path: str) -> bool:
        return policy.explicitly_denies(
            PolicyContext(tool="read_file", path=relative_path, user=user)
        )

    return excluded
