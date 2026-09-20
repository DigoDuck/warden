"""Render and parse are pure, so the mapping is tested with no network and no API key.

The responses here are built from the SDK's own Pydantic types rather than from hand-written
dicts. If Anthropic changes the response shape in a later SDK version, these break when the
dependency is bumped instead of in production.
"""

from anthropic.types import Message as AnthropicMessage
from anthropic.types import RefusalStopDetails, TextBlock, ToolUseBlock, Usage

from warden.providers.anthropic import (
    from_anthropic_message,
    to_anthropic_messages,
    to_anthropic_tools,
)
from warden.providers.base import (
    AssistantMessage,
    ToolResult,
    ToolResultsMessage,
    ToolSchema,
    UserMessage,
)


def _response(**overrides: object) -> AnthropicMessage:
    defaults: dict[str, object] = {
        "id": "msg_test",
        "type": "message",
        "role": "assistant",
        "model": "claude-opus-5",
        "content": [TextBlock(type="text", text="hello")],
        "stop_reason": "end_turn",
        "usage": Usage(input_tokens=10, output_tokens=5),
    }
    return AnthropicMessage.model_validate(defaults | overrides)


def test_user_message_renders_as_plain_text() -> None:
    assert to_anthropic_messages([UserMessage(text="read the repo")]) == [
        {"role": "user", "content": "read the repo"}
    ]


def test_assistant_message_is_echoed_untouched() -> None:
    """ADR-016: the provider's own blocks go back verbatim, thinking blocks included."""
    raw = [{"type": "thinking", "thinking": "...", "signature": "abc"}]
    rendered = to_anthropic_messages([AssistantMessage(raw_content=raw)])
    assert rendered == [{"role": "assistant", "content": raw}]
    assert rendered[0]["content"] is raw


def test_tool_results_render_as_one_user_message() -> None:
    """All results for a turn in one message; splitting them suppresses parallel calls."""
    message = ToolResultsMessage(
        results=[
            ToolResult(tool_call_id="toolu_1", content="ok"),
            ToolResult(tool_call_id="toolu_2", content="denied", is_error=True),
        ]
    )
    rendered = to_anthropic_messages([message])
    assert len(rendered) == 1
    assert rendered[0]["role"] == "user"
    assert rendered[0]["content"] == [
        {"type": "tool_result", "tool_use_id": "toolu_1", "content": "ok", "is_error": False},
        {"type": "tool_result", "tool_use_id": "toolu_2", "content": "denied", "is_error": True},
    ]


def test_tools_render_with_the_schema_the_api_expects() -> None:
    schema = ToolSchema(
        name="read_file",
        description="Read a file from the workspace",
        input_schema={"type": "object", "properties": {"path": {"type": "string"}}},
    )
    assert to_anthropic_tools([schema]) == [
        {
            "name": "read_file",
            "description": "Read a file from the workspace",
            "input_schema": schema.input_schema,
        }
    ]


def test_tool_use_response_becomes_tool_calls() -> None:
    response = _response(
        stop_reason="tool_use",
        content=[
            TextBlock(type="text", text="Let me look."),
            ToolUseBlock(
                type="tool_use", id="toolu_1", name="read_file", input={"path": "src/app.py"}
            ),
        ],
    )
    completion = from_anthropic_message(response)

    assert completion.stop_reason == "tool_use"
    assert completion.text == "Let me look."
    assert len(completion.tool_calls) == 1
    assert completion.tool_calls[0].id == "toolu_1"
    assert completion.tool_calls[0].arguments == {"path": "src/app.py"}


def test_raw_content_is_json_serialisable() -> None:
    """It is persisted in task_events.payload (JSONB), so SDK objects cannot survive there."""
    import json

    response = _response(
        content=[ToolUseBlock(type="tool_use", id="toolu_1", name="finish", input={"summary": "x"})]
    )
    completion = from_anthropic_message(response)

    round_tripped = json.loads(json.dumps(completion.raw_content))
    assert round_tripped == completion.raw_content
    assert round_tripped[0]["type"] == "tool_use"


def test_usage_carries_cache_fields_and_never_none() -> None:
    response = _response(
        usage=Usage(
            input_tokens=10,
            output_tokens=5,
            cache_creation_input_tokens=100,
            cache_read_input_tokens=200,
        )
    )
    usage = from_anthropic_message(response).usage
    assert (usage.cache_creation_input_tokens, usage.cache_read_input_tokens) == (100, 200)


def test_absent_cache_fields_become_zero() -> None:
    """The wire type is Optional[int]; a None here would break the cost arithmetic."""
    usage = from_anthropic_message(_response()).usage
    assert usage.cache_creation_input_tokens == 0
    assert usage.cache_read_input_tokens == 0


def test_refusal_is_carried_into_the_domain() -> None:
    response = _response(
        stop_reason="refusal",
        stop_details=RefusalStopDetails(type="refusal", category="cyber", explanation="nope"),
    )
    completion = from_anthropic_message(response)

    assert completion.stop_reason == "refusal"
    assert completion.refusal is not None
    assert completion.refusal.category == "cyber"


def test_pause_turn_reaches_the_loop_instead_of_looking_like_end_turn() -> None:
    """pause_turn means resend to continue. Collapsing it to end_turn truncates the task."""
    assert from_anthropic_message(_response(stop_reason="pause_turn")).stop_reason == "pause_turn"


def test_context_window_exceeded_reaches_the_loop() -> None:
    """Terminal, and easy to miss: it is in the SDK Literal but not in most docs."""
    completion = from_anthropic_message(_response(stop_reason="model_context_window_exceeded"))
    assert completion.stop_reason == "model_context_window_exceeded"
