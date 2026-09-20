"""Adapter for the Anthropic Messages API.

Render and parse are free functions on purpose: they are pure, so the mapping is tested
without a network call and without an API key.
"""

from collections.abc import Sequence

from anthropic import AsyncAnthropic, omit
from anthropic.types import Message as AnthropicMessage
from anthropic.types import MessageParam, ToolParam, ToolResultBlockParam

from warden.providers.base import (
    AssistantMessage,
    Completion,
    Message,
    Refusal,
    ToolCall,
    ToolResultsMessage,
    ToolSchema,
    Usage,
    UserMessage,
)

PROVIDER_NAME = "anthropic"
DEFAULT_MODEL = "claude-opus-5"


def to_anthropic_messages(messages: Sequence[Message]) -> list[MessageParam]:
    """Render the three domain message shapes into the wire format."""
    rendered: list[MessageParam] = []
    for message in messages:
        match message:
            case UserMessage():
                rendered.append(MessageParam(role="user", content=message.text))
            case AssistantMessage():
                # Echoed exactly as the provider returned it. See ADR-016.
                rendered.append(MessageParam(role="assistant", content=message.raw_content))
            case ToolResultsMessage():
                blocks: list[ToolResultBlockParam] = [
                    ToolResultBlockParam(
                        type="tool_result",
                        tool_use_id=result.tool_call_id,
                        content=result.content,
                        is_error=result.is_error,
                    )
                    for result in message.results
                ]
                rendered.append(MessageParam(role="user", content=blocks))
    return rendered


def to_anthropic_tools(tools: Sequence[ToolSchema]) -> list[ToolParam]:
    return [
        ToolParam(name=t.name, description=t.description, input_schema=t.input_schema)
        for t in tools
    ]


def from_anthropic_message(response: AnthropicMessage) -> Completion:
    """Map one API response onto the domain `Completion`."""
    if response.stop_reason is None:
        # Only happens on a partial streamed message; this adapter does not stream.
        raise ValueError(f"response {response.id} arrived without a stop_reason")

    text = "".join(block.text for block in response.content if block.type == "text")
    tool_calls = [
        ToolCall(
            id=block.id,
            name=block.name,
            # The model's JSON escaping varies between turns, so never string-match a
            # serialised input. The SDK already parsed it into a dict.
            arguments=dict(block.input) if isinstance(block.input, dict) else {},
        )
        for block in response.content
        if block.type == "tool_use"
    ]

    refusal = None
    if response.stop_reason == "refusal" and response.stop_details is not None:
        refusal = Refusal(
            category=response.stop_details.category,
            explanation=response.stop_details.explanation,
        )

    usage = response.usage
    return Completion(
        provider=PROVIDER_NAME,
        model=response.model,
        stop_reason=response.stop_reason,
        text=text,
        tool_calls=tool_calls,
        usage=Usage(
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cache_creation_input_tokens=usage.cache_creation_input_tokens or 0,
            cache_read_input_tokens=usage.cache_read_input_tokens or 0,
        ),
        # Dumped to plain JSON rather than kept as SDK objects: this value is persisted in
        # `task_events.payload` (JSONB) and replayed from there. The API accepts these dicts
        # back unchanged, so the round trip through the database is lossless.
        raw_content=[
            block.model_dump(mode="json", exclude_none=True) for block in response.content
        ],
        refusal=refusal,
        # Present only on a response that came off the wire: the SDK attaches it from the
        # `request-id` header. A response built in a test has no such attribute.
        request_id=getattr(response, "_request_id", None),
    )


class AnthropicProvider:
    """Calls the real API. Every request costs money."""

    name = PROVIDER_NAME

    def __init__(self, client: AsyncAnthropic | None = None, *, model: str = DEFAULT_MODEL) -> None:
        # The client is injected so tests never construct one and never need a key. A bare
        # AsyncAnthropic() resolves credentials from the environment on its own.
        self._client = client or AsyncAnthropic()
        self._model = model

    async def generate(
        self,
        messages: Sequence[Message],
        *,
        tools: Sequence[ToolSchema] | None = None,
        system: str | None = None,
        model: str | None = None,
        max_tokens: int = 16000,
    ) -> Completion:
        # `omit`, not the legacy NOT_GIVEN: in SDK 1.x these parameters are typed against
        # `Omit`, and mypy rejects the old sentinel.
        #
        # `thinking` is deliberately not passed: on Claude Opus 5 adaptive thinking is on by
        # default, and disabling it makes the model occasionally write a tool call into
        # visible text instead of a tool_use block, which an agent loop cannot see.
        response = await self._client.messages.create(
            model=model or self._model,
            max_tokens=max_tokens,
            messages=to_anthropic_messages(messages),
            tools=to_anthropic_tools(tools) if tools else omit,
            system=system if system is not None else omit,
        )
        return from_anthropic_message(response)
