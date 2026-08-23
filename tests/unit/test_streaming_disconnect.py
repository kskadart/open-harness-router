"""Unit tests for the resilience of the SSE converter and provider to disconnects and errors.

Verify:

- ``convert_openai_streaming_to_claude_with_cancellation``: on a transport
  abort (``httpx.RemoteProtocolError`` -- incomplete chunked read) the
  generator does not raise the exception outward, it finishes the Anthropic
  stream with terminal events, preserving the partial content already sent.
- Cooperative client cancellation (ProviderError 499) is not masked by the
  new handlers.
- Post-start upstream errors (ProviderError/AuthenticationError non-499)
  after the stream has started are closed with a proper ``event: error``
  instead of a ``raise`` (which would tear down the TCP socket -> the client
  sees ECONNRESET).
- ``OpenAITranslateProvider.create_stream`` retries a transient 401
  "insufficient permissions" before the stream starts, and returns a proper
  JSON response once retries are exhausted.
- ``PassthroughProvider``: an upstream disconnect mid-byte-for-byte-stream
  does not crash the ASGI app, it finishes the stream with an
  ``event: error`` frame; a client leaving finishes the stream silently;
  transport failures before the stream starts and on unary paths become
  ``UpstreamError`` with a neutral message.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from openai import APIError, AuthenticationError

from conversion.response_converter import (
    convert_openai_streaming_to_claude_with_cancellation,
)
from errors import ProviderError, UpstreamError
from models.claude import ClaudeMessagesRequest
from providers.openai_translate import OpenAITranslateProvider
from providers.passthrough import PassthroughProvider
from routing.schema import ProviderCfg


def _request() -> ClaudeMessagesRequest:
    """A minimal Anthropic request (the converter only needs the ``model`` field)."""
    return ClaudeMessagesRequest.model_validate(
        {
            "model": "zai-org/GLM-5.2-FP8",
            "max_tokens": 128,
            "messages": [{"role": "user", "content": "hi"}],
        }
    )


def _parse_events(chunks: list[str]) -> list[tuple[str, dict[str, Any]]]:
    """Parse the assembled SSE frames into ``(event_name, payload)`` pairs."""
    events: list[tuple[str, dict[str, Any]]] = []
    for chunk in chunks:
        event_name: str | None = None
        data_json: str | None = None
        for raw_line in chunk.split("\n"):
            if raw_line.startswith("event: "):
                event_name = raw_line[len("event: ") :]
            elif raw_line.startswith("data: "):
                data_json = raw_line[len("data: ") :]
        assert event_name is not None
        events.append((event_name, json.loads(data_json) if data_json else {}))
    return events


def _provider() -> OpenAITranslateProvider:
    """Create a provider with a mocked AsyncOpenAI for retry-logic tests."""
    cfg = ProviderCfg(
        type="openai-translate",
        base_url="https://api.openai.com/v1",
        api_key_env="OPENAI_API_KEY",
        token_param="max_completion_tokens",
        drop_params=["temperature"],
        max_tokens_limit=128000,
    )
    provider = OpenAITranslateProvider.__new__(OpenAITranslateProvider)
    provider.name = "openai"
    provider.cfg = cfg
    provider.active_requests = {}
    provider._client = MagicMock()  # type: ignore[assignment]
    provider._http_client = MagicMock()  # type: ignore[assignment]
    return provider


def _auth_error(message: str = "insufficient permissions") -> AuthenticationError:
    """Create an AuthenticationError with the given message."""
    request = httpx.Request("POST", "https://api.openai.com/v1/chat/completions")
    response = httpx.Response(
        status_code=401,
        request=request,
        content=json.dumps({"error": {"message": message}}).encode(),
    )
    return AuthenticationError(message=message, response=response, body=None)


def _chunk_stream(*chunks: str) -> AsyncIterator[Any]:
    """Create an async iterator from ready-made chunk objects."""

    async def _gen() -> AsyncIterator[Any]:
        for chunk in chunks:
            yield chunk

    return _gen()


# ---------------------------------------------------------------------------
# Regression: transport abort mid-stream (httpx.RemoteProtocolError)
# ---------------------------------------------------------------------------


async def test_upstream_disconnect_midstream_closes_stream_gracefully() -> None:
    async def broken_stream() -> AsyncIterator[str]:
        yield 'data: {"choices":[{"delta":{"role":"assistant"},"finish_reason":null}]}'
        yield 'data: {"choices":[{"delta":{"content":"Hello"},"finish_reason":null}]}'
        yield 'data: {"choices":[{"delta":{"content":" world"},"finish_reason":null}]}'
        raise httpx.RemoteProtocolError(
            "peer closed connection without sending complete message body "
            "(incomplete chunked read)"
        )

    logger = MagicMock()
    http_request = MagicMock()
    http_request.is_disconnected = AsyncMock(return_value=False)
    client = MagicMock()
    client.name = "openai_compatible"

    generator = convert_openai_streaming_to_claude_with_cancellation(
        broken_stream(), _request(), logger, http_request, client, "req-123"
    )
    # The generator must finish normally, without raising the httpx exception.
    chunks = [chunk async for chunk in generator]
    events = _parse_events(chunks)
    names = [name for name, _ in events]

    # The protocol prologue.
    assert names[0] == "message_start"
    assert names[1] == "content_block_start"
    assert names[2] == "ping"

    # The partial content already sent reached the client.
    text = "".join(
        payload["delta"]["text"]
        for name, payload in events
        if name == "content_block_delta"
    )
    assert text == "Hello world"

    # The stream is closed with a valid terminal sequence.
    assert names[-3:] == ["content_block_stop", "message_delta", "message_stop"]
    stop_indices = [
        payload["index"] for name, payload in events if name == "content_block_stop"
    ]
    assert 0 in stop_indices  # the open text block is closed

    message_delta = next(
        payload for name, payload in events if name == "message_delta"
    )
    assert message_delta["delta"]["stop_reason"] == "end_turn"

    # A transport abort must NOT turn into an error event.
    assert "error" not in names

    # The abort is logged structurally at warning level.
    assert logger.warning.called
    warn_kwargs = logger.warning.call_args.kwargs
    assert warn_kwargs["provider"] == "openai_compatible"
    assert warn_kwargs["request_id"] == "req-123"
    assert warn_kwargs["error_type"] == "RemoteProtocolError"

    # This is not a client cancellation -- cancel_request must not be called.
    client.cancel_request.assert_not_called()


# ---------------------------------------------------------------------------
# Regression: client cancellation (499) is not masked by the new handlers
# ---------------------------------------------------------------------------


async def test_client_cancellation_499_still_emits_cancelled_and_is_not_masked() -> None:
    async def cancelled_stream() -> AsyncIterator[str]:
        yield 'data: {"choices":[{"delta":{"content":"partial"},"finish_reason":null}]}'
        raise ProviderError(message="Request cancelled by client", status_code=499)

    logger = MagicMock()
    http_request = MagicMock()
    http_request.is_disconnected = AsyncMock(return_value=False)
    client = MagicMock()
    client.name = "openai_compatible"

    generator = convert_openai_streaming_to_claude_with_cancellation(
        cancelled_stream(), _request(), logger, http_request, client, "req-499"
    )
    chunks = [chunk async for chunk in generator]
    events = _parse_events(chunks)
    names = [name for name, _ in events]

    # Cancellation -> a separate error event of type cancelled.
    assert "error" in names
    error_event = next(payload for name, payload in events if name == "error")
    assert error_event["error"]["type"] == "cancelled"

    # Early return: the terminal message_stop is not sent after cancelled.
    assert "message_stop" not in names

    # The cancellation path is unaffected by the new httpx handler (no abort warnings).
    logger.warning.assert_not_called()


# ---------------------------------------------------------------------------
# Part B: ProviderError (non-499) mid-stream -> error-event, not raise
# ---------------------------------------------------------------------------


async def test_http_exception_midstream_emits_error_event_not_raise() -> None:
    """ProviderError(503) after the stream starts -> error-event, no ECONNRESET."""

    async def error_stream() -> AsyncIterator[str]:
        yield 'data: {"choices":[{"delta":{"content":"partial"},"finish_reason":null}]}'
        raise ProviderError(message="Upstream unavailable", status_code=503)

    logger = MagicMock()
    http_request = MagicMock()
    http_request.is_disconnected = AsyncMock(return_value=False)
    client = MagicMock()
    client.name = "openai"

    generator = convert_openai_streaming_to_claude_with_cancellation(
        error_stream(), _request(), logger, http_request, client, "req-503"
    )
    chunks = [chunk async for chunk in generator]
    events = _parse_events(chunks)
    names = [name for name, _ in events]

    # The preamble was sent before the error.
    assert names[:3] == ["message_start", "content_block_start", "ping"]

    # partial content reached the client.
    text = "".join(
        payload["delta"]["text"]
        for name, payload in events
        if name == "content_block_delta"
    )
    assert text == "partial"

    # error-event of type api_error (503 -> overloaded_error).
    assert "error" in names
    error_event = next(payload for name, payload in events if name == "error")
    assert error_event["error"]["type"] == "overloaded_error"
    assert "Upstream unavailable" in error_event["error"]["message"]

    # The terminal sequence is NOT sent -- upstream was interrupted.
    assert "message_stop" not in names

    # The error is logged structurally.
    assert logger.warning.called


async def test_authentication_error_midstream_emits_authentication_error_event() -> None:
    """AuthenticationError(401) after the stream starts -> authentication_error event."""

    async def auth_error_stream() -> AsyncIterator[str]:
        yield 'data: {"choices":[{"delta":{"content":"hi"},"finish_reason":null}]}'
        raise _auth_error("insufficient permissions")

    logger = MagicMock()
    http_request = MagicMock()
    http_request.is_disconnected = AsyncMock(return_value=False)
    client = MagicMock()
    client.name = "openai"

    generator = convert_openai_streaming_to_claude_with_cancellation(
        auth_error_stream(), _request(), logger, http_request, client, "req-auth"
    )
    chunks = [chunk async for chunk in generator]
    events = _parse_events(chunks)
    names = [name for name, _ in events]

    # error-event of type authentication_error (401).
    assert "error" in names
    error_event = next(payload for name, payload in events if name == "error")
    assert error_event["error"]["type"] == "authentication_error"

    # The terminal sequence is NOT sent.
    assert "message_stop" not in names


async def test_api_error_midstream_emits_error_event_not_raise() -> None:
    """APIError (not AuthenticationError) mid-stream -> error-event."""

    async def api_error_stream() -> AsyncIterator[str]:
        yield 'data: {"choices":[{"delta":{"content":"data"},"finish_reason":null}]}'
        request = httpx.Request("POST", "https://api.openai.com/v1/chat/completions")
        raise APIError(message="stream error event", request=request, body=None)

    logger = MagicMock()
    http_request = MagicMock()
    http_request.is_disconnected = AsyncMock(return_value=False)
    client = MagicMock()
    client.name = "openai"

    generator = convert_openai_streaming_to_claude_with_cancellation(
        api_error_stream(), _request(), logger, http_request, client, "req-apierr"
    )
    chunks = [chunk async for chunk in generator]
    events = _parse_events(chunks)
    names = [name for name, _ in events]

    assert "error" in names
    error_event = next(payload for name, payload in events if name == "error")
    assert error_event["error"]["type"] == "api_error"
    assert "message_stop" not in names


# ---------------------------------------------------------------------------
# Part A: retry transient 401 before the stream starts (provider level)
# ---------------------------------------------------------------------------


async def test_create_stream_retries_transient_401_then_succeeds() -> None:
    """The first create(stream=True) raises transient 401, the second succeeds."""
    provider = _provider()

    success_stream = MagicMock()
    provider._client.chat.completions.create = AsyncMock(
        side_effect=[_auth_error("insufficient permissions"), success_stream]
    )

    request: dict[str, Any] = {"model": "gpt-5.6", "stream": True}
    result = await provider.create_stream(request, "req-retry")

    assert result is success_stream
    assert provider._client.chat.completions.create.await_count == 2
    # active_requests was not cleared -- the stream has not been iterated yet.
    assert "req-retry" in provider.active_requests


async def test_create_stream_non_transient_401_not_retried() -> None:
    """invalid_api_key 401 is NOT retried -- it is raised immediately."""
    provider = _provider()
    provider._client.chat.completions.create = AsyncMock(
        side_effect=_auth_error("invalid_api_key")
    )

    request: dict[str, Any] = {"model": "gpt-5.6", "stream": True}
    with pytest.raises(ProviderError) as exc_info:
        await provider.create_stream(request, "req-nokey")

    assert exc_info.value.status_code == 401
    assert provider._client.chat.completions.create.await_count == 1


async def test_create_stream_exhausts_retries_returns_401() -> None:
    """All 3 attempts -- transient 401 -> ProviderError(401) is raised."""
    provider = _provider()
    provider._client.chat.completions.create = AsyncMock(
        side_effect=[
            _auth_error("insufficient permissions"),
            _auth_error("insufficient permissions"),
            _auth_error("insufficient permissions"),
        ]
    )

    request: dict[str, Any] = {"model": "gpt-5.6", "stream": True}
    with pytest.raises(ProviderError) as exc_info:
        await provider.create_stream(request, "req-exhaust")

    assert exc_info.value.status_code == 401
    assert provider._client.chat.completions.create.await_count == 3


async def test_create_stream_cancellation_during_retry_raises_499() -> None:
    """Client cancellation during the retry loop -> ProviderError(499)."""
    provider = _provider()
    # Pre-populate cancel_event for the request_id.
    cancel_event = asyncio.Event()
    cancel_event.set()
    provider.active_requests["req-cancel"] = cancel_event

    provider._client.chat.completions.create = AsyncMock(return_value=MagicMock())

    request: dict[str, Any] = {"model": "gpt-5.6", "stream": True}
    with pytest.raises(ProviderError) as exc_info:
        await provider.create_stream(request, "req-cancel")

    assert exc_info.value.status_code == 499
    # create is not called -- cancellation is checked before the first contact.
    provider._client.chat.completions.create.assert_not_called()


# ---------------------------------------------------------------------------
# Passthrough: upstream abort mid-byte-for-byte-stream and on unary paths
# ---------------------------------------------------------------------------


def _passthrough_provider() -> PassthroughProvider:
    """Create a passthrough provider with a mocked httpx client."""
    cfg = ProviderCfg(type="passthrough", base_url="https://api.anthropic.com")
    provider = PassthroughProvider.__new__(PassthroughProvider)
    provider.name = "anthropic"
    provider.cfg = cfg
    # forward_client_auth defaults to true on cfg above -> no own key.
    provider._api_key = None
    # Retries are disabled: the tests exercise single-attempt failure
    # handling, and the retry pauses would stretch every connect-failure
    # test by tens of seconds.
    provider._retry_delays = []
    provider._client = MagicMock()  # type: ignore[assignment]
    provider._client.build_request.return_value = httpx.Request(
        "POST", "https://api.anthropic.com/v1/messages"
    )
    return provider


def _upstream_stream(chunks: AsyncIterator[bytes]) -> MagicMock:
    """Create an upstream response that yields the given byte stream via aiter_raw."""
    upstream = MagicMock()
    upstream.status_code = 200
    upstream.headers = {"content-type": "text/event-stream"}
    upstream.aiter_raw.return_value = chunks
    upstream.aclose = AsyncMock()
    return upstream


def _last_sse_frame(body: bytes) -> tuple[str, dict[str, Any]]:
    """Parse the last SSE frame of the byte stream into an (event, payload) pair."""
    frames = [frame for frame in body.split(b"\n\n") if frame.strip()]
    event_name = ""
    payload: dict[str, Any] = {}
    for line in frames[-1].decode().split("\n"):
        if line.startswith("event: "):
            event_name = line[len("event: ") :]
        elif line.startswith("data: "):
            payload = json.loads(line[len("data: ") :])
    return event_name, payload


async def _drain_passthrough(
    provider: PassthroughProvider, upstream: MagicMock, http_request: MagicMock
) -> bytes:
    """Drain the passthrough stream to completion and collect the bytes sent to the client."""
    provider._client.send = AsyncMock(return_value=upstream)
    result = await provider._proxy_stream(
        "/v1/messages", b'{"stream":true}', {}, http_request
    )
    # The streaming path returns the body as an async byte iterator, not ready-made bytes.
    assert not isinstance(result.body, bytes)
    return b"".join([chunk async for chunk in result.body])


async def test_passthrough_upstream_disconnect_midstream_emits_error_event() -> None:
    """An Anthropic abort mid-passthrough -> partial + an event: error frame."""
    partial = b'event: content_block_delta\ndata: {"type":"content_block_delta"}\n\n'

    async def broken_stream() -> AsyncIterator[bytes]:
        yield partial
        raise httpx.RemoteProtocolError(
            "peer closed connection without sending complete message body "
            "(incomplete chunked read)"
        )

    provider = _passthrough_provider()
    upstream = _upstream_stream(broken_stream())
    http_request = MagicMock()
    http_request.is_disconnected = AsyncMock(return_value=False)

    body = await _drain_passthrough(provider, upstream, http_request)

    assert body.startswith(partial)
    event_name, payload = _last_sse_frame(body)
    assert event_name == "error"
    assert payload == {
        "type": "error",
        "error": {
            "type": "api_error",
            "message": "Upstream stream interrupted. Retry the request.",
        },
    }
    upstream.aclose.assert_awaited_once()


async def test_passthrough_upstream_read_timeout_midstream_emits_error_event() -> None:
    """An upstream read timeout mid-passthrough -> an event: error frame."""

    async def stalled_stream() -> AsyncIterator[bytes]:
        raise httpx.ReadTimeout("upstream read timed out")
        yield b""  # pragma: no cover

    provider = _passthrough_provider()
    upstream = _upstream_stream(stalled_stream())
    http_request = MagicMock()
    http_request.is_disconnected = AsyncMock(return_value=False)

    body = await _drain_passthrough(provider, upstream, http_request)

    event_name, payload = _last_sse_frame(body)
    assert event_name == "error"
    assert payload["error"]["message"] == "Upstream stream interrupted. Retry the request."
    upstream.aclose.assert_awaited_once()


async def test_passthrough_upstream_error_leaks_no_internal_details_to_client() -> None:
    """The upstream exception text does not leak to the client in the error frame."""
    secret_detail = "incomplete chunked read from api.anthropic.com pool 0x7f"

    async def broken_stream() -> AsyncIterator[bytes]:
        yield b'event: ping\ndata: {"type":"ping"}\n\n'
        raise httpx.RemoteProtocolError(secret_detail)

    provider = _passthrough_provider()
    upstream = _upstream_stream(broken_stream())
    http_request = MagicMock()
    http_request.is_disconnected = AsyncMock(return_value=False)

    body = await _drain_passthrough(provider, upstream, http_request)

    assert secret_detail.encode() not in body


async def test_passthrough_client_disconnect_midstream_stops_without_error_event() -> None:
    """A client leaving during passthrough -> a silent stop without an error frame."""

    async def upstream_chunks() -> AsyncIterator[bytes]:
        yield b'event: message_start\ndata: {"type":"message_start"}\n\n'

    provider = _passthrough_provider()
    upstream = _upstream_stream(upstream_chunks())
    http_request = MagicMock()
    http_request.is_disconnected = AsyncMock(return_value=True)

    body = await _drain_passthrough(provider, upstream, http_request)

    assert body == b""
    upstream.aclose.assert_awaited_once()


async def test_passthrough_upstream_disconnect_after_client_left_omits_error_event() -> None:
    """Upstream disconnected after the client already left -> no one to send the error frame to."""
    partial = b'event: ping\ndata: {"type":"ping"}\n\n'

    async def broken_stream() -> AsyncIterator[bytes]:
        yield partial
        raise httpx.RemoteProtocolError("incomplete chunked read")

    provider = _passthrough_provider()
    upstream = _upstream_stream(broken_stream())
    http_request = MagicMock()
    # The first check -- client present; the second (after the abort) -- already disconnected.
    http_request.is_disconnected = AsyncMock(side_effect=[False, True])

    body = await _drain_passthrough(provider, upstream, http_request)

    assert body == partial
    assert b"event: error" not in body
    upstream.aclose.assert_awaited_once()


async def test_passthrough_connect_error_before_stream_raises_upstream_error() -> None:
    """A connection error before the stream starts -> UpstreamError 502 without details."""
    provider = _passthrough_provider()
    provider._client.send = AsyncMock(
        side_effect=httpx.ConnectError("connection refused by api.anthropic.com")
    )

    with pytest.raises(UpstreamError) as exc_info:
        await provider._proxy_stream(
            "/v1/messages", b'{"stream":true}', {}, MagicMock()
        )

    assert exc_info.value.status_code == 502
    assert exc_info.value.message == "Upstream request failed. Retry the request."


async def test_passthrough_timeout_before_stream_raises_gateway_timeout() -> None:
    """A timeout before the stream starts -> UpstreamError 504 without details."""
    provider = _passthrough_provider()
    provider._client.send = AsyncMock(side_effect=httpx.ConnectTimeout("connect timed out"))

    with pytest.raises(UpstreamError) as exc_info:
        await provider._proxy_stream(
            "/v1/messages", b'{"stream":true}', {}, MagicMock()
        )

    assert exc_info.value.status_code == 504
    assert exc_info.value.message == "Upstream request timed out. Retry the request."


async def test_passthrough_unary_disconnect_raises_upstream_error() -> None:
    """An upstream abort on a non-streaming request -> UpstreamError 502."""
    provider = _passthrough_provider()
    provider._client.post = AsyncMock(
        side_effect=httpx.RemoteProtocolError("Server disconnected without sending a response.")
    )

    with pytest.raises(UpstreamError) as exc_info:
        await provider._proxy_unary("/v1/messages", b"{}", {})

    assert exc_info.value.status_code == 502
    assert exc_info.value.message == "Upstream request failed. Retry the request."


async def test_passthrough_unary_timeout_raises_gateway_timeout() -> None:
    """A non-streaming request timeout -> UpstreamError 504."""
    provider = _passthrough_provider()
    provider._client.post = AsyncMock(side_effect=httpx.ReadTimeout("read timed out"))

    with pytest.raises(UpstreamError) as exc_info:
        await provider._proxy_unary("/v1/messages", b"{}", {})

    assert exc_info.value.status_code == 504
    assert exc_info.value.message == "Upstream request timed out. Retry the request."


async def test_passthrough_count_tokens_disconnect_raises_upstream_error() -> None:
    """An upstream abort on count_tokens -> UpstreamError 502."""
    provider = _passthrough_provider()
    provider._client.post = AsyncMock(
        side_effect=httpx.RemoteProtocolError("Server disconnected without sending a response.")
    )

    with pytest.raises(UpstreamError) as exc_info:
        await provider.count_tokens(b"{}", {}, None)

    assert exc_info.value.status_code == 502
    assert exc_info.value.message == "Upstream request failed. Retry the request."
