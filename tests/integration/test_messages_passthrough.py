"""Integration test: ``POST /v1/messages`` for the passthrough provider.

The ``https://api.anthropic.com/v1/messages`` response is mocked via
pytest-httpx. The test verifies that the provider transparently proxies the
bytes and forwards ``x-api-key`` unchanged.
"""

from __future__ import annotations

import json

import httpx
from pytest_httpx import HTTPXMock

_ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"

_ANTHROPIC_RESPONSE = {
    "id": "msg_test_01",
    "type": "message",
    "role": "assistant",
    "model": "claude-opus-4-8",
    "content": [{"type": "text", "text": "Hi from Anthropic mock"}],
    "stop_reason": "end_turn",
    "stop_sequence": None,
    "usage": {"input_tokens": 3, "output_tokens": 5},
}


async def test_passthrough_forwards_claude_request_and_returns_upstream_body(
    client: httpx.AsyncClient, httpx_mock: HTTPXMock
) -> None:
    httpx_mock.add_response(
        url=_ANTHROPIC_URL,
        method="POST",
        json=_ANTHROPIC_RESPONSE,
        status_code=200,
        headers={"content-type": "application/json"},
    )

    payload = {
        "model": "claude-opus-4-8",
        "max_tokens": 16,
        "messages": [{"role": "user", "content": "hi"}],
    }
    response = await client.post(
        "/v1/messages",
        json=payload,
        headers={"x-api-key": "test-client-key", "anthropic-version": "2023-06-01"},
    )

    assert response.status_code == 200
    assert response.json() == _ANTHROPIC_RESPONSE

    upstream_requests = httpx_mock.get_requests(url=_ANTHROPIC_URL, method="POST")
    assert len(upstream_requests) == 1
    upstream = upstream_requests[0]
    # Passthrough must forward the client's x-api-key and protocol version.
    assert upstream.headers.get("x-api-key") == "test-client-key"
    assert upstream.headers.get("anthropic-version") == "2023-06-01"
    # The body must be proxied byte-for-byte.
    assert json.loads(upstream.content) == payload
