"""Message threads (Claude Code v2.1.278+) on the translated provider.

On a first-party host -- the forward-proxy -- Claude Code sends the first
request of a turn with ``thread: {"type": "create"}`` and the tool-result
steps after it as deltas with ``thread: {"type": "continue",
"previous_message_id": ...}``: the attribution block as the whole
``system``, the new tool results only, no earlier messages and no tools.
The provider holds the thread: it records every response handed out under
a ``thread`` field and rebuilds a continuation into the full conversation
before translation, so the upstream sees the history, the assistant turn
it produced and the new tool results. A continuation it holds nothing for
gets 400 without an upstream call, marked for the monitor; the client then
resends the step in full. The ``thread`` field itself never goes upstream.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from pydantic import SecretStr
from pytest_httpx import HTTPXMock

from const import CAPABILITY_REJECTED_HEADER
from providers.openai_translate import OpenAITranslateProvider
from routing.schema import ApiFlavor, ProviderCfg, RouteLimits
from settings import UpstreamSettings

_BASE_URL = "https://gateway.example/v1"
_CHAT_URL = _BASE_URL + "/chat/completions"
_MODEL = "zai-org/GLM-5.3-Flash"
_ATTRIBUTION_CREATE = "x-anthropic-billing-header: cc_version=2.1.278.516; cch=8faa5;"
_ATTRIBUTION_CONTINUE = "x-anthropic-billing-header: cc_version=2.1.278.516; cch=e40a9;"
_TOOL_CALL_ARGUMENTS = '{"command": "ls"}'

# A non-streaming upstream answer that calls a tool: what a create request gets.
_TOOL_CALL_COMPLETION: dict[str, Any] = {
    "id": "chatcmpl-tool-call",
    "object": "chat.completion",
    "created": 0,
    "model": _MODEL,
    "choices": [
        {
            "index": 0,
            "message": {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "Bash", "arguments": _TOOL_CALL_ARGUMENTS},
                    }
                ],
            },
            "finish_reason": "tool_calls",
        }
    ],
    "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
}
# A non-streaming upstream answer with text: what the continuation gets.
_TEXT_COMPLETION: dict[str, Any] = {
    "id": "chatcmpl-text",
    "object": "chat.completion",
    "created": 0,
    "model": _MODEL,
    "choices": [
        {"index": 0, "message": {"role": "assistant", "content": "Done."}, "finish_reason": "stop"}
    ],
    "usage": {"prompt_tokens": 20, "completion_tokens": 2, "total_tokens": 22},
}
# The same tool call as an upstream SSE stream.
_TOOL_CALL_STREAM = (
    'data: {"id":"chatcmpl-s","choices":[{"index":0,"delta":{"role":"assistant",'
    '"content":"On it."},"finish_reason":null}]}\n\n'
    'data: {"id":"chatcmpl-s","choices":[{"index":0,"delta":{"tool_calls":[{"index":0,'
    '"id":"call_s","type":"function","function":{"name":"Bash","arguments":""}}]},'
    '"finish_reason":null}]}\n\n'
    'data: {"id":"chatcmpl-s","choices":[{"index":0,"delta":{"tool_calls":[{"index":0,'
    '"function":{"arguments":"{\\"command\\": \\"ls\\"}"}}]},"finish_reason":null}]}\n\n'
    'data: {"id":"chatcmpl-s","choices":[{"index":0,"delta":{},"finish_reason":"tool_calls"}]}\n\n'
    'data: {"id":"chatcmpl-s","choices":[],"usage":{"prompt_tokens":10,"completion_tokens":5}}\n\n'
    "data: [DONE]\n\n"
)


class _ConnectedChannel:
    """Client channel that never disconnects."""

    async def is_disconnected(self) -> bool:
        """Report the client as still connected."""
        return False


def _provider(api_flavor: ApiFlavor = "chat") -> OpenAITranslateProvider:
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


def _create_body(**overrides: Any) -> bytes:
    """The first request of a turn: the whole conversation."""
    payload: dict[str, Any] = {
        "model": "ag-GLM-5.3-Flash",
        "max_tokens": 128,
        "thread": {"type": "create"},
        "system": [
            {"type": "text", "text": _ATTRIBUTION_CREATE},
            {"type": "text", "text": "Be terse."},
        ],
        "messages": [{"role": "user", "content": "List the files."}],
        "tools": [{"name": "Bash", "input_schema": {"type": "object"}}],
    }
    payload.update(overrides)
    return json.dumps(payload).encode()


def _continue_body(previous_message_id: str, tool_use_id: str = "call_1") -> bytes:
    """The delta Claude Code 2.1.278 sends after a tool call on a first-party host."""
    return json.dumps({
        "model": "ag-GLM-5.3-Flash",
        "max_tokens": 128,
        "thread": {"type": "continue", "previous_message_id": previous_message_id},
        "system": [{"type": "text", "text": _ATTRIBUTION_CONTINUE}],
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": tool_use_id, "content": "total 736"}
                ],
            },
            {
                "role": "system",
                "content": [{"type": "text", "text": "<total_tokens>1 tokens left</total_tokens>"}],
            },
        ],
    }).encode()


async def _handle(provider: OpenAITranslateProvider, body: bytes) -> Any:
    return await provider.handle_messages(
        body, {}, _ConnectedChannel(), _MODEL, RouteLimits.resolve(provider.cfg, None)
    )


def _message_id_of(sse: bytes) -> str:
    """The id the client sees in ``message_start`` of a streamed response."""
    for line in sse.decode().split("\n"):
        if line.startswith("data: "):
            payload = json.loads(line[6:])
            if payload.get("type") == "message_start":
                return str(payload["message"]["id"])
    raise AssertionError("no message_start in the stream")


@pytest.mark.parametrize("api_flavor", ["chat", "responses"])
async def test_a_continuation_the_store_does_not_hold_is_rejected_without_an_upstream_call(
    httpx_mock: HTTPXMock, api_flavor: ApiFlavor
) -> None:
    provider = _provider(api_flavor)
    try:
        result = await _handle(provider, _continue_body("msg_never_seen"))
    finally:
        await provider.aclose()

    assert result.status_code == 400
    assert result.headers[CAPABILITY_REJECTED_HEADER] == "thread_continue"
    assert result.headers["content-type"] == "application/json"
    body = json.loads(result.body)
    assert body["type"] == "error"
    assert body["error"]["type"] == "invalid_request_error"
    assert "thread.type=continue" in body["error"]["message"]
    assert httpx_mock.get_request() is None


async def test_a_continuation_is_served_as_the_full_conversation(httpx_mock: HTTPXMock) -> None:
    """create -> tool call -> continue: the upstream sees history, assistant turn and result."""
    httpx_mock.add_response(url=_CHAT_URL, method="POST", json=_TOOL_CALL_COMPLETION)
    httpx_mock.add_response(url=_CHAT_URL, method="POST", json=_TEXT_COMPLETION)
    provider = _provider()
    try:
        first = await _handle(provider, _create_body())
        assert first.status_code == 200
        second = await _handle(provider, _continue_body(json.loads(first.body)["id"]))
    finally:
        await provider.aclose()

    assert second.status_code == 200
    assert CAPABILITY_REJECTED_HEADER not in second.headers
    assert json.loads(second.body)["content"] == [{"type": "text", "text": "Done."}]

    requests = httpx_mock.get_requests()
    assert len(requests) == 2
    sent = json.loads(requests[1].content)
    assert "thread" not in sent
    assert [message["role"] for message in sent["messages"]] == [
        "system", "user", "assistant", "tool", "user",
    ]
    # The held system prompt, with the attribution block refreshed from the delta.
    assert sent["messages"][0]["content"] == f"{_ATTRIBUTION_CONTINUE}\n\nBe terse."
    assert sent["messages"][1]["content"] == "List the files."
    assert sent["messages"][2]["tool_calls"][0]["id"] == "call_1"
    assert sent["messages"][2]["tool_calls"][0]["function"]["arguments"] == _TOOL_CALL_ARGUMENTS
    assert sent["messages"][3] == {"role": "tool", "tool_call_id": "call_1", "content": "total 736"}
    assert sent["messages"][4]["content"] == "<total_tokens>1 tokens left</total_tokens>"
    assert [tool["function"]["name"] for tool in sent["tools"]] == ["Bash"]


async def test_a_streamed_response_is_recorded_as_the_client_received_it(
    httpx_mock: HTTPXMock,
) -> None:
    """The assistant turn of a streamed tool call is rebuilt from the SSE the client saw."""
    httpx_mock.add_response(
        url=_CHAT_URL,
        method="POST",
        headers={"content-type": "text/event-stream"},
        text=_TOOL_CALL_STREAM,
    )
    httpx_mock.add_response(url=_CHAT_URL, method="POST", json=_TEXT_COMPLETION)
    provider = _provider()
    try:
        first = await _handle(provider, _create_body(stream=True))
        assert first.status_code == 200
        assert not isinstance(first.body, bytes)
        streamed = b"".join([chunk async for chunk in first.body])
        second = await _handle(provider, _continue_body(_message_id_of(streamed), "call_s"))
    finally:
        await provider.aclose()

    assert second.status_code == 200
    sent = json.loads(httpx_mock.get_requests()[1].content)
    assert sent["messages"][2] == {
        "role": "assistant",
        "content": "On it.",
        "tool_calls": [
            {
                "id": "call_s",
                "type": "function",
                "function": {"name": "Bash", "arguments": _TOOL_CALL_ARGUMENTS},
            }
        ],
    }
    assert sent["messages"][3] == {"role": "tool", "tool_call_id": "call_s", "content": "total 736"}


@pytest.mark.parametrize("thread", [None, {"type": "create"}, {"type": "future-kind"}])
async def test_other_thread_shapes_are_served_and_the_field_stays_local(
    httpx_mock: HTTPXMock, thread: dict[str, Any] | None
) -> None:
    """``create``, an unknown type and no field at all reach the upstream without ``thread``."""
    httpx_mock.add_response(url=_CHAT_URL, method="POST", json=_TEXT_COMPLETION)
    provider = _provider()
    try:
        body = _create_body(thread=thread) if thread is not None else _create_body()
        if thread is None:
            body = json.dumps({k: v for k, v in json.loads(body).items() if k != "thread"}).encode()
        result = await _handle(provider, body)
    finally:
        await provider.aclose()

    assert result.status_code == 200
    assert CAPABILITY_REJECTED_HEADER not in result.headers
    upstream_request = httpx_mock.get_request()
    assert upstream_request is not None
    sent = json.loads(upstream_request.content)
    assert "thread" not in sent
    assert sent["messages"][0]["role"] == "system"
    assert "Be terse." in sent["messages"][0]["content"]


async def _count(provider: OpenAITranslateProvider, body: bytes) -> Any:
    count_body = {k: v for k, v in json.loads(body).items() if k != "max_tokens"}
    return await provider.count_tokens(
        json.dumps(count_body).encode(), {}, _MODEL, RouteLimits.resolve(provider.cfg, None)
    )


async def test_count_tokens_counts_a_continuation_as_the_whole_conversation(
    httpx_mock: HTTPXMock,
) -> None:
    httpx_mock.add_response(url=_CHAT_URL, method="POST", json=_TOOL_CALL_COMPLETION)
    provider = _provider()
    try:
        first = await _handle(provider, _create_body())
        message_id = json.loads(first.body)["id"]
        delta = _continue_body(message_id)
        as_thread = await _count(provider, delta)
        # The same delta counted on its own, as a fresh conversation.
        alone = json.loads(delta)
        alone["thread"] = {"type": "create"}
        as_fresh = await _count(provider, json.dumps(alone).encode())
        lost = await _count(provider, _continue_body("msg_never_seen"))
    finally:
        await provider.aclose()

    assert as_thread.status_code == 200
    assert as_fresh.status_code == 200
    assert json.loads(as_thread.body)["input_tokens"] > json.loads(as_fresh.body)["input_tokens"]
    assert lost.status_code == 400
    assert lost.headers[CAPABILITY_REJECTED_HEADER] == "thread_continue"
    assert len(httpx_mock.get_requests()) == 1  # counting never calls the upstream
