"""The openai-translate provider riding out a gateway's output-token quota.

Real ``AsyncOpenAI`` over pytest-httpx, so the gateway's 422 goes through
the SDK's own error rendering. The window margin and the release stagger
are zeroed and the hints are fractions of a second, so the real waits stay
in the tens of milliseconds.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from pydantic import SecretStr
from pytest_httpx import HTTPXMock

from errors import ProviderError
from providers.openai_translate import OpenAITranslateProvider
from routing.schema import ProviderCfg, RouteLimits
from services import upstream_quota
from settings import UpstreamSettings

_BASE_URL = "https://gateway.test/v1"
_CHAT_URL = _BASE_URL + "/chat/completions"
_REFUSAL = {
    "error": "Превышен лимит completion-токенов: использовано 12487, лимит 10000. "
    "Повторите попытку через 0,05 сек."
}
_LONG_REFUSAL = {
    "error": "Превышен лимит completion-токенов: использовано 12487, лимит 10000. "
    "Повторите попытку через 3 мин."
}
_COMPLETION = {
    "id": "chatcmpl-1",
    "object": "chat.completion",
    "created": 0,
    "model": "deepseek",
    "choices": [
        {"index": 0, "message": {"role": "assistant", "content": "pong"}, "finish_reason": "stop"}
    ],
    "usage": {"prompt_tokens": 5, "completion_tokens": 1, "total_tokens": 6},
}
_STREAM_BODY = (
    'data: {"id":"c1","object":"chat.completion.chunk","created":0,"model":"deepseek",'
    '"choices":[{"index":0,"delta":{"role":"assistant","content":"pong"},'
    '"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n'
)


class _ConnectedChannel:
    async def is_disconnected(self) -> bool:
        return False


@pytest.fixture(autouse=True)
def _instant_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(upstream_quota, "WINDOW_MARGIN_S", 0.0)
    monkeypatch.setattr(upstream_quota, "RELEASE_STAGGER_S", 0.0)


def _provider(quota_wait_max_s: float = 240.0) -> OpenAITranslateProvider:
    cfg = ProviderCfg(
        type="openai-translate",
        base_url=_BASE_URL,
        api_key_env="GATEWAY_API_KEY",
        max_tokens_limit=8192,
        quota_wait_max_s=quota_wait_max_s,
    )
    return OpenAITranslateProvider(
        name="gateway",
        cfg=cfg,
        api_key=SecretStr("test-gateway-key"),
        ca_bundle_path=None,
        upstream=UpstreamSettings(),
    )


def _claude_body(**overrides: Any) -> bytes:
    payload: dict[str, Any] = {
        "model": "ag-test",
        "max_tokens": 64,
        "messages": [{"role": "user", "content": "ping"}],
    }
    payload.update(overrides)
    return json.dumps(payload).encode()


async def _handle(provider: OpenAITranslateProvider, body: bytes) -> Any:
    return await provider.handle_messages(
        body, {}, _ConnectedChannel(), "deepseek", RouteLimits.resolve(provider.cfg, None)
    )


async def test_non_streaming_request_waits_out_the_window(httpx_mock: HTTPXMock) -> None:
    """422 quota refusal, then success: the client sees only the success."""
    httpx_mock.add_response(url=_CHAT_URL, method="POST", status_code=422, json=_REFUSAL)
    httpx_mock.add_response(url=_CHAT_URL, method="POST", json=_COMPLETION)

    provider = _provider()
    try:
        result = await _handle(provider, _claude_body())
    finally:
        await provider.aclose()

    assert result.status_code == 200
    assert len(httpx_mock.get_requests()) == 2


async def test_streaming_request_waits_out_the_window(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(url=_CHAT_URL, method="POST", status_code=422, json=_REFUSAL)
    httpx_mock.add_response(
        url=_CHAT_URL,
        method="POST",
        headers={"content-type": "text/event-stream"},
        text=_STREAM_BODY,
    )

    provider = _provider()
    try:
        result = await _handle(provider, _claude_body(stream=True))
        chunks = [chunk async for chunk in result.body]
    finally:
        await provider.aclose()

    assert result.status_code == 200
    assert any(b"pong" in chunk for chunk in chunks)
    assert len(httpx_mock.get_requests()) == 2


async def test_window_beyond_budget_becomes_429_with_retry_after(httpx_mock: HTTPXMock) -> None:
    """A 3-minute window over a 60 s budget: 429, retry-after capped at 60, no second attempt.

    Claude Code gives up at once on ``retry-after: 180`` but retries a 60 s
    one, so the header is capped; the message still names the real window.
    """
    httpx_mock.add_response(url=_CHAT_URL, method="POST", status_code=422, json=_LONG_REFUSAL)

    provider = _provider(quota_wait_max_s=60)
    try:
        result = await _handle(provider, _claude_body(stream=True))
    finally:
        await provider.aclose()

    assert result.status_code == 429
    assert result.headers["retry-after"] == "60"
    error = json.loads(result.body)["error"]
    assert error["type"] == "rate_limit_error"
    assert "reopens in 180 s" in error["message"]
    assert "лимит completion-токенов" in error["message"]
    assert len(httpx_mock.get_requests()) == 1


async def test_closed_gate_answers_later_requests_without_calling_upstream(
    httpx_mock: HTTPXMock,
) -> None:
    """While the window is closed, a new request does not spend another refusal."""
    httpx_mock.add_response(url=_CHAT_URL, method="POST", status_code=422, json=_LONG_REFUSAL)

    provider = _provider(quota_wait_max_s=0)
    try:
        first = await _handle(provider, _claude_body())
        second = await _handle(provider, _claude_body())
    finally:
        await provider.aclose()

    assert first.status_code == second.status_code == 429
    assert len(httpx_mock.get_requests()) == 1


async def test_genuine_422_is_not_mistaken_for_quota(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        url=_CHAT_URL, method="POST", status_code=422, json={"error": "messages: field required"}
    )

    provider = _provider()
    try:
        with pytest.raises(ProviderError) as exc_info:
            await provider.create_chat_completion(
                {"model": "deepseek", "messages": [], "stream": False}, None
            )
    finally:
        await provider.aclose()

    assert exc_info.value.status_code == 422
    assert exc_info.value.retry_after_s is None
    assert len(httpx_mock.get_requests()) == 1
