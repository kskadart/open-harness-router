"""Integration test: ``POST /v1/messages`` for the openai-translate provider.

An OpenAI-compatible ChatCompletion response is mocked; the test verifies
that the provider returns the response to the client in Anthropic Messages
format.
"""

from __future__ import annotations

import httpx
from pytest_httpx import HTTPXMock

_OPENAI_COMPATIBLE_CHAT_URL = "https://gateway.example.com/v1/chat/completions"

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
