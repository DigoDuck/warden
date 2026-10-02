"""verify/reviewer.py: one independent model call, a structured verdict, and the definition of
success (ADR-010).

No database and no container: what is under test is what the reviewer is shown, how a model
response is read, and how a verdict combines with the evidence. A recording provider stands in
for the model, because the FakeProvider ignores the messages it is sent and this file's whole
point is to look at them.
"""

import inspect
from collections.abc import Sequence
from typing import Any

import pytest

from warden.providers.base import Completion, Message, ToolSchema, Usage, UserMessage
from warden.providers.base import ToolCall as ProviderToolCall
from warden.verify.reviewer import (
    GATING_KINDS,
    VERDICT_TOOL,
    ProviderReviewer,
    decide,
)

SPEC = "make average() ignore None values"


class _Recording:
    name = "fake"

    def __init__(self, completion: Completion) -> None:
        self._completion = completion
        self.calls: list[dict[str, Any]] = []

    async def generate(
        self,
        messages: Sequence[Message],
        *,
        tools: Sequence[ToolSchema] | None = None,
        system: str | None = None,
        model: str | None = None,
        max_tokens: int = 16000,
    ) -> Completion:
        self.calls.append(
            {"messages": list(messages), "tools": list(tools or []), "system": system}
        )
        return self._completion


def _verdict(arguments: dict[str, Any], name: str = VERDICT_TOOL) -> Completion:
    return Completion(
        provider="fake",
        model="fake-model",
        stop_reason="tool_use",
        tool_calls=[ProviderToolCall(id="v1", name=name, arguments=arguments)],
        usage=Usage(),
    )


def _command_evidence(kind: str, status: str = "passed", output: str = "ok") -> dict[str, Any]:
    return {
        "kind": kind,
        "status": status,
        "passed": status == "passed",
        "commands": [
            {
                "argv": [kind, "--check"],
                "exit_code": 0 if status == "passed" else 1,
                "output": output,
                "duration_ms": 5,
            }
        ],
    }


def _diff_evidence(patch: str = "--- a/x\n+++ b/x\n+new line\n") -> dict[str, Any]:
    return {
        "kind": "diff",
        "status": "ok",
        "files": [{"path": "x", "change": "modified", "additions": 1, "deletions": 0}],
        "files_changed": 1,
        "additions": 1,
        "deletions": 0,
        "patch": patch,
        "patch_truncated": False,
    }


def _all_green() -> dict[str, dict[str, Any]]:
    return {
        "diff": _diff_evidence(),
        "lint": _command_evidence("lint"),
        "types": _command_evidence("types"),
        "tests": _command_evidence("tests"),
    }


# --- what the reviewer is shown ---------------------------------------------------------


def test_review_has_no_parameter_through_which_the_coders_words_could_arrive() -> None:
    """The independence is structural: there is nowhere to pass a summary or a conversation,
    so no future caller can leak one by accident. Checked on the concrete implementation, the
    only thing the loop ever calls."""
    params = set(inspect.signature(ProviderReviewer.review).parameters) - {"self"}
    assert params == {"spec", "evidence"}


async def test_the_reviewer_sees_the_spec_the_diff_and_every_gating_output() -> None:
    provider = _Recording(_verdict({"passed": True, "findings": []}))
    evidence = _all_green()
    evidence["tests"] = _command_evidence("tests", "failed", output="FAILED test_average")

    await ProviderReviewer(provider).review(SPEC, evidence)

    [call] = provider.calls  # exactly one model call per review
    [message] = call["messages"]
    assert isinstance(message, UserMessage)
    for needle in (SPEC, "+new line", "FAILED test_average", "lint", "types", "tests"):
        assert needle in message.text
    assert call["system"] and "independent" in call["system"].lower()


async def test_the_reviewer_can_only_answer_through_submit_verdict() -> None:
    provider = _Recording(_verdict({"passed": True, "findings": []}))

    await ProviderReviewer(provider).review(SPEC, _all_green())

    [call] = provider.calls
    assert [tool.name for tool in call["tools"]] == [VERDICT_TOOL]


# --- reading the answer -----------------------------------------------------------------


async def test_a_well_formed_verdict_is_parsed() -> None:
    provider = _Recording(_verdict({"passed": False, "findings": ["None still crashes"]}))

    review = await ProviderReviewer(provider).review(SPEC, _all_green())

    assert review.malformed_reason is None
    assert review.verdict is not None
    assert review.verdict.passed is False
    assert review.verdict.findings == ["None still crashes"]
    assert review.completion.model == "fake-model"  # the loop bills this call


async def test_no_tool_call_is_malformed_never_an_approval() -> None:
    text_only = Completion(
        provider="fake", model="m", stop_reason="end_turn", text="looks fine to me", usage=Usage()
    )

    review = await ProviderReviewer(_Recording(text_only)).review(SPEC, _all_green())

    assert review.verdict is None
    assert review.malformed_reason is not None
    assert VERDICT_TOOL in review.malformed_reason


async def test_a_call_to_some_other_tool_is_malformed() -> None:
    other = _verdict({"passed": True, "findings": []}, name="finish")

    review = await ProviderReviewer(_Recording(other)).review(SPEC, _all_green())

    assert review.verdict is None and review.malformed_reason is not None


@pytest.mark.parametrize(
    "arguments",
    [
        {"findings": ["forgot to say passed"]},  # required field missing
        {"passed": "yes", "findings": []},  # a string is not a boolean
        {"passed": 1, "findings": []},  # nor is a number
        {"passed": True, "findings": "all good"},  # findings must be a list
        {"passed": True, "findings": [1, 2]},  # of strings
        {},
    ],
)
async def test_wrong_shaped_arguments_are_malformed(arguments: dict[str, Any]) -> None:
    review = await ProviderReviewer(_Recording(_verdict(arguments))).review(SPEC, _all_green())

    assert review.verdict is None
    assert review.malformed_reason is not None


# --- the definition of success ----------------------------------------------------------


def test_green_evidence_and_an_approving_reviewer_succeed() -> None:
    resolution = decide(True, None, _all_green())

    assert (resolution.status, resolution.reason) == ("SUCCEEDED", None)


@pytest.mark.parametrize("kind", GATING_KINDS)
@pytest.mark.parametrize("status", ["failed", "timeout", "error"])
def test_red_evidence_fails_the_task_even_when_the_reviewer_approves(
    kind: str, status: str
) -> None:
    """The model cannot override a red check: that is the whole point of a gate."""
    evidence = _all_green()
    evidence[kind] = _command_evidence(kind, status)

    resolution = decide(True, None, evidence)

    assert resolution.status == "FAILED"
    assert resolution.reason is not None and kind in resolution.reason


def test_missing_gating_evidence_fails_the_task() -> None:
    evidence = _all_green()
    del evidence["types"]

    assert decide(True, None, evidence).status == "FAILED"


def test_a_rejecting_reviewer_fails_the_task_with_green_evidence() -> None:
    assert decide(False, None, _all_green()).status == "FAILED"


def test_a_malformed_verdict_fails_the_task_and_says_why() -> None:
    resolution = decide(False, "reviewer did not call submit_verdict", _all_green())

    assert resolution.status == "FAILED"
    assert resolution.reason is not None and "malformed" in resolution.reason


def test_the_diff_never_gates() -> None:
    """A diff is a fact, not a check: an errored diff alone does not fail an otherwise
    green, approved task."""
    evidence = _all_green()
    evidence["diff"] = {"kind": "diff", "status": "error", "error": "export failed"}

    assert decide(True, None, evidence).status == "SUCCEEDED"
