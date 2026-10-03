"""What `AnthropicProvider.generate` actually sends, checked without a network call or a key.

The request shape is where model upgrades break silently: Claude Opus 5.5 lowered its default
effort to `medium`, so leaving `effort` out would make the coder shallower with no error.
"""

from typing import Any

from anthropic.types import Message as AnthropicMessage
from anthropic.types import TextBlock, Usage

from warden.providers.anthropic import DEFAULT_EFFORT, DEFAULT_MODEL, AnthropicProvider
from warden.providers.base import UserMessage


class _RecordingMessages:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> AnthropicMessage:
        self.calls.append(kwargs)
        return AnthropicMessage.model_validate(
            {
                "id": "msg_test",
                "type": "message",
                "role": "assistant",
                "model": kwargs["model"],
                "content": [TextBlock(type="text", text="ok")],
                "stop_reason": "end_turn",
                "usage": Usage(input_tokens=1, output_tokens=1),
            }
        )


class _RecordingClient:
    def __init__(self) -> None:
        self.messages = _RecordingMessages()


async def _sent() -> dict[str, Any]:
    client = _RecordingClient()
    provider = AnthropicProvider(client)  # type: ignore[arg-type]
    await provider.generate([UserMessage(text="hi")])
    return client.messages.calls[0]


async def test_the_default_model_is_opus_5_5() -> None:
    assert DEFAULT_MODEL == "claude-opus-5-5"
    assert (await _sent())["model"] == "claude-opus-5-5"


async def test_effort_is_always_sent_explicitly() -> None:
    sent = await _sent()
    assert sent["output_config"] == {"effort": DEFAULT_EFFORT}
    assert DEFAULT_EFFORT == "high"


async def test_thinking_is_left_to_the_model() -> None:
    # On Opus 5.5 `thinking: disabled` is a 400; omitting it keeps adaptive thinking on.
    assert "thinking" not in await _sent()
