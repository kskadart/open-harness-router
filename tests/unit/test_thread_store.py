"""Unit tests for the message-thread store: what a continuation is rebuilt
into, how a response stream is recorded, and how the store forgets.

The request shapes are the ones Claude Code 2.1.278 sends through the
forward-proxy (captured 2026-09-21): a ``create`` with the whole
conversation, then ``continue`` deltas carrying the attribution block as
the entire ``system`` and the new tool results only.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import pytest

from services import thread_store as thread_store_module
from services.thread_store import ThreadMissError, ThreadStore

_ATTRIBUTION_CREATE = "x-anthropic-billing-header: cc_version=2.1.278.516; cch=8faa5;"
_ATTRIBUTION_CONTINUE = "x-anthropic-billing-header: cc_version=2.1.278.516; cch=e40a9;"
_TOOLS = [{"name": "Bash", "input_schema": {"type": "object"}}]
_ASSISTANT = [
    {"type": "text", "text": ""},
    {"type": "tool_use", "id": "call_1", "name": "Bash", "input": {"command": "ls"}},
]
_TOOL_RESULT = {
    "role": "user",
    "content": [{"type": "tool_result", "tool_use_id": "call_1", "content": "total 736"}],
}
_REMINDER = {
    "role": "system",
    "content": [{"type": "text", "text": "<total_tokens>1 tokens left</total_tokens>"}],
}


def _create(**overrides: Any) -> dict[str, Any]:
    """The first request of a turn, as the client sends it."""
    request: dict[str, Any] = {
        "model": "ag-GLM-5.3-Flash",
        "max_tokens": 32000,
        "stream": True,
        "temperature": 1.0,
        "thread": {"type": "create"},
        "system": [
            {"type": "text", "text": _ATTRIBUTION_CREATE},
            {"type": "text", "text": "Be terse.", "cache_control": {"type": "ephemeral"}},
        ],
        "messages": [{"role": "user", "content": "List the files."}],
        "tools": _TOOLS,
    }
    request.update(overrides)
    return request


def _continue(**overrides: Any) -> dict[str, Any]:
    """The delta the client sends after a tool call."""
    delta: dict[str, Any] = {
        "model": "ag-GLM-5.3-Flash",
        "max_tokens": 16000,
        "stream": True,
        "thread": {"type": "continue", "previous_message_id": "msg_1"},
        "system": [{"type": "text", "text": _ATTRIBUTION_CONTINUE}],
        "messages": [_TOOL_RESULT, _REMINDER],
    }
    delta.update(overrides)
    return delta


def test_resume_rebuilds_the_request_the_client_would_have_sent_in_full() -> None:
    store = ThreadStore("gateway")
    store.record("msg_1", _create(), _ASSISTANT)

    full = store.resume("msg_1", _continue())

    assert full["system"] == [
        {"type": "text", "text": _ATTRIBUTION_CONTINUE},
        {"type": "text", "text": "Be terse.", "cache_control": {"type": "ephemeral"}},
    ]
    assert full["messages"] == [
        {"role": "user", "content": "List the files."},
        {"role": "assistant", "content": _ASSISTANT},
        _TOOL_RESULT,
        _REMINDER,
    ]
    assert full["tools"] == _TOOLS
    assert full["max_tokens"] == 16000  # the delta's own fields win
    assert full["temperature"] == 1.0  # the held ones fill in the rest
    assert "thread" not in full


def test_resume_prefers_tools_the_delta_restates() -> None:
    store = ThreadStore("gateway")
    store.record("msg_1", _create(), _ASSISTANT)
    new_tools = [{"name": "Read", "input_schema": {"type": "object"}}]

    assert store.resume("msg_1", _continue(tools=new_tools))["tools"] == new_tools


@pytest.mark.parametrize(
    "system", ["Be terse.", [{"type": "text", "text": "Be terse."}], None]
)
def test_resume_keeps_a_system_prompt_without_attribution_as_is(system: object) -> None:
    store = ThreadStore("gateway")
    store.record("msg_1", _create(system=system), _ASSISTANT)

    assert store.resume("msg_1", _continue()).get("system") == system


def test_resume_does_not_change_what_is_held() -> None:
    """A client retry of the same continuation gets the same rebuilt request."""
    store = ThreadStore("gateway")
    store.record("msg_1", _create(), _ASSISTANT)

    first = store.resume("msg_1", _continue())
    first["messages"].append({"role": "user", "content": "tampered"})
    first["system"].append({"type": "text", "text": "tampered"})
    second = store.resume("msg_1", _continue())

    assert len(second["messages"]) == 4
    assert len(second["system"]) == 2


def test_record_stands_in_an_empty_text_block_for_no_content() -> None:
    store = ThreadStore("gateway")
    store.record("msg_1", _create(), [])

    assert store.resume("msg_1", _continue())["messages"][1] == {
        "role": "assistant",
        "content": [{"type": "text", "text": ""}],
    }


@pytest.mark.parametrize("previous_message_id", [None, "msg_never_seen"])
def test_unknown_id_raises_a_miss(previous_message_id: str | None) -> None:
    store = ThreadStore("gateway")
    store.record("msg_1", _create(), _ASSISTANT)

    with pytest.raises(ThreadMissError) as excinfo:
        store.resume(previous_message_id, _continue())
    assert excinfo.value.reason == "unknown_id"


def test_expired_entry_raises_a_miss_and_is_dropped(monkeypatch: pytest.MonkeyPatch) -> None:
    now = 1000.0
    monkeypatch.setattr(thread_store_module.time, "monotonic", lambda: now)
    store = ThreadStore("gateway", ttl_s=60.0)
    store.record("msg_1", _create(), _ASSISTANT)

    now = 1061.0
    with pytest.raises(ThreadMissError) as expired:
        store.resume("msg_1", _continue())
    assert expired.value.reason == "expired"
    with pytest.raises(ThreadMissError) as gone:
        store.resume("msg_1", _continue())
    assert gone.value.reason == "unknown_id"


def test_store_evicts_the_oldest_beyond_the_cap() -> None:
    store = ThreadStore("gateway", max_entries=2)
    for index in range(3):
        store.record(f"msg_{index}", _create(), _ASSISTANT)

    with pytest.raises(ThreadMissError):
        store.resume("msg_0", _continue())
    assert store.resume("msg_2", _continue())["messages"][1]["content"] == _ASSISTANT


def _frame(event: str, payload: dict[str, Any]) -> str:
    """One SSE frame the way the response converters emit it."""
    return f"event: {event}\ndata: {json.dumps(payload)}\n\n"


def _converter_stream() -> list[str]:
    """The frames the chat converter emits for a text-plus-tool-call response."""
    return [
        _frame(
            "message_start",
            {
                "type": "message_start",
                "message": {
                    "id": "msg_9",
                    "type": "message",
                    "role": "assistant",
                    "content": [],
                    "usage": {"input_tokens": 0, "output_tokens": 0},
                },
            },
        ),
        _frame(
            "content_block_start",
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "text", "text": ""},
            },
        ),
        _frame("ping", {"type": "ping"}),
        _frame(
            "content_block_delta",
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": "Sure"},
            },
        ),
        _frame(
            "content_block_delta",
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": "."},
            },
        ),
        _frame(
            "content_block_start",
            {
                "type": "content_block_start",
                "index": 1,
                "content_block": {"type": "tool_use", "id": "call_7", "name": "Bash", "input": {}},
            },
        ),
        _frame(
            "content_block_delta",
            {
                "type": "content_block_delta",
                "index": 1,
                "delta": {"type": "input_json_delta", "partial_json": '{"command": "ls"}'},
            },
        ),
        _frame("content_block_stop", {"type": "content_block_stop", "index": 0}),
        _frame("content_block_stop", {"type": "content_block_stop", "index": 1}),
        _frame(
            "message_delta",
            {
                "type": "message_delta",
                "delta": {"stop_reason": "tool_use", "stop_sequence": None},
                "usage": {"output_tokens": 5},
            },
        ),
        _frame("message_stop", {"type": "message_stop"}),
    ]


async def _iterate(frames: list[str]) -> AsyncIterator[str]:
    for frame in frames:
        yield frame


async def test_record_stream_relays_the_frames_and_records_the_assembled_message() -> None:
    store = ThreadStore("gateway")
    frames = _converter_stream()

    relayed = [frame async for frame in store.record_stream(_create(), _iterate(frames))]

    assert relayed == frames
    full = store.resume("msg_9", _continue())
    assert full["messages"][1] == {
        "role": "assistant",
        "content": [
            {"type": "text", "text": "Sure."},
            {"type": "tool_use", "id": "call_7", "name": "Bash", "input": {"command": "ls"}},
        ],
    }


async def test_record_stream_passes_over_frames_it_cannot_read() -> None:
    """Malformed frames are relayed as they are and never break the stream."""
    store = ThreadStore("gateway")
    frames = _converter_stream()
    noise = [
        "event: ping\n\n",  # no data line
        "event: content_block_delta\ndata: not json\n\n",
        _frame("content_block_start", {"type": "content_block_start", "index": "one"}),
        _frame("content_block_delta", {"type": "content_block_delta", "index": True}),
        _frame("content_block_delta", {"type": "content_block_delta", "index": 7, "delta": {}}),
        "data: [1, 2, 3]\n\n",
    ]
    frames[3:3] = noise

    relayed = [frame async for frame in store.record_stream(_create(), _iterate(frames))]

    assert relayed == frames
    assistant = store.resume("msg_9", _continue())["messages"][1]["content"]
    assert assistant[0] == {"type": "text", "text": "Sure."}


async def test_record_stream_records_nothing_for_a_stream_that_does_not_complete() -> None:
    """An error frame instead of message_stop: the client will not continue from it."""
    store = ThreadStore("gateway")
    frames = [
        *_converter_stream()[:-1],
        _frame("error", {"type": "error", "error": {"type": "api_error", "message": "gone"}}),
    ]

    relayed = [frame async for frame in store.record_stream(_create(), _iterate(frames))]

    assert relayed == frames
    with pytest.raises(ThreadMissError):
        store.resume("msg_9", _continue())


async def test_record_stream_records_nothing_when_the_stream_breaks() -> None:
    store = ThreadStore("gateway")
    closed = False

    async def breaking() -> AsyncIterator[str]:
        nonlocal closed
        try:
            for frame in _converter_stream()[:4]:
                yield frame
            raise ConnectionResetError("upstream reset")
        finally:
            closed = True

    with pytest.raises(ConnectionResetError):
        async for _frame in store.record_stream(_create(), breaking()):
            pass

    assert closed is True
    with pytest.raises(ThreadMissError):
        store.resume("msg_9", _continue())
