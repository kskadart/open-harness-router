"""Integration test: ``POST /v1/messages`` for the openai-translate provider.

An OpenAI-compatible ChatCompletion response is mocked; the test verifies
that the provider returns the response to the client in Anthropic Messages
format.
"""

from __future__ import annotations

import json

import httpx
from pytest_httpx import HTTPXMock

_OPENAI_COMPATIBLE_CHAT_URL = "https://gateway.example.com/v1/chat/completions"
_ATTRIBUTION_CREATE = "x-anthropic-billing-header: cc_version=2.1.278.516; cch=a;"
_ATTRIBUTION_CONTINUE = "x-anthropic-billing-header: cc_version=2.1.278.516; cch=b;"
_TOOL_CALL_RESPONSE = {
    "id": "chatcmpl-test-tool",
    "object": "chat.completion",
    "created": 1_700_000_000,
    "model": "zai-org/GLM-5.2-FP8",
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
                        "function": {"name": "Bash", "arguments": '{"command": "ls"}'},
                    }
                ],
            },
            "finish_reason": "tool_calls",
        }
    ],
    "usage": {"prompt_tokens": 9, "completion_tokens": 6, "total_tokens": 15},
}

_OPENAI_RESPONSE = {
    "id": "chatcmpl-test-01",
    "object": "chat.completion",
    "created": 1_700_000_000,
    "model": "zai-org/GLM-5.2-FP8",
    "choices": [
        {
            "index": 0,
            "message": {
                "role": "assistant",
                "content": "Hello from GLM mock",
            },
            "finish_reason": "stop",
        }
    ],
    "usage": {"prompt_tokens": 7, "completion_tokens": 4, "total_tokens": 11},
}


async def test_translate_returns_anthropic_shaped_response(
    client: httpx.AsyncClient, httpx_mock: HTTPXMock
) -> None:
    httpx_mock.add_response(
        url=_OPENAI_COMPATIBLE_CHAT_URL,
        method="POST",
        json=_OPENAI_RESPONSE,
        status_code=200,
        headers={"content-type": "application/json"},
    )

    payload = {
        "model": "zai-org/GLM-5.2-FP8",
        "max_tokens": 128,
        "messages": [{"role": "user", "content": "hi"}],
    }
    response = await client.post(
        "/v1/messages",
        json=payload,
        headers={"x-api-key": "irrelevant-openai-uses-server-key"},
    )

    assert response.status_code == 200
    body = response.json()
    # The response must be in Anthropic format.
    assert body["type"] == "message"
    assert body["role"] == "assistant"
    assert body["model"] == "zai-org/GLM-5.2-FP8"
    assert body["stop_reason"] == "end_turn"
    assert isinstance(body["content"], list)
    assert body["content"] == [{"type": "text", "text": "Hello from GLM mock"}]
    assert body["usage"] == {"input_tokens": 7, "output_tokens": 4}

    # The provider must call openai_compatible, not Anthropic.
    outbound = httpx_mock.get_requests(url=_OPENAI_COMPATIBLE_CHAT_URL, method="POST")
    assert len(outbound) == 1


async def test_translate_rejects_a_thread_continuation_without_calling_upstream(
    client: httpx.AsyncClient, httpx_mock: HTTPXMock
) -> None:
    """Claude Code's thread delta (v2.1.278+, first-party host) gets 400; no upstream call."""
    payload = {
        "model": "zai-org/GLM-5.2-FP8",
        "max_tokens": 128,
        "stream": True,
        "thread": {"type": "continue", "previous_message_id": "msg_0123456789abcdef01234567"},
        "system": [{"type": "text", "text": "x-anthropic-billing-header: cc_version=2.1.278.516;"}],
        "messages": [
            {
                "role": "user",
                "content": [{"type": "tool_result", "tool_use_id": "call_1", "content": "ok"}],
            }
        ],
    }
    response = await client.post(
        "/v1/messages",
        json=payload,
        headers={"x-api-key": "irrelevant-openai-uses-server-key"},
    )

    assert response.status_code == 400
    assert response.json()["error"]["type"] == "invalid_request_error"
    assert response.headers["x-ohr-capability-rejected"] == "thread_continue"
    assert httpx_mock.get_requests() == []


async def test_translate_holds_the_thread_for_a_continuation(
    client: httpx.AsyncClient, httpx_mock: HTTPXMock
) -> None:
    """create -> tool call -> continue: the second upstream request carries the whole turn."""
    httpx_mock.add_response(
        url=_OPENAI_COMPATIBLE_CHAT_URL, method="POST", json=_TOOL_CALL_RESPONSE
    )
    httpx_mock.add_response(url=_OPENAI_COMPATIBLE_CHAT_URL, method="POST", json=_OPENAI_RESPONSE)
    headers = {"x-api-key": "irrelevant-openai-uses-server-key"}

    first = await client.post(
        "/v1/messages",
        json={
            "model": "zai-org/GLM-5.2-FP8",
            "max_tokens": 128,
            "thread": {"type": "create"},
            "system": [
                {"type": "text", "text": _ATTRIBUTION_CREATE},
                {"type": "text", "text": "Be terse."},
            ],
            "messages": [{"role": "user", "content": "hi"}],
            "tools": [{"name": "Bash", "input_schema": {"type": "object"}}],
        },
        headers=headers,
    )
    assert first.status_code == 200
    assert first.json()["stop_reason"] == "tool_use"

    second = await client.post(
        "/v1/messages",
        json={
            "model": "zai-org/GLM-5.2-FP8",
            "max_tokens": 128,
            "thread": {"type": "continue", "previous_message_id": first.json()["id"]},
            "system": [{"type": "text", "text": _ATTRIBUTION_CONTINUE}],
            "messages": [
                {
                    "role": "user",
                    "content": [{"type": "tool_result", "tool_use_id": "call_1", "content": "ok"}],
                }
            ],
        },
        headers=headers,
    )
    assert second.status_code == 200
    assert second.json()["content"] == [{"type": "text", "text": "Hello from GLM mock"}]

    outbound = httpx_mock.get_requests(url=_OPENAI_COMPATIBLE_CHAT_URL, method="POST")
    assert len(outbound) == 2
    sent = json.loads(outbound[1].content)
    assert "thread" not in sent
    roles = [message["role"] for message in sent["messages"]]
    assert roles == ["system", "user", "assistant", "tool"]
    assert sent["messages"][0]["content"].startswith(_ATTRIBUTION_CONTINUE)
    assert sent["messages"][2]["tool_calls"][0]["id"] == "call_1"
    assert sent["messages"][3]["content"] == "ok"
    assert [tool["function"]["name"] for tool in sent["tools"]] == ["Bash"]
