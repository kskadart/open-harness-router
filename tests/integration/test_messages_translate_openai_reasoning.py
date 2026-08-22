"""Integration test for compatibility with GPT-5.x reasoning models.

Verifies that the openai provider on the chat path (``openai_chat``:
api_flavor=chat, token_param=max_completion_tokens,
drop_params=[temperature, top_p]) sends upstream a request body with the
token limit correctly renamed and without the forbidden parameters.
"""

from __future__ import annotations

import json

import httpx
from pytest_httpx import HTTPXMock

_OPENAI_CHAT_URL = "https://api.openai.com/v1/chat/completions"

# The chat- prefix routes to the provider with api_flavor=chat.
_CHAT_MODEL = "chat-gpt-5.6-sol"

_OPENAI_RESPONSE = {
    "id": "chatcmpl-gpt56-sol-01",
    "object": "chat.completion",
    "created": 1_720_000_000,
    "model": "gpt-5.6-sol",
    "choices": [
        {
            "index": 0,
            "message": {"role": "assistant", "content": "Hello from GPT-5.6 mock"},
            "finish_reason": "stop",
        }
    ],
    "usage": {"prompt_tokens": 5, "completion_tokens": 6, "total_tokens": 11},
}


async def test_reasoning_provider_sends_max_completion_tokens_and_drops_temperature(
    client: httpx.AsyncClient, httpx_mock: HTTPXMock
) -> None:
    httpx_mock.add_response(
        url=_OPENAI_CHAT_URL,
        method="POST",
        json=_OPENAI_RESPONSE,
        status_code=200,
        headers={"content-type": "application/json"},
    )

    payload = {
        "model": _CHAT_MODEL,
        "max_tokens": 256,
        "temperature": 0.4,
        "top_p": 0.8,
        "messages": [{"role": "user", "content": "ping"}],
    }
    response = await client.post(
        "/v1/messages",
        json=payload,
        headers={"x-api-key": "irrelevant-openai-uses-server-key"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["type"] == "message"
    assert body["role"] == "assistant"
    assert body["model"] == _CHAT_MODEL

    outbound = httpx_mock.get_requests(url=_OPENAI_CHAT_URL, method="POST")
    assert len(outbound) == 1
    upstream_body = json.loads(outbound[0].content)
    assert upstream_body["max_completion_tokens"] == 256
    assert "max_tokens" not in upstream_body
    assert "temperature" not in upstream_body
    assert "top_p" not in upstream_body
    assert upstream_body["model"] == _CHAT_MODEL
    assert upstream_body["messages"] == [{"role": "user", "content": "ping"}]
