"""Integration tests for request validation on ``POST /v1/messages``."""

from __future__ import annotations

import httpx
import pytest


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


@pytest.mark.parametrize("endpoint", ["/v1/messages", "/v1/messages/count_tokens"])
@pytest.mark.parametrize(
    ("block", "expected"),
    [
        (
            {"type": "future_block", "data": "private"},
            "cannot translate content block 'future_block'",
        ),
        ({"type": "tool_use", "id": "call1", "input": {}}, ".name: Field required"),
        ({"type": "text"}, ".text: Field required"),
        ({"data": "private"}, "A content block must have a type"),
    ],
)
async def test_bad_history_identifies_the_first_block(
    client: httpx.AsyncClient, endpoint: str, block: dict[str, object], expected: str
) -> None:
    payload = {
        "model": "GLM",
        "max_tokens": 16,
        "messages": [
            {"role": "user", "content": "private"},
            {"role": "assistant", "content": [{"type": "text", "text": "private"}, *[block] * 50]},
        ],
    }
    response = await client.post(endpoint, json=payload)
    assert response.status_code == 400
    error = response.json()["error"]
    assert error["type"] == "invalid_request_error"
    assert error["message"].startswith("messages[1].content[1]")
    assert expected in error["message"]
    assert "private" not in response.text
    assert len(error["message"]) < 250
