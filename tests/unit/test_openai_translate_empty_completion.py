"""Wire tests for the ``empty_completion`` warning of the openai-translate provider.

An upstream that closes the turn with no text and no tool calls
(DeepSeek-V4-Flash answering a chat array that ends in a system message)
used to leave no trace in the router log: the client saw an empty assistant
turn and nothing else. The provider logs a ``warning`` event
``empty_completion`` on the non-streaming path, and the stream converters
log it before ``message_stop`` on the streaming path. Upstream bodies are
served by pytest-httpx on top of a real ``AsyncOpenAI`` (same pattern as
``test_openai_translate_stream_flag``); log events are captured with a
recorder substituted for the provider module logger, for the reason given in
``test_openai_translate_context_window`` (a cached structlog chain makes
``capture_logs`` order-dependent). The stream converters receive that same
module logger as an argument, so the recorder sees their events too.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Callable
from typing import Any

import httpx
import pytest
from pydantic import SecretStr
from pytest_httpx import HTTPXMock

import providers.openai_translate as openai_translate_module
from conversion.response_converter import (
    convert_openai_streaming_to_claude_with_cancellation,
    convert_responses_streaming_to_claude_with_cancellation,
)
from models.claude import ClaudeMessagesRequest
from providers.base import ProviderResult
from providers.openai_translate import OpenAITranslateProvider
from routing.schema import ApiFlavor, ProviderCfg, RouteLimits
from services.reasoning_cache import ReasoningCache
from settings import UpstreamSettings

_BASE_URL = "https://gateway.example/v1"
_CLAUDE_MODEL = "claude-sonnet-4"
_MODEL = "deepseek-v4-flash"
_ENDPOINT_PATH: dict[ApiFlavor, str] = {
    "chat": "/chat/completions",
    "responses": "/responses",
}
_USAGE = {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}
_TOOL_CALL = {
    "id": "call_01",
    "type": "function",
    "function": {"name": "ls", "arguments": '{"path": "src"}'},
}
_ABORT_EVENT = "upstream stream aborted; closing anthropic stream gracefully"


class _ConnectedChannel:
    """Client channel that never disconnects."""

    async def is_disconnected(self) -> bool:
        """Report the client as still connected."""
        return False


class _DisconnectedChannel:
    """Client channel whose client has already walked away."""

    async def is_disconnected(self) -> bool:
        """Report the client as gone, so the converter cancels and stops."""
        return True


class _StubUpstream:
    """Minimal stand-in for the provider the stream converters cancel through."""

    name = "gateway"

    def cancel_request(self, request_id: str) -> bool:
        """Accept the cancellation the converter issues on a disconnect."""
        return True


class _LogRecorder:
    """Stand-in for the provider module logger that records structlog-style events."""

    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def __getattr__(self, level: str) -> Callable[..., None]:
        def record(event: str, **fields: Any) -> None:
            self.events.append({"event": event, "level": level, **fields})

        return record

    def named(self, event: str) -> list[dict[str, Any]]:
        """All recorded events with the given name."""
        return [entry for entry in self.events if entry["event"] == event]


@pytest.fixture
def provider_log(monkeypatch: pytest.MonkeyPatch) -> _LogRecorder:
    """Replace the provider module logger with a recorder for the test's duration."""
    recorder = _LogRecorder()
    monkeypatch.setattr(openai_translate_module, "logger", recorder)
    return recorder


def _provider(api_flavor: ApiFlavor = "chat") -> OpenAITranslateProvider:
    """Create a provider on a real AsyncOpenAI so the SDK parses the mocked upstream body."""
    cfg = ProviderCfg(
        type="openai-translate",
        base_url=_BASE_URL,
        api_key_env="GATEWAY_API_KEY",
        api_flavor=api_flavor,
        max_tokens_limit=8192,
    )
    return OpenAITranslateProvider(
        name="gateway",
        cfg=cfg,
        api_key=SecretStr("test-gateway-key"),
        ca_bundle_path=None,
        upstream=UpstreamSettings(),
    )


def _claude_body(**overrides: Any) -> bytes:
    """A minimal Anthropic request body with overridable fields."""
    payload: dict[str, Any] = {
        "model": _CLAUDE_MODEL,
        "max_tokens": 128,
        "messages": [{"role": "user", "content": "Hello"}],
    }
    payload.update(overrides)
    return json.dumps(payload).encode()


def _chat_completion_body(
    content: str | None,
    *,
    tool_calls: list[dict[str, Any]] | None = None,
    finish_reason: str = "stop",
) -> dict[str, Any]:
    """A non-streaming Chat Completions body with the given assistant message."""
    message: dict[str, Any] = {"role": "assistant", "content": content}
    if tool_calls is not None:
        message["tool_calls"] = tool_calls
    return {
        "id": "chatcmpl-test",
        "object": "chat.completion",
        "created": 0,
        "model": _MODEL,
        "choices": [{"index": 0, "message": message, "finish_reason": finish_reason}],
        "usage": _USAGE,
    }


def _responses_body(output: list[dict[str, Any]], *, status: str = "completed") -> dict[str, Any]:
    """A non-streaming Responses API body (also the terminal object of its stream)."""
    return {
        "id": "resp_test",
        "object": "response",
        "created_at": 0,
        "model": _MODEL,
        "status": status,
        "output": output,
        "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
    }


def _output_text_item(text: str) -> dict[str, Any]:
    """A Responses ``message`` output item with one ``output_text`` part."""
    return {
        "type": "message",
        "id": "msg_test",
        "role": "assistant",
        "status": "completed",
        "content": [{"type": "output_text", "text": text, "annotations": []}],
    }


def _chat_sse(*deltas: dict[str, Any], finish_reason: str = "stop") -> str:
    """A Chat Completions SSE body: the given deltas, a finish chunk, a usage chunk, [DONE]."""

    def chunk(choices: list[dict[str, Any]], **extra: Any) -> dict[str, Any]:
        return {
            "id": "chatcmpl-test",
            "object": "chat.completion.chunk",
            "created": 0,
            "model": _MODEL,
            "choices": choices,
            **extra,
        }

    chunks = [chunk([{"index": 0, "delta": delta, "finish_reason": None}]) for delta in deltas]
    chunks.append(chunk([{"index": 0, "delta": {}, "finish_reason": finish_reason}]))
    chunks.append(chunk([], usage=_USAGE))
    return "".join(f"data: {json.dumps(item)}\n\n" for item in chunks) + "data: [DONE]\n\n"


def _responses_sse(*events: dict[str, Any], final: dict[str, Any]) -> str:
    """A Responses API SSE body: ``response.created``, the given events, ``response.completed``."""
    frames = [
        {"type": "response.created", "sequence_number": 0, "response": _responses_body([])},
        *events,
        {"type": "response.completed", "sequence_number": len(events) + 1, "response": final},
    ]
    return "".join(f"event: {frame['type']}\ndata: {json.dumps(frame)}\n\n" for frame in frames)


async def _handle(
    provider: OpenAITranslateProvider,
    claude_body: bytes,
    channel: _ConnectedChannel | _DisconnectedChannel | None = None,
) -> ProviderResult:
    """Run ``handle_messages`` and, for a stream, drain it before closing the provider."""
    try:
        result = await provider.handle_messages(
            claude_body,
            {},
            channel or _ConnectedChannel(),
            _MODEL,
            RouteLimits.resolve(provider.cfg, None),
        )
        assert result.status_code == 200
        if not isinstance(result.body, bytes):
            async for _ in result.body:
                pass
        return result
    finally:
        await provider.aclose()


def _assert_single_warning(provider_log: _LogRecorder, *, stream: bool, finish_reason: str) -> None:
    """Exactly one ``empty_completion`` warning with the provider-level fields."""
    events = provider_log.named("empty_completion")
    assert len(events) == 1
    assert events[0]["level"] == "warning"
    assert events[0]["provider"] == "gateway"
    assert events[0]["model"] == _CLAUDE_MODEL
    assert events[0]["upstream_model"] == _MODEL
    assert events[0]["stream"] is stream
    assert events[0]["finish_reason"] == finish_reason


@pytest.mark.parametrize("content", ["", None])
async def test_non_streaming_chat_completion_without_text_or_tool_calls_logs_warning(
    httpx_mock: HTTPXMock, provider_log: _LogRecorder, content: str | None
) -> None:
    """An empty (or null) message with no tool_calls is logged as empty_completion."""
    httpx_mock.add_response(
        url=_BASE_URL + _ENDPOINT_PATH["chat"], json=_chat_completion_body(content)
    )

    await _handle(_provider("chat"), _claude_body())

    _assert_single_warning(provider_log, stream=False, finish_reason="stop")


async def test_non_streaming_chat_completion_with_text_does_not_log_warning(
    httpx_mock: HTTPXMock, provider_log: _LogRecorder
) -> None:
    """A normal text completion produces no empty_completion event."""
    httpx_mock.add_response(
        url=_BASE_URL + _ENDPOINT_PATH["chat"], json=_chat_completion_body("Hi")
    )

    await _handle(_provider("chat"), _claude_body())

    assert provider_log.named("empty_completion") == []


async def test_non_streaming_chat_completion_with_tool_call_only_does_not_log_warning(
    httpx_mock: HTTPXMock, provider_log: _LogRecorder
) -> None:
    """A tool call with null content is a real completion, not an empty one."""
    httpx_mock.add_response(
        url=_BASE_URL + _ENDPOINT_PATH["chat"],
        json=_chat_completion_body(None, tool_calls=[_TOOL_CALL], finish_reason="tool_calls"),
    )

    await _handle(_provider("chat"), _claude_body())

    assert provider_log.named("empty_completion") == []


async def test_non_streaming_chat_completion_cut_by_length_reports_its_finish_reason(
    httpx_mock: HTTPXMock, provider_log: _LogRecorder
) -> None:
    """The upstream finish_reason is carried verbatim so a zero-budget cut is distinguishable."""
    httpx_mock.add_response(
        url=_BASE_URL + _ENDPOINT_PATH["chat"],
        json=_chat_completion_body("", finish_reason="length"),
    )

    await _handle(_provider("chat"), _claude_body())

    _assert_single_warning(provider_log, stream=False, finish_reason="length")


async def test_non_streaming_responses_completion_with_empty_text_logs_warning(
    httpx_mock: HTTPXMock, provider_log: _LogRecorder
) -> None:
    """Responses flavor: an empty output_text is logged; finish_reason carries the status."""
    httpx_mock.add_response(
        url=_BASE_URL + _ENDPOINT_PATH["responses"],
        json=_responses_body([_output_text_item("")]),
    )

    await _handle(_provider("responses"), _claude_body())

    _assert_single_warning(provider_log, stream=False, finish_reason="completed")


async def test_non_streaming_responses_completion_with_text_does_not_log_warning(
    httpx_mock: HTTPXMock, provider_log: _LogRecorder
) -> None:
    """Responses flavor: a normal text completion produces no event."""
    httpx_mock.add_response(
        url=_BASE_URL + _ENDPOINT_PATH["responses"],
        json=_responses_body([_output_text_item("Hi")]),
    )

    await _handle(_provider("responses"), _claude_body())

    assert provider_log.named("empty_completion") == []


async def test_streaming_chat_completion_without_text_or_tool_calls_logs_warning(
    httpx_mock: HTTPXMock, provider_log: _LogRecorder
) -> None:
    """A stream of an empty role delta and an immediate stop is logged as empty_completion."""
    httpx_mock.add_response(
        url=_BASE_URL + _ENDPOINT_PATH["chat"],
        headers={"content-type": "text/event-stream"},
        text=_chat_sse({"role": "assistant", "content": ""}),
    )

    await _handle(_provider("chat"), _claude_body(stream=True))

    _assert_single_warning(provider_log, stream=True, finish_reason="stop")


async def test_streaming_chat_completion_with_text_delta_does_not_log_warning(
    httpx_mock: HTTPXMock, provider_log: _LogRecorder
) -> None:
    """One non-empty text delta after an empty role delta is a real completion."""
    httpx_mock.add_response(
        url=_BASE_URL + _ENDPOINT_PATH["chat"],
        headers={"content-type": "text/event-stream"},
        text=_chat_sse({"role": "assistant", "content": ""}, {"content": "Hi"}),
    )

    await _handle(_provider("chat"), _claude_body(stream=True))

    assert provider_log.events == []


async def test_streaming_chat_completion_with_tool_call_does_not_log_warning(
    httpx_mock: HTTPXMock, provider_log: _LogRecorder
) -> None:
    """A streamed tool call with no text is a real completion."""
    httpx_mock.add_response(
        url=_BASE_URL + _ENDPOINT_PATH["chat"],
        headers={"content-type": "text/event-stream"},
        text=_chat_sse(
            {"role": "assistant", "content": None},
            {"tool_calls": [{"index": 0, **_TOOL_CALL}]},
            finish_reason="tool_calls",
        ),
    )

    await _handle(_provider("chat"), _claude_body(stream=True))

    assert provider_log.events == []


async def test_streaming_responses_completion_without_output_logs_warning(
    httpx_mock: HTTPXMock, provider_log: _LogRecorder
) -> None:
    """Responses flavor: a stream that completes with no output items is logged."""
    httpx_mock.add_response(
        url=_BASE_URL + _ENDPOINT_PATH["responses"],
        headers={"content-type": "text/event-stream"},
        text=_responses_sse(final=_responses_body([])),
    )

    await _handle(_provider("responses"), _claude_body(stream=True))

    _assert_single_warning(provider_log, stream=True, finish_reason="completed")


async def test_streaming_responses_completion_with_text_delta_does_not_log_warning(
    httpx_mock: HTTPXMock, provider_log: _LogRecorder
) -> None:
    """Responses flavor: a non-empty output_text delta is a real completion."""
    text_delta = {
        "type": "response.output_text.delta",
        "sequence_number": 1,
        "item_id": "msg_test",
        "output_index": 0,
        "content_index": 0,
        "delta": "Hi",
        "logprobs": [],
    }
    httpx_mock.add_response(
        url=_BASE_URL + _ENDPOINT_PATH["responses"],
        headers={"content-type": "text/event-stream"},
        text=_responses_sse(text_delta, final=_responses_body([_output_text_item("Hi")])),
    )

    await _handle(_provider("responses"), _claude_body(stream=True))

    assert provider_log.events == []


async def _drain(stream: AsyncIterator[str]) -> None:
    """Consume an Anthropic SSE stream to completion."""
    async for _ in stream:
        pass


def _aborted_stream(*lines: str) -> AsyncIterator[str]:
    """An upstream SSE stream that breaks mid-flight after the given lines."""

    async def generate() -> AsyncIterator[str]:
        for line in lines:
            yield line
        raise httpx.RemoteProtocolError(
            "peer closed connection without sending complete message body"
        )

    return generate()


def _stream_request() -> ClaudeMessagesRequest:
    """The original Anthropic request the stream converters echo the model from."""
    return ClaudeMessagesRequest.model_validate(
        {
            "model": _CLAUDE_MODEL,
            "max_tokens": 128,
            "messages": [{"role": "user", "content": "Hello"}],
            "stream": True,
        }
    )


async def test_streaming_chat_client_disconnect_does_not_log_empty_completion(
    httpx_mock: HTTPXMock, provider_log: _LogRecorder
) -> None:
    """A client that walks away leaves no output -- but that is not an empty completion."""
    httpx_mock.add_response(
        url=_BASE_URL + _ENDPOINT_PATH["chat"],
        headers={"content-type": "text/event-stream"},
        text=_chat_sse({"role": "assistant", "content": "Hi"}),
    )

    await _handle(_provider("chat"), _claude_body(stream=True), _DisconnectedChannel())

    assert provider_log.named("empty_completion") == []


async def test_streaming_responses_client_disconnect_does_not_log_empty_completion(
    httpx_mock: HTTPXMock, provider_log: _LogRecorder
) -> None:
    """Responses flavor: the disconnect is already logged on its own, once."""
    httpx_mock.add_response(
        url=_BASE_URL + _ENDPOINT_PATH["responses"],
        headers={"content-type": "text/event-stream"},
        text=_responses_sse(final=_responses_body([_output_text_item("Hi")])),
    )

    await _handle(_provider("responses"), _claude_body(stream=True), _DisconnectedChannel())

    assert provider_log.named("empty_completion") == []


async def test_streaming_chat_upstream_abort_does_not_log_empty_completion(
    provider_log: _LogRecorder,
) -> None:
    """A transport break before any delta is an aborted stream, not an empty completion."""
    await _drain(
        convert_openai_streaming_to_claude_with_cancellation(
            _aborted_stream('data: {"choices":[{"delta":{"role":"assistant"}}]}'),
            _stream_request(),
            provider_log,
            _ConnectedChannel(),
            _StubUpstream(),
            "req-abort",
        )
    )

    assert provider_log.named("empty_completion") == []
    assert len(provider_log.named(_ABORT_EVENT)) == 1


async def test_streaming_responses_upstream_abort_does_not_log_empty_completion(
    provider_log: _LogRecorder,
) -> None:
    """Responses flavor: the abort is reported by its own event, not as empty_completion."""
    created = {"type": "response.created", "sequence_number": 0, "response": _responses_body([])}
    await _drain(
        convert_responses_streaming_to_claude_with_cancellation(
            _aborted_stream(f"data: {json.dumps(created)}"),
            _stream_request(),
            provider_log,
            _ConnectedChannel(),
            _StubUpstream(),
            "req-abort",
            reasoning_cache=ReasoningCache(),
        )
    )

    assert provider_log.named("empty_completion") == []
    assert len(provider_log.named(_ABORT_EVENT)) == 1


async def test_non_streaming_chat_completion_with_whitespace_only_text_logs_warning(
    httpx_mock: HTTPXMock, provider_log: _LogRecorder
) -> None:
    """Whitespace is not content: the client sees a blank turn just the same."""
    httpx_mock.add_response(
        url=_BASE_URL + _ENDPOINT_PATH["chat"], json=_chat_completion_body("  \n  ")
    )

    await _handle(_provider("chat"), _claude_body())

    _assert_single_warning(provider_log, stream=False, finish_reason="stop")


async def test_non_streaming_responses_completion_with_whitespace_only_text_logs_warning(
    httpx_mock: HTTPXMock, provider_log: _LogRecorder
) -> None:
    """Responses flavor: a whitespace-only output_text is an empty completion."""
    httpx_mock.add_response(
        url=_BASE_URL + _ENDPOINT_PATH["responses"],
        json=_responses_body([_output_text_item("  \n  ")]),
    )

    await _handle(_provider("responses"), _claude_body())

    _assert_single_warning(provider_log, stream=False, finish_reason="completed")


async def test_streaming_chat_completion_with_whitespace_only_deltas_logs_warning(
    httpx_mock: HTTPXMock, provider_log: _LogRecorder
) -> None:
    """A stream of whitespace deltas carries no answer either."""
    httpx_mock.add_response(
        url=_BASE_URL + _ENDPOINT_PATH["chat"],
        headers={"content-type": "text/event-stream"},
        text=_chat_sse({"role": "assistant", "content": " "}, {"content": "\n"}),
    )

    await _handle(_provider("chat"), _claude_body(stream=True))

    _assert_single_warning(provider_log, stream=True, finish_reason="stop")


async def test_streaming_responses_completion_with_whitespace_only_delta_logs_warning(
    httpx_mock: HTTPXMock, provider_log: _LogRecorder
) -> None:
    """Responses flavor: a whitespace-only output_text delta is an empty completion."""
    text_delta = {
        "type": "response.output_text.delta",
        "sequence_number": 1,
        "item_id": "msg_test",
        "output_index": 0,
        "content_index": 0,
        "delta": "  ",
        "logprobs": [],
    }
    httpx_mock.add_response(
        url=_BASE_URL + _ENDPOINT_PATH["responses"],
        headers={"content-type": "text/event-stream"},
        text=_responses_sse(text_delta, final=_responses_body([_output_text_item("  ")])),
    )

    await _handle(_provider("responses"), _claude_body(stream=True))

    _assert_single_warning(provider_log, stream=True, finish_reason="completed")
