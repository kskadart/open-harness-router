"""Integration tests for the OpenAI Responses API path (``POST /v1/responses``).

The ``openai`` provider in the test routing fixture is declared with
``api_flavor: responses`` and ``reasoning_effort: high``, so the
``gpt-5.6-sol`` model goes to /v1/responses. The body and event shapes mirror
data actually captured from the live API (the same fixtures as in
``tests/unit/test_request_converter_responses.py`` and
``tests/unit/test_response_converter_responses.py``).

Additionally pins down the chat-path regression: openai_compatible (GLM) and
the openai provider with ``api_flavor: chat`` still call
/v1/chat/completions.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from pytest_httpx import HTTPXMock

_RESPONSES_URL = "https://api.openai.com/v1/responses"
_OPENAI_CHAT_URL = "https://api.openai.com/v1/chat/completions"
_OPENAI_COMPATIBLE_CHAT_URL = "https://gateway.example.com/v1/chat/completions"

# The gpt- prefix routes to the provider with api_flavor=responses,
# the chat- prefix -- to the provider with api_flavor=chat.
_RESPONSES_MODEL = "gpt-5.6-sol"
_CHAT_MODEL = "chat-gpt-5.6-sol"
_GLM_MODEL = "zai-org/GLM-5.2-FP8"

_CLIENT_HEADERS = {"x-api-key": "irrelevant-openai-uses-server-key"}
_MAX_TOKENS = 256

# The openai provider's reasoning level in the routing fixture, and the
# level the user sends in output_config.
_PROVIDER_FALLBACK_EFFORT = "high"
_REQUEST_EFFORT = "xhigh"

# Chat-path parameters not allowed in a Responses API request body.
_CHAT_ONLY_PARAMS = frozenset(
    {"max_tokens", "max_completion_tokens", "temperature", "top_p", "stream_options"}
)

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
_ANSWER_TEXT = "".join(_TEXT_DELTAS)
_TOOL_SCHEMA = {"type": "object", "properties": {"answer": {"type": "integer"}}}

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

_GLM_CHAT_RESPONSE = {
    "id": "chatcmpl-glm-regression-01",
    "object": "chat.completion",
    "created": 1_720_000_000,
    "model": _GLM_MODEL,
    "choices": [
        {
            "index": 0,
            "message": {"role": "assistant", "content": "Hello from GLM mock"},
            "finish_reason": "stop",
        }
    ],
    "usage": {"prompt_tokens": 7, "completion_tokens": 4, "total_tokens": 11},
}

_OPENAI_CHAT_RESPONSE = {
    "id": "chatcmpl-openai-chat-regression-01",
    "object": "chat.completion",
    "created": 1_720_000_000,
    "model": _CHAT_MODEL,
    "choices": [
        {
            "index": 0,
            "message": {"role": "assistant", "content": "Hello from chat mock"},
            "finish_reason": "stop",
        }
    ],
    "usage": {"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8},
}


def _anthropic_usage(usage: dict[str, Any]) -> dict[str, int]:
    """Expected Anthropic usage for a Responses API usage object."""
    return {
        "input_tokens": usage["input_tokens"],
        "output_tokens": usage["output_tokens"],
        "cache_read_input_tokens": usage["input_tokens_details"]["cached_tokens"],
    }


def _claude_payload(**overrides: Any) -> dict[str, Any]:
    """Minimal Anthropic request to a reasoning model via /v1/responses."""
    payload: dict[str, Any] = {
        "model": _RESPONSES_MODEL,
        "max_tokens": _MAX_TOKENS,
        "messages": [{"role": "user", "content": "2+2?"}],
    }
    payload.update(overrides)
    return payload


def _responses_body(
    output: list[dict[str, Any]], usage: dict[str, Any]
) -> dict[str, Any]:
    """Body of a non-streaming /v1/responses response with the given output and usage."""
    return {
        "id": _RESPONSE_ID,
        "object": "response",
        "created_at": 1_720_000_000,
        "model": _RESPONSES_MODEL,
        "status": "completed",
        "output": output,
        "usage": usage,
    }


def _text_output() -> list[dict[str, Any]]:
    """Output consisting of one ``message`` item with a text response."""
    return [
        {
            "id": _TEXT_ITEM_ID,
            "type": "message",
            "role": "assistant",
            "status": "completed",
            "content": [{"type": "output_text", "text": _ANSWER_TEXT, "annotations": []}],
        }
    ]


def _tool_output() -> list[dict[str, Any]]:
    """Output consisting of one ``function_call`` item with a tool call."""
    return [
        {
            "id": _TOOL_ITEM_ID,
            "type": "function_call",
            "status": "completed",
            "call_id": _TOOL_CALL_ID,
            "name": _TOOL_NAME,
            "arguments": _TOOL_ARGUMENTS,
        }
    ]


def _completed_event(usage: dict[str, Any]) -> dict[str, Any]:
    """Terminal event for a successful Responses API response."""
    return {
        "type": "response.completed",
        "response": {"id": _RESPONSE_ID, "status": "completed", "usage": usage},
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
            "text": _ANSWER_TEXT,
        },
        {
            "type": "response.output_item.done",
            "output_index": 0,
            "item": {
                "id": _TEXT_ITEM_ID,
                "type": "message",
                "role": "assistant",
                "status": "completed",
                "content": [{"type": "output_text", "text": _ANSWER_TEXT}],
            },
        },
        _completed_event(_TEXT_USAGE),
    ]
    return events


def _tool_stream_events() -> list[dict[str, Any]]:
    """Real event sequence for a response with a tool call."""
    events: list[dict[str, Any]] = [
        {
            "type": "response.output_item.added",
            "output_index": 0,
            "item": {
                "id": _TOOL_ITEM_ID,
                "type": "function_call",
                "status": "in_progress",
                "arguments": "",
                "call_id": _TOOL_CALL_ID,
                "name": _TOOL_NAME,
            },
        }
    ]
    events += [
        {
            "type": "response.function_call_arguments.delta",
            "output_index": 0,
            "item_id": _TOOL_ITEM_ID,
            "delta": delta,
        }
        for delta in _TOOL_ARGUMENT_DELTAS
    ]
    events.append(_completed_event(_TOOL_USAGE))
    return events


def _sse_body(events: list[dict[str, Any]]) -> bytes:
    """Build a Responses API SSE body: one ``event``/``data`` pair per event."""
    return "".join(
        f"event: {event['type']}\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"
        for event in events
    ).encode()


def _mock_responses(httpx_mock: HTTPXMock, body: dict[str, Any]) -> None:
    """Mock a non-streaming upstream response on /v1/responses."""
    httpx_mock.add_response(
        url=_RESPONSES_URL,
        method="POST",
        json=body,
        status_code=200,
        headers={"content-type": "application/json"},
    )


def _mock_responses_stream(httpx_mock: HTTPXMock, events: list[dict[str, Any]]) -> None:
    """Mock an upstream SSE stream on /v1/responses."""
    httpx_mock.add_response(
        url=_RESPONSES_URL,
        method="POST",
        status_code=200,
        headers={"content-type": "text/event-stream"},
        content=_sse_body(events),
    )


def _single_upstream_body(httpx_mock: HTTPXMock, url: str) -> dict[str, Any]:
    """Body of the single outbound POST request to the given URL."""
    outbound = httpx_mock.get_requests(url=url, method="POST")
    assert len(outbound) == 1
    body: dict[str, Any] = json.loads(outbound[0].content)
    return body


def _parse_anthropic_sse(raw: str) -> list[tuple[str, dict[str, Any]]]:
    """Parse an Anthropic SSE response into ``(event_name, payload)`` pairs."""
    events: list[tuple[str, dict[str, Any]]] = []
    for frame in raw.split("\n\n"):
        if not frame.strip():
            continue
        event_name = ""
        payload: dict[str, Any] = {}
        for line in frame.split("\n"):
            if line.startswith("event: "):
                event_name = line[len("event: ") :]
            elif line.startswith("data: "):
                payload = json.loads(line[len("data: ") :])
        events.append((event_name, payload))
    return events


def _text_of(events: list[tuple[str, dict[str, Any]]]) -> str:
    """Join the text deltas of an Anthropic stream."""
    return "".join(
        payload["delta"]["text"]
        for name, payload in events
        if name == "content_block_delta" and payload["delta"]["type"] == "text_delta"
    )


# ---------------------------------------------------------------------------
# Non-streaming
# ---------------------------------------------------------------------------


async def test_responses_nonstreaming_returns_anthropic_message_and_responses_body(
    client: httpx.AsyncClient, httpx_mock: HTTPXMock
) -> None:
    """Full path: Anthropic request -> /v1/responses -> Anthropic response.

    Also pins down the upstream body shape: the limit is sent as
    ``max_output_tokens`` with the reasoning level from the provider config,
    and chat-path parameters are absent from the body.
    """
    _mock_responses(httpx_mock, _responses_body(_text_output(), _TEXT_USAGE))

    response = await client.post(
        "/v1/messages",
        json=_claude_payload(temperature=0.4, top_p=0.8),
        headers=_CLIENT_HEADERS,
    )

    assert response.status_code == 200
    body = response.json()
    assert body["type"] == "message"
    assert body["role"] == "assistant"
    assert body["model"] == _RESPONSES_MODEL
    assert body["content"] == [{"type": "text", "text": _ANSWER_TEXT}]
    assert body["stop_reason"] == "end_turn"
    assert body["usage"] == _anthropic_usage(_TEXT_USAGE)

    upstream_body = _single_upstream_body(httpx_mock, _RESPONSES_URL)
    assert upstream_body["model"] == _RESPONSES_MODEL
    assert upstream_body["max_output_tokens"] == _MAX_TOKENS
    assert upstream_body["reasoning"] == {"effort": _PROVIDER_FALLBACK_EFFORT}
    assert upstream_body["input"] == [{"role": "user", "content": "2+2?"}]
    assert _CHAT_ONLY_PARAMS & upstream_body.keys() == set()


async def test_responses_tool_call_returns_tool_use_block_with_flat_upstream_tools(
    client: httpx.AsyncClient, httpx_mock: HTTPXMock
) -> None:
    """Tool call -> tool_use block to the client, flat tools shape upstream."""
    _mock_responses(httpx_mock, _responses_body(_tool_output(), _TOOL_USAGE))

    payload = _claude_payload(
        tools=[
            {
                "name": _TOOL_NAME,
                "description": "Submit the answer",
                "input_schema": _TOOL_SCHEMA,
            }
        ]
    )
    response = await client.post("/v1/messages", json=payload, headers=_CLIENT_HEADERS)

    assert response.status_code == 200
    body = response.json()
    assert body["content"] == [
        {
            "type": "tool_use",
            "id": _TOOL_CALL_ID,
            "name": _TOOL_NAME,
            "input": {"answer": 391},
        }
    ]
    assert body["stop_reason"] == "tool_use"
    assert body["usage"] == _anthropic_usage(_TOOL_USAGE)

    upstream_body = _single_upstream_body(httpx_mock, _RESPONSES_URL)
    assert upstream_body["tools"] == [
        {
            "type": "function",
            "name": _TOOL_NAME,
            "description": "Submit the answer",
            "parameters": _TOOL_SCHEMA,
        }
    ]


@pytest.mark.parametrize(
    ("payload_overrides", "expected_effort"),
    [
        ({"output_config": {"effort": _REQUEST_EFFORT}}, _REQUEST_EFFORT),
        ({}, _PROVIDER_FALLBACK_EFFORT),
    ],
    ids=["effort_from_output_config", "effort_from_provider_fallback"],
)
async def test_reasoning_effort_reaches_upstream_from_request_then_provider_fallback(
    client: httpx.AsyncClient,
    httpx_mock: HTTPXMock,
    payload_overrides: dict[str, Any],
    expected_effort: str,
) -> None:
    """Reasoning level: from output_config verbatim, otherwise from the provider config."""
    _mock_responses(httpx_mock, _responses_body(_text_output(), _TEXT_USAGE))

    response = await client.post(
        "/v1/messages",
        json=_claude_payload(**payload_overrides),
        headers=_CLIENT_HEADERS,
    )

    assert response.status_code == 200
    upstream_body = _single_upstream_body(httpx_mock, _RESPONSES_URL)
    assert upstream_body["reasoning"] == {"effort": expected_effort}


# ---------------------------------------------------------------------------
# Streaming
# ---------------------------------------------------------------------------


async def test_responses_streaming_yields_anthropic_event_sequence_with_nonempty_content(
    client: httpx.AsyncClient, httpx_mock: HTTPXMock
) -> None:
    """Responses API events -> a correct Anthropic SSE event sequence."""
    _mock_responses_stream(httpx_mock, _text_stream_events())

    response = await client.post(
        "/v1/messages", json=_claude_payload(stream=True), headers=_CLIENT_HEADERS
    )

    assert response.status_code == 200
    events = _parse_anthropic_sse(response.text)
    names = [name for name, _ in events]

    assert names[:3] == ["message_start", "content_block_start", "ping"]
    assert names[-3:] == ["content_block_stop", "message_delta", "message_stop"]
    assert "error" not in names
    assert _text_of(events) == _ANSWER_TEXT

    message_delta = next(payload for name, payload in events if name == "message_delta")
    assert message_delta["delta"]["stop_reason"] == "end_turn"
    assert message_delta["usage"] == _anthropic_usage(_TEXT_USAGE)

    upstream_body = _single_upstream_body(httpx_mock, _RESPONSES_URL)
    assert upstream_body["stream"] is True
    assert _CHAT_ONLY_PARAMS & upstream_body.keys() == set()


async def test_responses_streaming_tool_call_yields_tool_use_block_to_client(
    client: httpx.AsyncClient, httpx_mock: HTTPXMock
) -> None:
    """Streaming with a tool -> a tool_use block and args in one input_json_delta."""
    _mock_responses_stream(httpx_mock, _tool_stream_events())

    response = await client.post(
        "/v1/messages", json=_claude_payload(stream=True), headers=_CLIENT_HEADERS
    )

    assert response.status_code == 200
    events = _parse_anthropic_sse(response.text)

    tool_starts = [
        payload
        for name, payload in events
        if name == "content_block_start" and payload["content_block"]["type"] == "tool_use"
    ]
    assert len(tool_starts) == 1
    assert tool_starts[0]["content_block"]["id"] == _TOOL_CALL_ID
    assert tool_starts[0]["content_block"]["name"] == _TOOL_NAME

    json_deltas = [
        payload
        for name, payload in events
        if name == "content_block_delta" and payload["delta"]["type"] == "input_json_delta"
    ]
    assert len(json_deltas) == 1
    assert json.loads(json_deltas[0]["delta"]["partial_json"]) == {"answer": 391}

    message_delta = next(payload for name, payload in events if name == "message_delta")
    assert message_delta["delta"]["stop_reason"] == "tool_use"


# ---------------------------------------------------------------------------
# Regression: the chat path is unaffected by adding the Responses API
# ---------------------------------------------------------------------------


async def test_openai_compatible_provider_still_uses_chat_completions_endpoint(
    client: httpx.AsyncClient, httpx_mock: HTTPXMock
) -> None:
    """Regression: GLM via openai_compatible goes to /v1/chat/completions, not /v1/responses."""
    httpx_mock.add_response(
        url=_OPENAI_COMPATIBLE_CHAT_URL,
        method="POST",
        json=_GLM_CHAT_RESPONSE,
        status_code=200,
        headers={"content-type": "application/json"},
    )

    payload = _claude_payload(model=_GLM_MODEL)
    response = await client.post("/v1/messages", json=payload, headers=_CLIENT_HEADERS)

    assert response.status_code == 200
    body = response.json()
    assert body["content"] == [{"type": "text", "text": "Hello from GLM mock"}]
    assert body["stop_reason"] == "end_turn"

    outbound_urls = [str(request.url) for request in httpx_mock.get_requests()]
    assert outbound_urls == [_OPENAI_COMPATIBLE_CHAT_URL]

    upstream_body = _single_upstream_body(httpx_mock, _OPENAI_COMPATIBLE_CHAT_URL)
    assert upstream_body["messages"] == [{"role": "user", "content": "2+2?"}]
    assert upstream_body["max_tokens"] == _MAX_TOKENS


async def test_openai_chat_flavor_provider_still_uses_chat_completions_endpoint(
    client: httpx.AsyncClient, httpx_mock: HTTPXMock
) -> None:
    """Regression: the openai provider with api_flavor=chat stays on /v1/chat/completions."""
    httpx_mock.add_response(
        url=_OPENAI_CHAT_URL,
        method="POST",
        json=_OPENAI_CHAT_RESPONSE,
        status_code=200,
        headers={"content-type": "application/json"},
    )

    payload = _claude_payload(model=_CHAT_MODEL)
    response = await client.post("/v1/messages", json=payload, headers=_CLIENT_HEADERS)

    assert response.status_code == 200
    body = response.json()
    assert body["model"] == _CHAT_MODEL
    assert body["content"] == [{"type": "text", "text": "Hello from chat mock"}]

    assert [str(request.url) for request in httpx_mock.get_requests()] == [_OPENAI_CHAT_URL]

    upstream_body = _single_upstream_body(httpx_mock, _OPENAI_CHAT_URL)
    assert upstream_body["messages"] == [{"role": "user", "content": "2+2?"}]
    assert "max_output_tokens" not in upstream_body
    assert "reasoning" not in upstream_body


# ---------------------------------------------------------------------------
# Preserving the reasoning chain across tool-call steps
# ---------------------------------------------------------------------------


def _reasoning_item() -> dict[str, Any]:
    """A ``reasoning`` item in the shape returned by the SDK (``model_dump``)."""
    return {
        "id": _REASONING_ITEM_ID,
        "type": "reasoning",
        "summary": [],
        "content": [],
        "encrypted_content": _ENCRYPTED_CONTENT,
        "status": None,
    }


def _tool_cycle_payload() -> dict[str, Any]:
    """Second-step request: the tool call and its result are already in history."""
    return _claude_payload(
        messages=[
            {"role": "user", "content": "2+2?"},
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": _TOOL_CALL_ID,
                        "name": _TOOL_NAME,
                        "input": {"answer": 391},
                    }
                ],
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": _TOOL_CALL_ID,
                        "content": "4712",
                    }
                ],
            },
        ],
        tools=[
            {"name": _TOOL_NAME, "description": "Submit the answer", "input_schema": _TOOL_SCHEMA}
        ],
    )


def _second_upstream_input(httpx_mock: HTTPXMock) -> list[dict[str, Any]]:
    """The ``input`` array of the second outbound request to /v1/responses."""
    outbound = httpx_mock.get_requests(url=_RESPONSES_URL, method="POST")
    assert len(outbound) == 2
    body: dict[str, Any] = json.loads(outbound[1].content)
    input_items: list[dict[str, Any]] = body["input"]
    return input_items


async def test_reasoning_from_first_step_is_replayed_before_function_call_on_second(
    client: httpx.AsyncClient, httpx_mock: HTTPXMock
) -> None:
    """First-step reasoning is replayed to upstream strictly before its call.

    The client does not change the Anthropic protocol: it sends the usual
    tool_use/tool_result, and the router injects reasoning from its own cache.
    """
    _mock_responses(
        httpx_mock,
        _responses_body([_reasoning_item(), *_tool_output()], _TOOL_USAGE),
    )
    _mock_responses(httpx_mock, _responses_body(_text_output(), _TEXT_USAGE))

    first = await client.post(
        "/v1/messages",
        json=_claude_payload(
            tools=[
                {
                    "name": _TOOL_NAME,
                    "description": "Submit the answer",
                    "input_schema": _TOOL_SCHEMA,
                }
            ]
        ),
        headers=_CLIENT_HEADERS,
    )
    assert first.status_code == 200
    # Reasoning does not leak out: the client's content is just the tool call.
    assert first.json()["content"] == [
        {"type": "tool_use", "id": _TOOL_CALL_ID, "name": _TOOL_NAME, "input": {"answer": 391}}
    ]

    second = await client.post(
        "/v1/messages", json=_tool_cycle_payload(), headers=_CLIENT_HEADERS
    )
    assert second.status_code == 200

    input_items = _second_upstream_input(httpx_mock)
    assert [item.get("type") or item.get("role") for item in input_items] == [
        "user",
        "reasoning",
        "function_call",
        "function_call_output",
    ]

    reasoning = input_items[1]
    assert reasoning["encrypted_content"] == _ENCRYPTED_CONTENT
    assert reasoning["id"] == _REASONING_ITEM_ID
    # The shape upstream accepts: status is absent, summary is required.
    assert "status" not in reasoning
    assert "summary" in reasoning


async def test_streaming_first_step_also_feeds_reasoning_into_next_request(
    client: httpx.AsyncClient, httpx_mock: HTTPXMock
) -> None:
    """A streaming first step fills the cache from the terminal event."""
    stream_events = _tool_stream_events()[:-1] + [
        {
            "type": "response.completed",
            "response": {
                "id": _RESPONSE_ID,
                "status": "completed",
                "usage": _TOOL_USAGE,
                "output": [_reasoning_item(), *_tool_output()],
            },
        }
    ]
    _mock_responses_stream(httpx_mock, stream_events)
    _mock_responses(httpx_mock, _responses_body(_text_output(), _TEXT_USAGE))

    first = await client.post(
        "/v1/messages", json=_claude_payload(stream=True), headers=_CLIENT_HEADERS
    )
    assert first.status_code == 200

    second = await client.post(
        "/v1/messages", json=_tool_cycle_payload(), headers=_CLIENT_HEADERS
    )
    assert second.status_code == 200

    input_items = _second_upstream_input(httpx_mock)
    assert [item.get("type") or item.get("role") for item in input_items] == [
        "user",
        "reasoning",
        "function_call",
        "function_call_output",
    ]
    assert input_items[1]["encrypted_content"] == _ENCRYPTED_CONTENT


async def test_second_step_without_cached_reasoning_keeps_previous_input_shape(
    client: httpx.AsyncClient, httpx_mock: HTTPXMock
) -> None:
    """Cache miss: the ``input`` shape is unchanged, no client-facing error."""
    _mock_responses(httpx_mock, _responses_body(_tool_output(), _TOOL_USAGE))
    _mock_responses(httpx_mock, _responses_body(_text_output(), _TEXT_USAGE))

    first = await client.post(
        "/v1/messages",
        json=_claude_payload(
            tools=[
                {
                    "name": _TOOL_NAME,
                    "description": "Submit the answer",
                    "input_schema": _TOOL_SCHEMA,
                }
            ]
        ),
        headers=_CLIENT_HEADERS,
    )
    assert first.status_code == 200

    second = await client.post(
        "/v1/messages", json=_tool_cycle_payload(), headers=_CLIENT_HEADERS
    )
    assert second.status_code == 200

    input_items = _second_upstream_input(httpx_mock)
    assert [item.get("type") or item.get("role") for item in input_items] == [
        "user",
        "function_call",
        "function_call_output",
    ]
