"""Regression tests for the explicit ``stream`` flag in upstream request bodies.

Some OpenAI-compatible gateways answer ``400`` with an empty body to
``POST /v1/chat/completions`` when the JSON body carries no usable
``stream`` field, and accept the same request once ``"stream": false`` is
present. The tests capture the JSON body the SDK actually puts on the wire
(pytest-httpx on top of a real ``AsyncOpenAI``), so they cover the SDK's
serialization as well as the translator's request assembly.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from pydantic import SecretStr
from pytest_httpx import HTTPXMock

from providers.openai_translate import OpenAITranslateProvider
from routing.schema import ApiFlavor, ProviderCfg, RouteLimits
from settings import UpstreamSettings

_BASE_URL = "https://gateway.example/v1"
_MODEL = "glm-5.2"
_ENDPOINT_PATH: dict[ApiFlavor, str] = {
    "chat": "/chat/completions",
    "responses": "/responses",
}

# Minimal upstream bodies accepted by the non-streaming response converters.
_CHAT_COMPLETION_BODY: dict[str, Any] = {
    "id": "chatcmpl-test",
    "object": "chat.completion",
    "created": 0,
    "model": _MODEL,
    "choices": [
        {
            "index": 0,
            "message": {"role": "assistant", "content": "Hi"},
            "finish_reason": "stop",
        }
    ],
    "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
}
_RESPONSES_BODY: dict[str, Any] = {
    "id": "resp_test",
    "object": "response",
    "created_at": 0,
    "model": _MODEL,
    "status": "completed",
    "output": [
        {
            "type": "message",
            "id": "msg_test",
            "role": "assistant",
            "status": "completed",
            "content": [{"type": "output_text", "text": "Hi", "annotations": []}],
        }
    ],
    "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
}
_UPSTREAM_BODY: dict[ApiFlavor, dict[str, Any]] = {
    "chat": _CHAT_COMPLETION_BODY,
    "responses": _RESPONSES_BODY,
}
# create(stream=True) only needs the connection to open; the body is never
# consumed by these tests.
_STREAM_BODY: dict[ApiFlavor, str] = {
    "chat": (
        'data: {"id":"chatcmpl-test","choices":[{"index":0,'
        '"delta":{"role":"assistant"},"finish_reason":null}]}\n\n'
        "data: [DONE]\n\n"
    ),
    "responses": (
        'event: response.created\ndata: {"type":"response.created",'
        '"response":{"id":"resp_test"}}\n\n'
    ),
}


class _ConnectedChannel:
    """Client channel that never disconnects."""

    async def is_disconnected(self) -> bool:
        """Report the client as still connected."""
        return False


def _provider(api_flavor: ApiFlavor) -> OpenAITranslateProvider:
    """Create a provider on a real AsyncOpenAI so the captured body is what the SDK sends."""
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


def _claude_body(**overrides: Any) -> bytes:
    """A minimal Anthropic request body with overridable fields."""
    payload: dict[str, Any] = {
        "model": "claude-sonnet-4",
        "max_tokens": 128,
        "messages": [{"role": "user", "content": "Hello"}],
    }
    payload.update(overrides)
    return json.dumps(payload).encode()


async def _sent_body(
    httpx_mock: HTTPXMock,
    api_flavor: ApiFlavor,
    claude_body: bytes,
    **upstream_response: Any,
) -> dict[str, Any]:
    """Run ``handle_messages`` against the mocked upstream and return the JSON body it sent."""
    httpx_mock.add_response(
        url=_BASE_URL + _ENDPOINT_PATH[api_flavor], method="POST", **upstream_response
    )
    provider = _provider(api_flavor)
    try:
        await provider.handle_messages(
            claude_body, {}, _ConnectedChannel(), _MODEL, RouteLimits.resolve(provider.cfg, None)
        )
    finally:
        await provider.aclose()
    upstream_request = httpx_mock.get_request()
    assert upstream_request is not None
    return json.loads(upstream_request.content)


@pytest.mark.parametrize("api_flavor", ["chat", "responses"])
async def test_handle_messages_non_streaming_request_sends_explicit_stream_false(
    httpx_mock: HTTPXMock, api_flavor: ApiFlavor
) -> None:
    """A client body without ``stream`` yields an explicit ``"stream": false`` upstream."""
    sent = await _sent_body(
        httpx_mock, api_flavor, _claude_body(), json=_UPSTREAM_BODY[api_flavor]
    )
    assert sent["stream"] is False


@pytest.mark.parametrize("api_flavor", ["chat", "responses"])
async def test_handle_messages_stream_null_request_sends_explicit_stream_false(
    httpx_mock: HTTPXMock, api_flavor: ApiFlavor
) -> None:
    """``"stream": null`` from the client is normalized to ``false`` upstream, not forwarded."""
    sent = await _sent_body(
        httpx_mock, api_flavor, _claude_body(stream=None), json=_UPSTREAM_BODY[api_flavor]
    )
    assert sent["stream"] is False


@pytest.mark.parametrize("api_flavor", ["chat", "responses"])
async def test_handle_messages_streaming_request_sends_stream_true(
    httpx_mock: HTTPXMock, api_flavor: ApiFlavor
) -> None:
    """A streaming client request keeps ``"stream": true`` upstream."""
    sent = await _sent_body(
        httpx_mock,
        api_flavor,
        _claude_body(stream=True),
        headers={"content-type": "text/event-stream"},
        text=_STREAM_BODY[api_flavor],
    )
    assert sent["stream"] is True
