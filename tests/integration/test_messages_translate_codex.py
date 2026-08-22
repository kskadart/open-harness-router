"""Integration test: OpenAI codex models route to the ``openai`` provider.

The ``{type: prefix, value: "gpt-"}`` rule in ``routing.yaml`` already covers
codex models (``gpt-5-codex``, ``gpt-5.1-codex``, ``gpt-5.1-codex-mini``,
etc.) -- they share the same "gpt-" prefix as regular OpenAI chat models. No
separate provider was set up: gpt-5-codex has 400K context / 128K max output
(https://developers.openai.com/api/docs/models/gpt-5-codex), which fits
within the ``openai`` provider's already configured ``max_tokens_limit:
128000``, and the codex endpoint itself is only reachable via the Responses
API -- exactly the ``api_flavor: responses`` already enabled for ``openai``
in ``routing.yaml``.

The test uses the shared ``tests/fixtures/routing_test.yaml`` fixture (client
from ``tests/conftest.py``) unmodified -- it already contains the ``openai``
provider with ``api_flavor: responses``.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
from pytest_httpx import HTTPXMock

_RESPONSES_URL = "https://api.openai.com/v1/responses"
_CLIENT_HEADERS = {"x-api-key": "irrelevant-openai-uses-server-key"}
_MAX_TOKENS = 256

_TEXT_ITEM_ID = "msg_codex_test_item_01"
_ANSWER_TEXT = "4"

_TEXT_USAGE = {
    "input_tokens": 12,
    "input_tokens_details": {"cache_write_tokens": 0, "cached_tokens": 0},
    "output_tokens": 3,
    "output_tokens_details": {"reasoning_tokens": 0},
    "total_tokens": 15,
}


def _responses_body(model: str) -> dict[str, Any]:
    """Body of a non-streaming /v1/responses response with one text message."""
    return {
        "id": "resp_codex_test_01",
        "object": "response",
        "created_at": 1_720_000_000,
        "model": model,
        "status": "completed",
        "output": [
            {
                "id": _TEXT_ITEM_ID,
                "type": "message",
                "role": "assistant",
                "status": "completed",
                "content": [
                    {"type": "output_text", "text": _ANSWER_TEXT, "annotations": []}
                ],
            }
        ],
        "usage": _TEXT_USAGE,
    }


def _single_upstream_body(httpx_mock: HTTPXMock, url: str) -> dict[str, Any]:
    """Body of the single outbound POST request to the given URL."""
    outbound = httpx_mock.get_requests(url=url, method="POST")
    assert len(outbound) == 1
    body: dict[str, Any] = json.loads(outbound[0].content)
    return body


async def _assert_codex_model_routes_to_responses_endpoint(
    client: httpx.AsyncClient, httpx_mock: HTTPXMock, model: str
) -> None:
    httpx_mock.add_response(
        url=_RESPONSES_URL,
        method="POST",
        json=_responses_body(model),
        status_code=200,
        headers={"content-type": "application/json"},
    )

    payload = {
        "model": model,
        "max_tokens": _MAX_TOKENS,
        "messages": [{"role": "user", "content": "2+2?"}],
    }
    response = await client.post("/v1/messages", json=payload, headers=_CLIENT_HEADERS)

    assert response.status_code == 200
    body = response.json()
    assert body["type"] == "message"
    assert body["model"] == model
    assert body["content"] == [{"type": "text", "text": _ANSWER_TEXT}]

    # A codex model must go to the openai provider's /v1/responses, not to
    # /v1/chat/completions and not to Anthropic.
    outbound_urls = [str(request.url) for request in httpx_mock.get_requests()]
    assert outbound_urls == [_RESPONSES_URL]

    upstream_body = _single_upstream_body(httpx_mock, _RESPONSES_URL)
    # The client's codex model name is forwarded to upstream unchanged (no
    # upstream_model in the "gpt-" rule).
    assert upstream_body["model"] == model
    assert upstream_body["max_output_tokens"] == _MAX_TOKENS
    assert "reasoning" in upstream_body


async def test_gpt5_codex_model_routes_to_openai_responses_provider(
    client: httpx.AsyncClient, httpx_mock: HTTPXMock
) -> None:
    """``gpt-5-codex`` (the flagship codex model) routes to the openai provider."""
    await _assert_codex_model_routes_to_responses_endpoint(
        client, httpx_mock, "gpt-5-codex"
    )


async def test_gpt51_codex_mini_model_routes_to_openai_responses_provider(
    client: httpx.AsyncClient, httpx_mock: HTTPXMock
) -> None:
    """``gpt-5.1-codex-mini`` is also covered by the shared "gpt-" prefix rule."""
    await _assert_codex_model_routes_to_responses_endpoint(
        client, httpx_mock, "gpt-5.1-codex-mini"
    )
