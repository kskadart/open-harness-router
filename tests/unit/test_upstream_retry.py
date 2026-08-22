"""Unit tests for the openai-translate provider's retry budget on upstream failures.

Verify the real OpenAI SDK retry path (not a client mock, but HTTP
interception via pytest-httpx), configured with ``UPSTREAM_MAX_RETRIES``:

- 500 and 429 before the stream starts are retried and the request succeeds
  on a subsequent attempt;
- 400 is not retried -- the error is returned immediately;
- ``Retry-After`` from upstream is used instead of the calculated backoff;
- exhausting the budget raises ``ProviderError`` outward;
- an error arriving inside the SSE body (after HTTP 200) is NOT retried: a
  retry would splice two different generations into one response to the
  client.

Real backoff pauses are stubbed out: ``_sleep_for_retry`` computes the delay
with the real ``_calculate_retry_timeout`` (so the ``Retry-After`` check is
honest), but does not sleep -- otherwise the run would take tens of seconds.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from openai import APIError
from openai._base_client import AsyncAPIClient, FinalRequestOptions
from pydantic import SecretStr
from pytest_httpx import HTTPXMock

from const import UPSTREAM_MAX_RETRIES
from errors import ProviderError
from providers.openai_translate import OpenAITranslateProvider
from routing.schema import ProviderCfg
from settings import UpstreamSettings

_RESPONSES_URL = "https://api.openai.com/v1/responses"
_MODEL = "gpt-5.6-sol"

# Body of a successful stream response: create(stream=True) does not read the
# body, but the content-type and first event make the mock realistic.
_STREAM_BODY = (
    'event: response.created\ndata: {"type":"response.created",'
    '"response":{"id":"resp_test"}}\n\n'
)
# An error inside the SSE body after HTTP 200: this is how upstream returns
# a rate limit once the connection is already established (captured from the
# router's log).
_IN_STREAM_ERROR_BODY = (
    'data: {"error":{"type":"rate_limit_exceeded",'
    '"message":"Rate limit reached for gpt-5.6-sol"}}\n\n'
)


def _provider() -> OpenAITranslateProvider:
    """Create a provider with a real AsyncOpenAI on top of the default httpx."""
    cfg = ProviderCfg(
        type="openai-translate",
        base_url="https://api.openai.com/v1",
        api_key_env="OPENAI_API_KEY",
        token_param="max_completion_tokens",
        api_flavor="responses",
        max_tokens_limit=128000,
    )
    return OpenAITranslateProvider(
        name="openai",
        cfg=cfg,
        api_key=SecretStr("test-openai-key"),
        ca_bundle_path=None,
        upstream=UpstreamSettings(),
    )


def _stream_request() -> dict[str, Any]:
    """A minimal Responses API streaming request body."""
    return {
        "model": _MODEL,
        "input": [{"role": "user", "content": [{"type": "input_text", "text": "hi"}]}],
        "stream": True,
    }


def _error_body(message: str) -> str:
    """An upstream error body in OpenAI format."""
    return json.dumps({"error": {"message": message}})


@pytest.fixture
def retry_delays(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Record the calculated backoff delays without actually waiting them out."""
    recorded: list[float] = []

    async def _record_without_sleeping(
        self: AsyncAPIClient,
        *,
        retries_taken: int,
        max_retries: int,
        options: FinalRequestOptions,
        response: Any,
    ) -> None:
        recorded.append(
            self._calculate_retry_timeout(
                max_retries - retries_taken,
                options,
                response.headers if response is not None else None,
            )
        )

    monkeypatch.setattr(AsyncAPIClient, "_sleep_for_retry", _record_without_sleeping)
    return recorded


async def test_client_retry_budget_matches_configured_constant() -> None:
    """AsyncOpenAI is built with an explicit retry budget, not the SDK default."""
    provider = _provider()
    try:
        assert provider._client.max_retries == UPSTREAM_MAX_RETRIES
    finally:
        await provider.aclose()


async def test_create_stream_retries_on_500_then_succeeds(
    httpx_mock: HTTPXMock, retry_delays: list[float]
) -> None:
    """Two consecutive 500s before the stream starts -> the third attempt yields the stream."""
    for _ in range(2):
        httpx_mock.add_response(
            url=_RESPONSES_URL, method="POST", status_code=500, text=_error_body("server_error")
        )
    httpx_mock.add_response(
        url=_RESPONSES_URL,
        method="POST",
        status_code=200,
        headers={"content-type": "text/event-stream"},
        text=_STREAM_BODY,
    )

    provider = _provider()
    try:
        stream = await provider.create_stream(_stream_request(), "req-500")
    finally:
        await provider.aclose()

    assert stream is not None
    assert len(httpx_mock.get_requests()) == 3
    assert len(retry_delays) == 2


async def test_create_stream_retries_on_429_then_succeeds(
    httpx_mock: HTTPXMock, retry_delays: list[float]
) -> None:
    """429 before the stream starts is retried, the second attempt yields the stream."""
    httpx_mock.add_response(
        url=_RESPONSES_URL,
        method="POST",
        status_code=429,
        text=_error_body("Rate limit reached for gpt-5.6-sol"),
    )
    httpx_mock.add_response(
        url=_RESPONSES_URL,
        method="POST",
        status_code=200,
        headers={"content-type": "text/event-stream"},
        text=_STREAM_BODY,
    )

    provider = _provider()
    try:
        stream = await provider.create_stream(_stream_request(), "req-429")
    finally:
        await provider.aclose()

    assert stream is not None
    assert len(httpx_mock.get_requests()) == 2
    assert len(retry_delays) == 1


async def test_create_stream_does_not_retry_on_400(
    httpx_mock: HTTPXMock, retry_delays: list[float]
) -> None:
    """400 is a request error, not an upstream failure: there must be no retries."""
    httpx_mock.add_response(
        url=_RESPONSES_URL,
        method="POST",
        status_code=400,
        text=_error_body("Unknown parameter: 'stream_options'"),
    )

    provider = _provider()
    try:
        with pytest.raises(ProviderError) as exc_info:
            await provider.create_stream(_stream_request(), "req-400")
    finally:
        await provider.aclose()

    assert exc_info.value.status_code == 400
    assert len(httpx_mock.get_requests()) == 1
    assert retry_delays == []


async def test_create_stream_honors_retry_after_header(
    httpx_mock: HTTPXMock, retry_delays: list[float]
) -> None:
    """The pause is taken from upstream's Retry-After, not the calculated backoff."""
    retry_after_s = 7
    httpx_mock.add_response(
        url=_RESPONSES_URL,
        method="POST",
        status_code=429,
        headers={"retry-after": str(retry_after_s)},
        text=_error_body("Rate limit reached for gpt-5.6-sol"),
    )
    httpx_mock.add_response(
        url=_RESPONSES_URL,
        method="POST",
        status_code=200,
        headers={"content-type": "text/event-stream"},
        text=_STREAM_BODY,
    )

    provider = _provider()
    try:
        await provider.create_stream(_stream_request(), "req-retry-after")
    finally:
        await provider.aclose()

    # The calculated backoff for the first attempt is 0.5s with jitter; 7s
    # can only come from the header.
    assert retry_delays == [float(retry_after_s)]


async def test_create_stream_exhausted_retries_raise_error(
    httpx_mock: HTTPXMock, retry_delays: list[float]
) -> None:
    """Consecutive 500s -> budget exhausted, the error is raised outward."""
    httpx_mock.add_response(
        url=_RESPONSES_URL,
        method="POST",
        status_code=500,
        text=_error_body("server_error"),
        is_reusable=True,
    )

    provider = _provider()
    try:
        with pytest.raises(ProviderError) as exc_info:
            await provider.create_stream(_stream_request(), "req-exhausted")
    finally:
        await provider.aclose()

    assert exc_info.value.status_code == 500
    assert len(httpx_mock.get_requests()) == UPSTREAM_MAX_RETRIES + 1
    assert len(retry_delays) == UPSTREAM_MAX_RETRIES


async def test_error_inside_stream_body_is_not_retried(
    httpx_mock: HTTPXMock, retry_delays: list[float]
) -> None:
    """An error inside SSE after HTTP 200 is not retried: the stream has already started."""
    httpx_mock.add_response(
        url=_RESPONSES_URL,
        method="POST",
        status_code=200,
        headers={"content-type": "text/event-stream"},
        text=_IN_STREAM_ERROR_BODY,
        is_reusable=True,
    )

    provider = _provider()
    try:
        stream = await provider.create_stream(_stream_request(), "req-in-stream")
        # create() returned the stream: the HTTP layer succeeded, nothing to retry.
        assert len(httpx_mock.get_requests()) == 1

        with pytest.raises(APIError):
            async for _ in stream:
                pass
    finally:
        await provider.aclose()

    # Iterating the stream does not trigger a new call to upstream.
    assert len(httpx_mock.get_requests()) == 1
    assert retry_delays == []
