"""Integration test: routing for the Kimi model (Moonshot AI).

This test deliberately does NOT use the shared
``tests/fixtures/routing_test.yaml`` fixture and the shared client from
``tests/conftest.py``. The ``kimi`` provider is openai-translate, so it
requires ``api_key_env`` (see ``routing/schema.py``), and
``ProviderRegistry.build`` builds ALL providers in the config eagerly (see
``routing/registry.py``): adding the ``kimi`` provider to the shared fixture
would require a ``MOONSHOT_API_KEY`` environment variable in the shared
``_env`` fixture (``tests/conftest.py``), and editing that file is outside
the scope of this task. Without it, ``create_app()``/``ProviderRegistry.build``
would fail with a ConfigError for EVERY integration test in the project, not
just this one.

So the test builds its own, fully isolated FastAPI app with a separate
temporary routing YAML (only ``anthropic`` + ``kimi``) and its own set of
environment variables -- mirroring the fixture logic from
``tests/conftest.py`` locally, without modifying the shared files.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from pytest_httpx import HTTPXMock

from main import create_app
from routing.config_loader import load_routing_config
from routing.registry import ProviderRegistry
from settings import Settings

_PROJECT_ROOT = Path(__file__).resolve().parents[2]

_MOONSHOT_CHAT_URL = "https://api.moonshot.ai/v1/chat/completions"
_KIMI_MODEL = "kimi-k3"
_CLIENT_HEADERS = {"x-api-key": "irrelevant-kimi-uses-server-key"}

# Isolated registry: only what's needed to verify the route to kimi
# (anthropic is required -- default.provider must be passthrough).
_ROUTING_YAML = """
version: 1
providers:
  anthropic:
    type: passthrough
    base_url: https://api.anthropic.com
    forward_client_auth: true
    timeout_s: 30
    stream_read_timeout_s: 30
  kimi:
    type: openai-translate
    base_url: https://api.moonshot.ai/v1
    api_key_env: MOONSHOT_API_KEY
    drop_params: [temperature, top_p]
    token_param: max_completion_tokens
    max_tokens_limit: 131072
    timeout_s: 30
rules:
  - match: {type: prefix, value: "kimi-"}
    provider: kimi
default:
  provider: anthropic
"""

_KIMI_RESPONSE = {
    "id": "chatcmpl-kimi-test-01",
    "object": "chat.completion",
    "created": 1_700_000_000,
    "model": _KIMI_MODEL,
    "choices": [
        {
            "index": 0,
            "message": {"role": "assistant", "content": "Hello from Kimi mock"},
            "finish_reason": "stop",
        }
    ],
    "usage": {"prompt_tokens": 6, "completion_tokens": 5, "total_tokens": 11},
}


@pytest_asyncio.fixture
async def kimi_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[FastAPI]:
    """Isolated FastAPI app with the ``kimi`` provider.

    Does not reuse the shared ``tests/fixtures/routing_test.yaml`` (see the
    module docstring) -- builds its own temporary routing YAML and its own
    set of environment variables, following the pattern of
    ``tests/conftest.py``.
    """
    routing_path = tmp_path / "routing_kimi.yaml"
    routing_path.write_text(_ROUTING_YAML, encoding="utf-8")

    monkeypatch.setenv("ROUTER_CONFIG_PATH", str(routing_path))
    monkeypatch.setenv("ROUTER_CERTS_DIR", str(_PROJECT_ROOT / "certs"))
    monkeypatch.setenv(
        "ROUTER_LOG_CONF_PATH", str(_PROJECT_ROOT / "logging_conf.json")
    )
    monkeypatch.setenv("MOONSHOT_API_KEY", "test-moonshot-key")

    fastapi_app = create_app()
    settings = Settings()
    registry = ProviderRegistry.build(
        load_routing_config(settings.routing.config_path), settings
    )
    fastapi_app.state.settings = settings
    fastapi_app.state.registry = registry
    try:
        yield fastapi_app
    finally:
        await registry.close_all()


@pytest_asyncio.fixture
async def kimi_client(kimi_app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    """HTTP client that reaches the isolated kimi app through ASGITransport."""
    transport = httpx.ASGITransport(app=kimi_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


def _single_upstream_body(httpx_mock: HTTPXMock, url: str) -> dict[str, Any]:
    """Body of the single outbound POST request to the given URL."""
    outbound = httpx_mock.get_requests(url=url, method="POST")
    assert len(outbound) == 1
    body: dict[str, Any] = json.loads(outbound[0].content)
    return body


async def test_kimi_model_routes_to_moonshot_chat_completions(
    kimi_client: httpx.AsyncClient, httpx_mock: HTTPXMock
) -> None:
    """The ``kimi-k3`` model routes to the kimi provider (api.moonshot.ai), not anthropic."""
    httpx_mock.add_response(
        url=_MOONSHOT_CHAT_URL,
        method="POST",
        json=_KIMI_RESPONSE,
        status_code=200,
        headers={"content-type": "application/json"},
    )

    payload = {
        "model": _KIMI_MODEL,
        "max_tokens": 256,
        "temperature": 0.4,
        "top_p": 0.8,
        "messages": [{"role": "user", "content": "hi"}],
    }
    response = await kimi_client.post(
        "/v1/messages", json=payload, headers=_CLIENT_HEADERS
    )

    assert response.status_code == 200
    body = response.json()
    assert body["type"] == "message"
    assert body["role"] == "assistant"
    assert body["model"] == _KIMI_MODEL
    assert body["content"] == [{"type": "text", "text": "Hello from Kimi mock"}]
    assert body["stop_reason"] == "end_turn"

    # The provider must call Kimi, not Anthropic (the default route).
    outbound_urls = [str(request.url) for request in httpx_mock.get_requests()]
    assert outbound_urls == [_MOONSHOT_CHAT_URL]

    upstream_body = _single_upstream_body(httpx_mock, _MOONSHOT_CHAT_URL)
    assert upstream_body["model"] == _KIMI_MODEL
    assert upstream_body["messages"] == [{"role": "user", "content": "hi"}]
    # token_param=max_completion_tokens: the limit is renamed, max_tokens is absent.
    assert upstream_body["max_completion_tokens"] == 256
    assert "max_tokens" not in upstream_body
    # drop_params=[temperature, top_p]: the Kimi K series pins these values on
    # its own side and returns HTTP 400 for any other value.
    assert "temperature" not in upstream_body
    assert "top_p" not in upstream_body


async def test_non_kimi_model_still_falls_back_to_default_anthropic_route(
    kimi_client: httpx.AsyncClient, httpx_mock: HTTPXMock
) -> None:
    """Regression: a model without the ``kimi-`` prefix is not caught by the new rule."""
    httpx_mock.add_response(
        url="https://api.anthropic.com/v1/messages",
        method="POST",
        json={
            "id": "msg_test",
            "type": "message",
            "role": "assistant",
            "model": "claude-opus-4",
            "content": [{"type": "text", "text": "hi from anthropic"}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 1, "output_tokens": 1},
        },
        status_code=200,
        headers={"content-type": "application/json"},
    )

    payload = {
        "model": "claude-opus-4",
        "max_tokens": 16,
        "messages": [{"role": "user", "content": "hi"}],
    }
    response = await kimi_client.post(
        "/v1/messages",
        json=payload,
        headers={"x-api-key": "anthropic-key", "anthropic-version": "2023-06-01"},
    )

    assert response.status_code == 200
    outbound_urls = [str(request.url) for request in httpx_mock.get_requests()]
    assert outbound_urls == ["https://api.anthropic.com/v1/messages"]
