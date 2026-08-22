"""Unit tests for the OpenAI Responses API (/v1/responses) response converters.

The fixtures were assembled from real ``gpt-5.6-sol`` streams: event types,
order, field names (``call_id``, ``arguments``, ``input_tokens``/
``output_tokens``, ``input_tokens_details.cached_tokens``), and terminal
events (``response.completed`` / ``response.incomplete``) were captured from
the live API.

Verify:

- ``convert_responses_to_claude_response``: parsing the flat ``output[]``
  (message / function_call / reasoning) instead of ``choices[]``.
- ``convert_responses_streaming_to_claude_with_cancellation``: the same
  Anthropic SSE event sequence as the Chat Completions version, including
  the overlap of the text block with tool blocks and a shared terminal tail.
- Protection against a silent failure: Responses API events must not be
  silently lost, and an unfamiliar event type must be logged without
  breaking the stream.
- Behavior on client disconnect, cancellation (499), an upstream error, and
  a transport abort -- identical to the Chat Completions version.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from conversion.response_converter import (
    convert_openai_streaming_to_claude_with_cancellation,
    convert_responses_streaming_to_claude_with_cancellation,
    convert_responses_to_claude_response,
    extract_reasoning_by_call_id,
)
from errors import ProviderError
from models.claude import ClaudeMessagesRequest
from services.reasoning_cache import ReasoningCache

# Identifiers and values from real gpt-5.6-sol responses.
_RESPONSE_ID = "resp_0f0230b0cf1e862e006a63d7eca6f88191976e2276ca6bd676"
_TEXT_ITEM_ID = "msg_0c6f6f1632b28ea7006a63d514026c81a19d72166293002c45"
_TOOL_ITEM_ID = "fc_0aaece5ed6ed3fb0006a63d7eb73e4819e9745c8c3677e7360"
_TOOL_CALL_ID = "call_oiAhWSjbBKCPqmjzySa6gZmu"
_TOOL_NAME = "submit_answer"
_TOOL_ARGUMENTS = '{"answer":391}'
_REASONING_ITEM_ID = "rs_0226e3ff2e40c123006a63d584cd1c81929878ad4d01848b01"
_ENCRYPTED_CONTENT = "gAAAAABo" + "x" * 512
_TOOL_ARGUMENT_DELTAS = ['{"', "answer", '":', "391", "}"]
_TEXT_DELTAS = ["2", "+", "2", "=", "4", "."]

_TEXT_USAGE = {
    "input_tokens": 23,
    "input_tokens_details": {"cache_write_tokens": 0, "cached_tokens": 0},
    "output_tokens": 10,
    "output_tokens_details": {"reasoning_tokens": 0},
    "total_tokens": 33,
}
_TOOL_USAGE = {
    "input_tokens": 61,
    "input_tokens_details": {"cache_write_tokens": 0, "cached_tokens": 0},
    "output_tokens": 18,
    "output_tokens_details": {"reasoning_tokens": 0},
    "total_tokens": 79,
}


def _request() -> ClaudeMessagesRequest:
    """A minimal Anthropic request (the converter only needs the ``model`` field)."""
    return ClaudeMessagesRequest.model_validate(
        {
            "model": "gpt-5.6-sol",
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


def _text_of(events: list[tuple[str, dict[str, Any]]]) -> str:
    """Join the text deltas of an Anthropic stream."""
    return "".join(
        payload["delta"]["text"]
        for name, payload in events
        if name == "content_block_delta" and payload["delta"]["type"] == "text_delta"
    )


def _completed(
    usage: dict[str, Any], output: list[dict[str, Any]] | None = None
) -> dict[str, Any]:
    """Terminal event for a successful Responses API response."""
    response: dict[str, Any] = {
        "id": _RESPONSE_ID,
        "status": "completed",
        "usage": usage,
    }
    if output is not None:
        response["output"] = output
    return {"type": "response.completed", "response": response}


def _reasoning_item(item_id: str = _REASONING_ITEM_ID) -> dict[str, Any]:
    """A ``reasoning`` item in the shape returned by the SDK (``model_dump``)."""
    return {
        "id": item_id,
        "type": "reasoning",
        "summary": [],
        "content": [],
        "encrypted_content": _ENCRYPTED_CONTENT,
        "status": None,
    }


def _function_call_item(call_id: str = _TOOL_CALL_ID) -> dict[str, Any]:
    """A ``function_call`` item in the shape of the Responses API's final output."""
    return {
        "id": _TOOL_ITEM_ID,
        "type": "function_call",
        "status": "completed",
        "call_id": call_id,
        "name": _TOOL_NAME,
        "arguments": _TOOL_ARGUMENTS,
    }


def _text_stream_events() -> list[dict[str, Any]]:
    """Real event sequence for a text response without tools."""
    events: list[dict[str, Any]] = [
        {"type": "response.created", "response": {"id": _RESPONSE_ID, "status": "in_progress"}},
        {
            "type": "response.in_progress",
            "response": {"id": _RESPONSE_ID, "status": "in_progress"},
        },
        {
            "type": "response.output_item.added",
            "output_index": 0,
            "item": {
                "id": _TEXT_ITEM_ID,
                "type": "message",
                "role": "assistant",
                "status": "in_progress",
                "content": [],
            },
        },
        {
            "type": "response.content_part.added",
            "output_index": 0,
            "content_index": 0,
            "item_id": _TEXT_ITEM_ID,
            "part": {"type": "output_text", "text": "", "annotations": []},
        },
    ]
    events += [
        {
            "type": "response.output_text.delta",
            "output_index": 0,
            "content_index": 0,
            "item_id": _TEXT_ITEM_ID,
            "delta": delta,
        }
        for delta in _TEXT_DELTAS
    ]
    events += [
        {
            "type": "response.output_text.done",
            "output_index": 0,
            "content_index": 0,
            "item_id": _TEXT_ITEM_ID,
            "text": "".join(_TEXT_DELTAS),
        },
        {
            "type": "response.content_part.done",
            "output_index": 0,
            "content_index": 0,
            "item_id": _TEXT_ITEM_ID,
            "part": {"type": "output_text", "text": "".join(_TEXT_DELTAS), "annotations": []},
        },
        {
            "type": "response.output_item.done",
            "output_index": 0,
            "item": {
                "id": _TEXT_ITEM_ID,
                "type": "message",
                "role": "assistant",
                "status": "completed",
                "content": [{"type": "output_text", "text": "".join(_TEXT_DELTAS)}],
            },
        },
        _completed(_TEXT_USAGE),
    ]
    return events


def _tool_item(output_index: int, *, status: str, arguments: str) -> dict[str, Any]:
    """A ``function_call`` item in the shape returned by the Responses API."""
    return {
        "type": "response.output_item.added"
        if status == "in_progress"
        else "response.output_item.done",
        "output_index": output_index,
        "item": {
            "id": _TOOL_ITEM_ID,
            "type": "function_call",
            "status": status,
            "arguments": arguments,
            "call_id": _TOOL_CALL_ID,
            "name": _TOOL_NAME,
        },
    }


def _tool_stream_events(output_index: int = 0) -> list[dict[str, Any]]:
    """Real event sequence for a response with a tool call."""
    events: list[dict[str, Any]] = [_tool_item(output_index, status="in_progress", arguments="")]
    events += [
        {
            "type": "response.function_call_arguments.delta",
            "output_index": output_index,
            "item_id": _TOOL_ITEM_ID,
            "delta": delta,
        }
        for delta in _TOOL_ARGUMENT_DELTAS
    ]
    events += [
        {
            "type": "response.function_call_arguments.done",
            "output_index": output_index,
            "item_id": _TOOL_ITEM_ID,
            "name": None,
            "arguments": _TOOL_ARGUMENTS,
        },
        _tool_item(output_index, status="completed", arguments=_TOOL_ARGUMENTS),
    ]
    return events


def _responses_stream(
    events: list[dict[str, Any]], *, raises: Exception | None = None
) -> AsyncIterator[str]:
    """Wrap Responses API events into SSE strings, the way the provider does."""

    async def _gen() -> AsyncIterator[str]:
        for event in events:
            yield f"data: {json.dumps(event, ensure_ascii=False)}"
        if raises is not None:
            raise raises

    return _gen()


def _mocks() -> tuple[MagicMock, MagicMock, MagicMock]:
    """A standard set of mocks: logger, FastAPI request without a disconnect, provider."""
    logger = MagicMock()
    http_request = MagicMock()
    http_request.is_disconnected = AsyncMock(return_value=False)
    client = MagicMock()
    client.name = "openai"
    return logger, http_request, client


async def _run(
    stream: AsyncIterator[str],
    logger: MagicMock,
    http_request: MagicMock,
    client: MagicMock,
    request_id: str = "req-resp",
    reasoning_cache: ReasoningCache | None = None,
) -> list[tuple[str, dict[str, Any]]]:
    """Run the converter and return the parsed Anthropic events."""
    generator = convert_responses_streaming_to_claude_with_cancellation(
        stream,
        _request(),
        logger,
        http_request,
        client,
        request_id,
        reasoning_cache=reasoning_cache if reasoning_cache is not None else ReasoningCache(),
    )
    return _parse_events([chunk async for chunk in generator])


# ---------------------------------------------------------------------------
# Streaming: successful scenarios and the reference event order
# ---------------------------------------------------------------------------


async def test_text_stream_yields_reference_event_order_with_nonempty_content() -> None:
    """A text response -> preamble, deltas, terminal tail, content is not empty."""
    logger, http_request, client = _mocks()

    events = await _run(_responses_stream(_text_stream_events()), logger, http_request, client)
    names = [name for name, _ in events]

    assert names[:3] == ["message_start", "content_block_start", "ping"]
    assert names[-3:] == ["content_block_stop", "message_delta", "message_stop"]

    assert _text_of(events) == "2+2=4."

    content_block_start = events[1][1]
    assert content_block_start["index"] == 0
    assert content_block_start["content_block"] == {"type": "text", "text": ""}

    message_delta = next(payload for name, payload in events if name == "message_delta")
    assert message_delta["delta"]["stop_reason"] == "end_turn"

    # The single closed block is the text one.
    stop_indices = [payload["index"] for name, payload in events if name == "content_block_stop"]
    assert stop_indices == [0]

    assert "error" not in names


async def test_message_id_is_generated_locally_and_not_taken_from_upstream() -> None:
    """``message_start.id`` is generated locally, not taken from ``response.id``."""
    logger, http_request, client = _mocks()

    events = await _run(_responses_stream(_text_stream_events()), logger, http_request, client)

    message_id = events[0][1]["message"]["id"]
    assert message_id.startswith("msg_")
    assert message_id != _RESPONSE_ID
    assert len(message_id) == len("msg_") + 24


async def test_tool_stream_emits_tool_block_with_single_whole_input_json_delta() -> None:
    """A tool call -> a block with index 1 and one whole ``input_json_delta``."""
    logger, http_request, client = _mocks()

    stream_events = _tool_stream_events() + [_completed(_TOOL_USAGE)]
    events = await _run(_responses_stream(stream_events), logger, http_request, client)

    tool_starts = [
        payload
        for name, payload in events
        if name == "content_block_start" and payload["content_block"]["type"] == "tool_use"
    ]
    assert len(tool_starts) == 1
    # Index 1 = text_block_index (0) + the first tool block.
    assert tool_starts[0]["index"] == 1
    # The identifier comes from call_id, not from the output item's id.
    assert tool_starts[0]["content_block"]["id"] == _TOOL_CALL_ID
    assert tool_starts[0]["content_block"]["name"] == _TOOL_NAME
    assert tool_starts[0]["content_block"]["input"] == {}

    json_deltas = [
        payload
        for name, payload in events
        if name == "content_block_delta" and payload["delta"]["type"] == "input_json_delta"
    ]
    assert len(json_deltas) == 1
    assert json_deltas[0]["index"] == 1
    assert json_deltas[0]["delta"]["partial_json"] == _TOOL_ARGUMENTS
    assert json.loads(json_deltas[0]["delta"]["partial_json"]) == {"answer": 391}

    message_delta = next(payload for name, payload in events if name == "message_delta")
    assert message_delta["delta"]["stop_reason"] == "tool_use"


async def test_mixed_stream_keeps_text_block_open_until_shared_terminal_tail() -> None:
    """Text + tool -> blocks overlap, closed by a single shared tail at the end."""
    logger, http_request, client = _mocks()

    stream_events = (
        _text_stream_events()[:-1] + _tool_stream_events(output_index=1) + [_completed(_TOOL_USAGE)]
    )
    events = await _run(_responses_stream(stream_events), logger, http_request, client)
    names = [name for name, _ in events]

    tool_start_position = names.index("content_block_start", 2)
    first_stop_position = names.index("content_block_stop")

    # The text block opens first and is NOT closed until the tool block opens.
    assert tool_start_position < first_stop_position
    assert _text_of(events) == "2+2=4."

    # Tail: first the text block, then the tool blocks, then the terminators.
    assert names[-4:] == [
        "content_block_stop",
        "content_block_stop",
        "message_delta",
        "message_stop",
    ]
    stop_indices = [payload["index"] for name, payload in events if name == "content_block_stop"]
    assert stop_indices == [0, 1]

    message_delta = next(payload for name, payload in events if name == "message_delta")
    assert message_delta["delta"]["stop_reason"] == "tool_use"


async def test_tool_arguments_done_emits_json_when_deltas_never_form_valid_json() -> None:
    """Argument deltas that never form valid JSON -> JSON is taken from ``arguments.done``."""
    logger, http_request, client = _mocks()

    stream_events = [
        _tool_item(0, status="in_progress", arguments=""),
        {
            "type": "response.function_call_arguments.delta",
            "output_index": 0,
            "item_id": _TOOL_ITEM_ID,
            "delta": '{"answer":',
        },
        {
            "type": "response.function_call_arguments.done",
            "output_index": 0,
            "item_id": _TOOL_ITEM_ID,
            "name": None,
            "arguments": _TOOL_ARGUMENTS,
        },
        _completed(_TOOL_USAGE),
    ]
    events = await _run(_responses_stream(stream_events), logger, http_request, client)

    json_deltas = [
        payload
        for name, payload in events
        if name == "content_block_delta" and payload["delta"]["type"] == "input_json_delta"
    ]
    assert len(json_deltas) == 1
    assert json_deltas[0]["delta"]["partial_json"] == _TOOL_ARGUMENTS


# ---------------------------------------------------------------------------
# Protection against a silent failure: Responses events must not be lost silently
# ---------------------------------------------------------------------------


async def test_responses_events_in_chat_completions_parser_lose_content_silently() -> None:
    """Trap guard: the old parser fed Responses events yields empty content."""
    logger, http_request, client = _mocks()

    generator = convert_openai_streaming_to_claude_with_cancellation(
        _responses_stream(_text_stream_events()),
        _request(),
        logger,
        http_request,
        client,
        "req-trap",
    )
    events = _parse_events([chunk async for chunk in generator])
    names = [name for name, _ in events]

    # The failure looks like success: a valid tail, zero errors, empty content.
    assert names[-3:] == ["content_block_stop", "message_delta", "message_stop"]
    assert "error" not in names
    assert _text_of(events) == ""

    # The new converter does NOT lose content on the same data.
    logger, http_request, client = _mocks()
    new_events = await _run(_responses_stream(_text_stream_events()), logger, http_request, client)
    assert _text_of(new_events) == "2+2=4."


async def test_unknown_event_type_is_logged_and_does_not_break_stream() -> None:
    """An unfamiliar event type -> a warning is logged, content and tail are unaffected."""
    logger, http_request, client = _mocks()

    stream_events = _text_stream_events()
    stream_events.insert(5, {"type": "response.some_future_event", "output_index": 0})
    stream_events.insert(6, {"type": "response.some_future_event", "output_index": 0})

    events = await _run(_responses_stream(stream_events), logger, http_request, client)
    names = [name for name, _ in events]

    assert _text_of(events) == "2+2=4."
    assert names[-3:] == ["content_block_stop", "message_delta", "message_stop"]
    assert "error" not in names

    warn_calls = [
        call
        for call in logger.warning.call_args_list
        if call.kwargs.get("event_type") == "response.some_future_event"
    ]
    # Logged exactly once per type, so as not to spam the log.
    assert len(warn_calls) == 1


# ---------------------------------------------------------------------------
# Streaming: usage and stop_reason
# ---------------------------------------------------------------------------


async def test_usage_from_completed_event_is_reported_in_message_delta() -> None:
    """usage from the terminal event -> input/output/cache_read in ``message_delta``."""
    logger, http_request, client = _mocks()

    usage = {
        "input_tokens": 61,
        "input_tokens_details": {"cache_write_tokens": 0, "cached_tokens": 1024},
        "output_tokens": 18,
        "output_tokens_details": {"reasoning_tokens": 12},
        "total_tokens": 79,
    }
    stream_events = _text_stream_events()[:-1] + [_completed(usage)]

    events = await _run(_responses_stream(stream_events), logger, http_request, client)
    message_delta = next(payload for name, payload in events if name == "message_delta")

    assert message_delta["usage"] == {
        "input_tokens": 61,
        "output_tokens": 18,
        "cache_read_input_tokens": 1024,
    }


async def test_incomplete_terminal_event_maps_stop_reason_to_max_tokens() -> None:
    """``response.incomplete`` with max_output_tokens -> ``stop_reason`` max_tokens."""
    logger, http_request, client = _mocks()

    incomplete = {
        "type": "response.incomplete",
        "response": {
            "id": _RESPONSE_ID,
            "status": "incomplete",
            "incomplete_details": {"reason": "max_output_tokens"},
            "usage": _TEXT_USAGE,
        },
    }
    stream_events = _text_stream_events()[:-1] + [incomplete]

    events = await _run(_responses_stream(stream_events), logger, http_request, client)
    names = [name for name, _ in events]

    message_delta = next(payload for name, payload in events if name == "message_delta")
    assert message_delta["delta"]["stop_reason"] == "max_tokens"
    assert names[-1] == "message_stop"


async def test_failed_terminal_event_emits_error_event_without_message_stop() -> None:
    """``response.failed`` -> ``event: error``, the terminal tail is not sent."""
    logger, http_request, client = _mocks()

    failed = {
        "type": "response.failed",
        "response": {
            "id": _RESPONSE_ID,
            "status": "failed",
            "error": {"code": "server_error", "message": "upstream exploded"},
            "usage": _TEXT_USAGE,
        },
    }
    stream_events = _text_stream_events()[:-1] + [failed]

    events = await _run(_responses_stream(stream_events), logger, http_request, client)
    names = [name for name, _ in events]

    assert "error" in names
    error_event = next(payload for name, payload in events if name == "error")
    assert error_event["error"]["type"] == "api_error"
    assert "upstream exploded" in error_event["error"]["message"]

    assert "message_stop" not in names
    assert _text_of(events) == "2+2=4."
    assert logger.warning.called


# ---------------------------------------------------------------------------
# Streaming: disconnects, cancellation, and upstream errors
# ---------------------------------------------------------------------------


async def test_client_disconnect_cancels_upstream_and_emits_terminal_tail() -> None:
    """A client disconnect -> cancel_request and a terminal tail with partial content."""
    logger, http_request, client = _mocks()

    checks = {"count": 0}

    async def _is_disconnected() -> bool:
        checks["count"] += 1
        # Disconnect after the preamble and the first two text deltas.
        return checks["count"] > 6

    http_request.is_disconnected = _is_disconnected

    events = await _run(
        _responses_stream(_text_stream_events()), logger, http_request, client, "req-disconnect"
    )
    names = [name for name, _ in events]

    client.cancel_request.assert_called_once_with("req-disconnect")

    # Partial content arrived, the stream is closed with a valid tail and no errors.
    assert _text_of(events) == "2+"
    assert names[-3:] == ["content_block_stop", "message_delta", "message_stop"]
    assert "error" not in names

    message_delta = next(payload for name, payload in events if name == "message_delta")
    assert message_delta["delta"]["stop_reason"] == "end_turn"


async def test_client_cancellation_499_emits_cancelled_error_without_message_stop() -> None:
    """ProviderError(499) -> an ``event: error`` of type cancelled, without ``message_stop``."""
    logger, http_request, client = _mocks()

    stream = _responses_stream(
        _text_stream_events()[:-1],
        raises=ProviderError(message="Request cancelled by client", status_code=499),
    )
    events = await _run(stream, logger, http_request, client, "req-499")
    names = [name for name, _ in events]

    assert "error" in names
    error_event = next(payload for name, payload in events if name == "error")
    assert error_event["error"]["type"] == "cancelled"

    assert "message_stop" not in names
    logger.warning.assert_not_called()


async def test_http_exception_midstream_emits_error_event_without_terminal_tail() -> None:
    """ProviderError(503) after start -> overloaded_error, without a terminal tail."""
    logger, http_request, client = _mocks()

    stream = _responses_stream(
        _text_stream_events()[:-1],
        raises=ProviderError(message="Upstream unavailable", status_code=503),
    )
    events = await _run(stream, logger, http_request, client, "req-503")
    names = [name for name, _ in events]

    assert names[:3] == ["message_start", "content_block_start", "ping"]
    assert _text_of(events) == "2+2=4."

    error_event = next(payload for name, payload in events if name == "error")
    assert error_event["error"]["type"] == "overloaded_error"
    assert "Upstream unavailable" in error_event["error"]["message"]

    assert "message_stop" not in names
    assert logger.warning.called


async def test_httpx_error_midstream_closes_gracefully_with_partial_content() -> None:
    """A transport abort -> a graceful tail with partial content, no error event."""
    logger, http_request, client = _mocks()

    stream = _responses_stream(
        _text_stream_events()[:-1],
        raises=httpx.RemoteProtocolError(
            "peer closed connection without sending complete message body "
            "(incomplete chunked read)"
        ),
    )
    events = await _run(stream, logger, http_request, client, "req-abort")
    names = [name for name, _ in events]

    assert _text_of(events) == "2+2=4."
    assert names[-3:] == ["content_block_stop", "message_delta", "message_stop"]
    assert "error" not in names

    message_delta = next(payload for name, payload in events if name == "message_delta")
    assert message_delta["delta"]["stop_reason"] == "end_turn"

    warn_kwargs = logger.warning.call_args.kwargs
    assert warn_kwargs["error_type"] == "RemoteProtocolError"
    assert warn_kwargs["request_id"] == "req-abort"

    client.cancel_request.assert_not_called()


async def test_tool_block_is_closed_in_terminal_tail_after_transport_abort() -> None:
    """A transport abort mid-tool-block -> the block is still closed in the tail."""
    logger, http_request, client = _mocks()

    stream = _responses_stream(
        _tool_stream_events(),
        raises=httpx.RemoteProtocolError("incomplete chunked read"),
    )
    events = await _run(stream, logger, http_request, client, "req-tool-abort")
    names = [name for name, _ in events]

    stop_indices = [payload["index"] for name, payload in events if name == "content_block_stop"]
    assert stop_indices == [0, 1]
    assert names[-3:] == ["content_block_stop", "message_delta", "message_stop"]


# ---------------------------------------------------------------------------
# Non-streaming
# ---------------------------------------------------------------------------


def test_convert_responses_nonstreaming_message_returns_text_block() -> None:
    """A ``message`` item -> an Anthropic text block and usage from the Responses fields."""
    responses_response = {
        "id": _RESPONSE_ID,
        "status": "completed",
        "output": [
            {
                "id": _TEXT_ITEM_ID,
                "type": "message",
                "role": "assistant",
                "status": "completed",
                "content": [{"type": "output_text", "text": "2+2=4.", "annotations": []}],
            }
        ],
        "usage": _TEXT_USAGE,
    }

    claude_response = convert_responses_to_claude_response(
        responses_response, _request(), reasoning_cache=ReasoningCache()
    )

    assert claude_response["content"] == [{"type": "text", "text": "2+2=4."}]
    assert claude_response["stop_reason"] == "end_turn"
    assert claude_response["role"] == "assistant"
    assert claude_response["model"] == "gpt-5.6-sol"
    assert claude_response["usage"] == {
        "input_tokens": 23,
        "output_tokens": 10,
        "cache_read_input_tokens": 0,
    }


def test_convert_responses_nonstreaming_function_call_maps_call_id_to_tool_use() -> None:
    """A ``function_call`` item -> tool_use with an identifier from ``call_id``."""
    responses_response = {
        "id": _RESPONSE_ID,
        "status": "completed",
        "output": [
            {
                "id": _TOOL_ITEM_ID,
                "type": "function_call",
                "status": "completed",
                "call_id": _TOOL_CALL_ID,
                "name": _TOOL_NAME,
                "arguments": _TOOL_ARGUMENTS,
            }
        ],
        "usage": _TOOL_USAGE,
    }

    claude_response = convert_responses_to_claude_response(
        responses_response, _request(), reasoning_cache=ReasoningCache()
    )

    assert claude_response["content"] == [
        {
            "type": "tool_use",
            "id": _TOOL_CALL_ID,
            "name": _TOOL_NAME,
            "input": {"answer": 391},
        }
    ]
    assert claude_response["stop_reason"] == "tool_use"


def test_convert_responses_nonstreaming_reasoning_item_is_skipped_without_error() -> None:
    """A ``reasoning`` item does not end up in content and does not break parsing."""
    responses_response = {
        "id": _RESPONSE_ID,
        "status": "completed",
        "output": [
            {
                "id": "rs_0226e3ff2e40c123006a63d584cd1c81929878ad4d01848b01",
                "type": "reasoning",
                "summary": [{"type": "summary_text", "text": "secret chain of thought"}],
                "content": [],
                "encrypted_content": "gAAAAAB...",
            },
            {
                "id": _TEXT_ITEM_ID,
                "type": "message",
                "role": "assistant",
                "status": "completed",
                "content": [{"type": "output_text", "text": "6016", "annotations": []}],
            },
        ],
        "usage": _TEXT_USAGE,
    }

    claude_response = convert_responses_to_claude_response(
        responses_response, _request(), reasoning_cache=ReasoningCache()
    )

    assert claude_response["content"] == [{"type": "text", "text": "6016"}]


def test_convert_responses_nonstreaming_reasoning_only_output_returns_empty_text() -> None:
    """A single ``reasoning`` output -> an empty text block instead of empty content."""
    responses_response = {
        "id": _RESPONSE_ID,
        "status": "incomplete",
        "incomplete_details": {"reason": "max_output_tokens"},
        "output": [
            {
                "id": "rs_0d1acf0114225a21006a63d58c2ee081a2bd7825a7658ac3d4",
                "type": "reasoning",
                "summary": [],
                "content": [],
                "encrypted_content": "gAAAAAB...",
            }
        ],
        "usage": {
            "input_tokens": 22,
            "input_tokens_details": {"cache_write_tokens": 0, "cached_tokens": 0},
            "output_tokens": 24,
            "output_tokens_details": {"reasoning_tokens": 24},
            "total_tokens": 46,
        },
    }

    claude_response = convert_responses_to_claude_response(
        responses_response, _request(), reasoning_cache=ReasoningCache()
    )

    assert claude_response["content"] == [{"type": "text", "text": ""}]
    assert claude_response["stop_reason"] == "max_tokens"


def test_convert_responses_nonstreaming_malformed_arguments_fall_back_to_raw() -> None:
    """Invalid argument JSON -> preserved in ``raw_arguments``, without raising."""
    responses_response = {
        "id": _RESPONSE_ID,
        "status": "completed",
        "output": [
            {
                "id": _TOOL_ITEM_ID,
                "type": "function_call",
                "status": "completed",
                "call_id": _TOOL_CALL_ID,
                "name": _TOOL_NAME,
                "arguments": '{"answer":',
            }
        ],
        "usage": _TOOL_USAGE,
    }

    claude_response = convert_responses_to_claude_response(
        responses_response, _request(), reasoning_cache=ReasoningCache()
    )

    assert claude_response["content"][0]["input"] == {"raw_arguments": '{"answer":'}


def test_convert_responses_nonstreaming_empty_output_raises_500() -> None:
    """A missing ``output`` -> ProviderError(500), not a silent empty response."""
    with pytest.raises(ProviderError) as exc_info:
        convert_responses_to_claude_response(
            {"id": _RESPONSE_ID, "output": []}, _request(), reasoning_cache=ReasoningCache()
        )

    assert exc_info.value.status_code == 500


# ---------------------------------------------------------------------------
# Reasoning cache: extracting items from the upstream response
# ---------------------------------------------------------------------------


def test_extract_reasoning_binds_items_to_following_function_call() -> None:
    """``reasoning`` items are attributed to the ``call_id`` of the following call."""
    output = [_reasoning_item(), _function_call_item()]

    by_call_id = extract_reasoning_by_call_id(output)

    assert list(by_call_id) == [_TOOL_CALL_ID]
    assert by_call_id[_TOOL_CALL_ID] == [_reasoning_item()]


def test_extract_reasoning_without_function_call_yields_nothing() -> None:
    """Reasoning without a following tool call is not stored: there is no anchor."""
    output = [
        _reasoning_item(),
        {
            "id": _TEXT_ITEM_ID,
            "type": "message",
            "role": "assistant",
            "status": "completed",
            "content": [{"type": "output_text", "text": "6016"}],
        },
    ]

    assert extract_reasoning_by_call_id(output) == {}


def test_extract_reasoning_assigns_items_only_to_first_of_parallel_calls() -> None:
    """With several calls, items are assigned to the first one, without duplication."""
    output = [
        _reasoning_item(),
        _function_call_item("call_first"),
        _function_call_item("call_second"),
    ]

    by_call_id = extract_reasoning_by_call_id(output)

    assert list(by_call_id) == ["call_first"]
    assert "call_second" not in by_call_id


def test_extract_reasoning_keeps_separate_groups_per_call() -> None:
    """Each reasoning group is assigned to its own tool call."""
    output = [
        _reasoning_item("rs_first"),
        _function_call_item("call_first"),
        _reasoning_item("rs_second"),
        _function_call_item("call_second"),
    ]

    by_call_id = extract_reasoning_by_call_id(output)

    assert [item["id"] for item in by_call_id["call_first"]] == ["rs_first"]
    assert [item["id"] for item in by_call_id["call_second"]] == ["rs_second"]


def test_nonstreaming_response_stores_reasoning_in_cache_by_call_id() -> None:
    """A non-streaming response stores reasoning in the cache under the call's ``call_id``."""
    cache = ReasoningCache()
    responses_response = {
        "id": _RESPONSE_ID,
        "status": "completed",
        "output": [_reasoning_item(), _function_call_item()],
        "usage": _TOOL_USAGE,
    }

    claude_response = convert_responses_to_claude_response(
        responses_response, _request(), reasoning_cache=cache
    )

    assert claude_response["content"][0]["type"] == "tool_use"
    stored = cache.get(_TOOL_CALL_ID)
    assert [item["id"] for item in stored] == [_REASONING_ITEM_ID]
    assert stored[0]["encrypted_content"] == _ENCRYPTED_CONTENT


async def test_streaming_terminal_event_stores_reasoning_in_cache_by_call_id() -> None:
    """The stream's final object stores reasoning in the cache under the call's ``call_id``."""
    logger, http_request, client = _mocks()
    cache = ReasoningCache()

    stream_events = _tool_stream_events() + [
        _completed(_TOOL_USAGE, [_reasoning_item(), _function_call_item()])
    ]
    events = await _run(
        _responses_stream(stream_events),
        logger,
        http_request,
        client,
        reasoning_cache=cache,
    )

    message_delta = next(payload for name, payload in events if name == "message_delta")
    assert message_delta["delta"]["stop_reason"] == "tool_use"

    stored = cache.get(_TOOL_CALL_ID)
    assert [item["id"] for item in stored] == [_REASONING_ITEM_ID]
    assert stored[0]["encrypted_content"] == _ENCRYPTED_CONTENT


async def test_streaming_without_reasoning_in_output_leaves_cache_empty() -> None:
    """A response without reasoning items does not populate the cache."""
    logger, http_request, client = _mocks()
    cache = ReasoningCache()

    stream_events = _tool_stream_events() + [
        _completed(_TOOL_USAGE, [_function_call_item()])
    ]
    await _run(
        _responses_stream(stream_events),
        logger,
        http_request,
        client,
        reasoning_cache=cache,
    )

    assert cache.get(_TOOL_CALL_ID) == []


async def test_streaming_aborted_before_terminal_event_leaves_cache_empty() -> None:
    """An abort before the terminal event -> the cache is empty, the stream closes normally."""
    logger, http_request, client = _mocks()
    cache = ReasoningCache()

    stream = _responses_stream(
        _tool_stream_events(),
        raises=httpx.RemoteProtocolError("incomplete chunked read"),
    )
    events = await _run(
        stream, logger, http_request, client, "req-abort-cache", reasoning_cache=cache
    )
    names = [name for name, _ in events]

    assert names[-3:] == ["content_block_stop", "message_delta", "message_stop"]
    assert cache.get(_TOOL_CALL_ID) == []
