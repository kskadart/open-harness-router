"""Unit tests for the failure detail the openai-translate provider reports.

``str()`` of ``openai.APIConnectionError`` is the fixed "Connection error.":
the transport failure behind it -- an expired upstream certificate, a
refused port, a DNS miss -- lives in ``__cause__`` (httpx raises with an
explicit ``from``). The provider's ``upstream error`` log entry and the
502 sent to the client must carry that chain, otherwise a certificate
incident is indistinguishable from a network outage (2026-09-22: half an
hour of bare "Connection error." while the gateway served an expired leaf).
"""

from __future__ import annotations

import httpx
from openai import APIConnectionError, APIStatusError, APITimeoutError
from pydantic import SecretStr

from providers.openai_translate import OpenAITranslateProvider
from routing.schema import ProviderCfg
from settings import UpstreamSettings

_REQUEST = httpx.Request("POST", "https://gateway.test/v1/chat/completions")
_SSL_DETAIL = (
    "[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: "
    "certificate has expired (_ssl.c:1028)"
)


def _provider() -> OpenAITranslateProvider:
    """Create a provider; no request leaves the process in these tests."""
    cfg = ProviderCfg(
        type="openai-translate",
        base_url="https://gateway.test/v1",
        api_key_env="GATEWAY_API_KEY",
        max_tokens_limit=8192,
    )
    return OpenAITranslateProvider(
        name="gateway",
        cfg=cfg,
        api_key=SecretStr("test-gateway-key"),
        ca_bundle_path=None,
        upstream=UpstreamSettings(),
    )


def _chained(error: APIConnectionError, *causes: BaseException) -> APIConnectionError:
    """Link ``causes`` under ``error`` the way ``raise ... from`` does, outermost first."""
    link: BaseException = error
    for cause in causes:
        link.__cause__ = cause
        link = cause
    return error


def test_connection_error_names_the_transport_failure() -> None:
    """The SSL verification failure behind "Connection error." reaches the message."""
    error = _chained(
        APIConnectionError(request=_REQUEST),
        httpx.ConnectError(_SSL_DETAIL),
        OSError(_SSL_DETAIL),
    )

    provider_error = _provider()._to_provider_error(error)

    assert provider_error.status_code == 502
    assert provider_error.message.startswith("Connection error.")
    assert f"ConnectError: {_SSL_DETAIL}" in provider_error.message
    # The innermost OSError repeats the httpx text: it adds nothing and is
    # left out, so the client-facing message stays readable.
    assert provider_error.message.count("certificate has expired") == 1
    assert "OSError" not in provider_error.message


def test_timeout_names_the_httpx_timeout() -> None:
    """A timeout is not just "Request timed out.": the httpx phase is named."""
    error = _chained(APITimeoutError(request=_REQUEST), httpx.ReadTimeout("timed out"))

    provider_error = _provider()._to_provider_error(error)

    assert provider_error.status_code == 502
    assert "ReadTimeout: timed out" in provider_error.message


def test_status_error_without_cause_is_unchanged() -> None:
    """An upstream HTTP error already carries its message; nothing is appended."""
    error = APIStatusError(
        "upstream exploded",
        response=httpx.Response(500, request=_REQUEST),
        body=None,
    )

    provider_error = _provider()._to_provider_error(error)

    assert provider_error.status_code == 500
    assert provider_error.message == "upstream exploded"
