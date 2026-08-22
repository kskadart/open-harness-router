"""Integration tests for request validation on ``POST /v1/messages``."""

from __future__ import annotations

import httpx


async def test_invalid_json_body_returns_400_with_anthropic_error(
    client: httpx.AsyncClient,
) -> None:
    response = await client.post(
        "/v1/messages",
        content=b"{not-json,,,",
        headers={"content-type": "application/json"},
    )
    assert response.status_code == 400
    body = response.json()
    assert body["type"] == "error"
    assert body["error"]["type"] == "invalid_request_error"
    assert "Invalid JSON body" in body["error"]["message"]


async def test_missing_model_field_returns_400_with_anthropic_error(
    client: httpx.AsyncClient,
) -> None:
    payload = {
        "max_tokens": 16,
        "messages": [{"role": "user", "content": "hi"}],
    }
    response = await client.post("/v1/messages", json=payload)
    assert response.status_code == 400
    body = response.json()
    assert body["type"] == "error"
    assert body["error"]["type"] == "invalid_request_error"
    assert "Missing 'model' field" in body["error"]["message"]


async def test_empty_model_field_returns_400_with_anthropic_error(
    client: httpx.AsyncClient,
) -> None:
    response = await client.post(
        "/v1/messages",
        json={"model": "", "max_tokens": 16, "messages": []},
    )
    assert response.status_code == 400
    body = response.json()
    assert body["type"] == "error"
    assert body["error"]["type"] == "invalid_request_error"


async def test_count_tokens_invalid_json_returns_400(client: httpx.AsyncClient) -> None:
    response = await client.post(
        "/v1/messages/count_tokens",
        content=b"broken",
        headers={"content-type": "application/json"},
    )
    assert response.status_code == 400
    body = response.json()
    assert body["type"] == "error"
    assert body["error"]["type"] == "invalid_request_error"
