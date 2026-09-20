"""The contract every model provider implements.

Design rule, decided in ADR-016: `Completion` normalises only what the agent loop and the
policy engine need to reason about (`stop_reason`, `tool_calls`, `usage`, `text`). Whatever
else the provider returned travels in `raw_content` as an opaque value that `core/` stores
in the event log and hands back untouched on the next turn.

The reason is concrete: Anthropic requires the response blocks to be echoed back verbatim,
thinking blocks included, and losing a byte there invalidates replay. The cost is that
`raw_content` is provider-specific, so switching provider mid-task is forbidden by
contract. The router (week 8) picks a provider per task, never per iteration.
"""

from collections.abc import Sequence
from typing import Any, Literal, Protocol

from pydantic import BaseModel, Field

# Every terminal and continuation signal the API can return. Taken from the SDK's own
# Literal rather than from memory: `model_context_window_exceeded` is easy to miss and is
# terminal, so the loop must fail the task instead of iterating again.
StopReason = Literal[
    "end_turn",
    "max_tokens",
    "stop_sequence",
    "tool_use",
    "pause_turn",
    "refusal",
    "model_context_window_exceeded",
]


class ToolCall(BaseModel):
    """A tool the model wants to run. The policy engine decides whether it may."""

    id: str
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)


class ToolResult(BaseModel):
    """The outcome of a tool call, going back to the model."""

    tool_call_id: str
    content: str
    is_error: bool = False


class ToolSchema(BaseModel):
    """A tool offered to the model. Produced by the tool registry, rendered per provider."""

    name: str
    description: str
    input_schema: dict[str, Any]


class Usage(BaseModel):
    """Token counts for one model call.

    Cache fields are optional on the wire and default to zero, so a provider that does not
    report them never produces a None that later arithmetic has to guard.
    """

    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0


class Refusal(BaseModel):
    """Populated only when `stop_reason == "refusal"`."""

    category: str | None = None
    explanation: str | None = None


class UserMessage(BaseModel):
    """Text from the control plane to the model: the initial spec, or an injected error."""

    role: Literal["user"] = "user"
    text: str


class AssistantMessage(BaseModel):
    """A previous model turn, replayed as the provider itself returned it."""

    role: Literal["assistant"] = "assistant"
    raw_content: Any


class ToolResultsMessage(BaseModel):
    """Results of the tool calls from the previous turn.

    All results for one turn belong in a single message. Splitting them across messages
    teaches the model to stop making parallel calls.
    """

    role: Literal["user"] = "user"
    results: list[ToolResult]


Message = UserMessage | AssistantMessage | ToolResultsMessage


class Completion(BaseModel):
    """One model response, normalised at the seam the agent loop reads."""

    provider: str
    model: str
    stop_reason: StopReason
    text: str = ""
    tool_calls: list[ToolCall] = Field(default_factory=list)
    usage: Usage = Field(default_factory=Usage)
    # Opaque to everything outside the provider that produced it. Must stay
    # JSON-serialisable: it is persisted in `task_events.payload` (JSONB) and replayed
    # from there on resume.
    raw_content: Any = None
    refusal: Refusal | None = None
    request_id: str | None = None


class ModelProvider(Protocol):
    """What the agent loop is allowed to ask of a model.

    Deliberately smaller than the sketch in briefing section 18: `generate_structured` and
    `stream` have no caller before week 8, and a Protocol member nobody implements is worse
    than a small Protocol. They arrive with the router and with SSE.
    """

    name: str

    async def generate(
        self,
        messages: Sequence[Message],
        *,
        tools: Sequence[ToolSchema] | None = None,
        system: str | None = None,
        model: str | None = None,
        max_tokens: int = 16000,
    ) -> Completion: ...
