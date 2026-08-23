"""Integration test: the actual vulnerability this change fixes.

``forward_client_auth: false`` (own-key passthrough) must send NO client
credential -- not ``authorization``, not ``x-api-key``, not ``cookie``, not
any other header the client happens to carry -- to a third-party
Anthropic-compatible upstream, and inject exactly the provider's own key.
Own-key mode uses an ALLOWLIST (``services.header_utils.own_key_headers``),
not a denylist over the two known auth headers: a denylist would still leak
e.g. a cookie session token. Supplementing instead of replacing the known
auth headers would also leak the client's Claude Code OAuth token -- the
Anthropic SDK is known to emit both authorization and x-api-key at once
when both an auth token and an API key are configured
(anthropics/anthropic-sdk-csharp#47) -- and Anthropic bans using a
subscription credential with third-party products.

Follows the isolated-app pattern from ``test_messages_translate_kimi.py``:
own-key passthrough providers need their own ``api_key_env`` in the
environment, so this test does not reuse the shared
``tests/fixtures/routing_test.yaml``/``tests/conftest.py`` fixtures (adding
them there would require every integration test in the project to carry
those environment variables).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
from pytest_httpx import HTTPXMock

from main import create_app
from routing.config_loader import load_routing_config
from routing.registry import ProviderRegistry
from settings import Settings

_PROJECT_ROOT = Path(__file__).resolve().parents[2]

_MOONSHOT_MESSAGES_URL = "https://api.moonshot.ai/anthropic/v1/messages"
_MOONSHOT_MODEL = "moonshot-anthropic-v1"
_MOONSHOT_OWN_KEY = "moonshot-server-side-secret"

_KIMI_CODE_MESSAGES_URL = "https://api.kimi.com/coding/v1/messages"
_KIMI_CODE_MODEL = "kimicode-k3"
_KIMI_CODE_OWN_KEY = "kimi-code-server-side-secret"

# Simulates the Anthropic SDK sending both credentials at once (see the
# module docstring): the client's OAuth Bearer token AND its x-api-key, plus
# a third credential (cookie) that a denylist over just those two would miss.
_CLIENT_HEADERS = {
    "authorization": "Bearer client-claude-code-oauth-token",
    "x-api-key": "client-side-anthropic-key",
    "cookie": "sessionKey=sk-ant-sid01-client-session-token",
    "anthropic-version": "2023-06-01",
}


def _assert_no_client_credential_leaked(upstream_headers: httpx.Headers) -> None:
    """No client credential value may appear anywhere in the outgoing headers.

    Checks by VALUE across every outgoing header, not just the
    authorization/x-api-key slots -- the actual vulnerability this test
    guards against is a client credential surviving under some OTHER header
    name (see the module docstring).
    """
    outgoing_values = set(upstream_headers.values())
    assert _CLIENT_HEADERS["authorization"] not in outgoing_values
    assert _CLIENT_HEADERS["x-api-key"] not in outgoing_values
    assert _CLIENT_HEADERS["cookie"] not in outgoing_values
    assert "cookie" not in upstream_headers

# Isolated registry: anthropic stays the default (forward_client_auth=true,
# required by the schema's default-route invariant). Two own-key
# passthrough providers cover both auth_header styles, mirroring the two
# real Kimi/Moonshot endpoint variants documented in routing.example.yaml.
_ROUTING_YAML = """
version: 1
providers:
  anthropic:
    type: passthrough
    base_url: https://api.anthropic.com
    forward_client_auth: true
    stream_read_timeout_s: 30
  moonshot:
    type: passthrough
    base_url: https://api.moonshot.ai/anthropic
    forward_client_auth: false
    api_key_env: MOONSHOT_API_KEY
    auth_header: bearer
    stream_read_timeout_s: 30
  kimi_code:
    type: passthrough
    base_url: https://api.kimi.com/coding/
    forward_client_auth: false
    api_key_env: KIMI_CODE_API_KEY
    auth_header: x-api-key
    stream_read_timeout_s: 30
rules:
  - match: {type: prefix, value: "moonshot-"}
    provider: moonshot
  - match: {type: prefix, value: "kimicode-"}
    provider: kimi_code
default:
  provider: anthropic
"""


def _own_key_response(model: str, msg_id: str) -> dict[str, object]:
    """Build a minimal upstream response body for the given model."""
    return {
        "id": msg_id,
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": [{"type": "text", "text": "Hi from the third-party mock"}],
        "stop_reason": "end_turn",
        "stop_sequence": None,
        "usage": {"input_tokens": 3, "output_tokens": 5},
    }


@pytest_asyncio.fixture
async def own_key_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[httpx.AsyncClient]:
    """Isolated app with own-key passthrough providers, reached through ASGITransport."""
    routing_path = tmp_path / "routing_own_key.yaml"
    routing_path.write_text(_ROUTING_YAML, encoding="utf-8")

    monkeypatch.setenv("ROUTER_CONFIG_PATH", str(routing_path))
    monkeypatch.setenv("ROUTER_CERTS_DIR", str(_PROJECT_ROOT / "certs"))
    monkeypatch.setenv("ROUTER_LOG_CONF_PATH", str(_PROJECT_ROOT / "logging_conf.json"))
    monkeypatch.setenv("MOONSHOT_API_KEY", _MOONSHOT_OWN_KEY)
    monkeypatch.setenv("KIMI_CODE_API_KEY", _KIMI_CODE_OWN_KEY)

    fastapi_app = create_app()
    settings = Settings()
    registry = ProviderRegistry.build(
        load_routing_config(settings.routing.config_path), settings
    )
    fastapi_app.state.settings = settings
    fastapi_app.state.registry = registry

    transport = httpx.ASGITransport(app=fastapi_app)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            yield client
    finally:
        await registry.close_all()


async def test_own_key_passthrough_never_forwards_client_oauth_or_api_key(
    own_key_client: httpx.AsyncClient, httpx_mock: HTTPXMock
) -> None:
    """Regression test for the vulnerability.

    The client's credentials must reach the third-party upstream nowhere --
    not in authorization, not in x-api-key, not under any other header name.
    """
    httpx_mock.add_response(
        url=_MOONSHOT_MESSAGES_URL,
        method="POST",
        json=_own_key_response(_MOONSHOT_MODEL, "msg_moonshot_test_01"),
        status_code=200,
        headers={"content-type": "application/json"},
    )

    payload = {
        "model": _MOONSHOT_MODEL,
        "max_tokens": 16,
        "messages": [{"role": "user", "content": "hi"}],
    }
    response = await own_key_client.post(
        "/v1/messages", json=payload, headers=_CLIENT_HEADERS
    )

    assert response.status_code == 200

    upstream_requests = httpx_mock.get_requests(url=_MOONSHOT_MESSAGES_URL, method="POST")
    assert len(upstream_requests) == 1
    upstream_headers = upstream_requests[0].headers

    # Exactly the provider's own key, injected via authorization (auth_header: bearer).
    assert upstream_headers.get("authorization") == f"Bearer {_MOONSHOT_OWN_KEY}"
    assert "x-api-key" not in upstream_headers
    _assert_no_client_credential_leaked(upstream_headers)


async def test_own_key_passthrough_x_api_key_style_never_forwards_client_creds(
    own_key_client: httpx.AsyncClient, httpx_mock: HTTPXMock
) -> None:
    """The x-api-key auth_header variant (Kimi Code subscription endpoint style)

    gets the same replace-not-supplement treatment as the bearer style above.
    """
    httpx_mock.add_response(
        url=_KIMI_CODE_MESSAGES_URL,
        method="POST",
        json=_own_key_response(_KIMI_CODE_MODEL, "msg_kimi_code_test_01"),
        status_code=200,
        headers={"content-type": "application/json"},
    )

    payload = {
        "model": _KIMI_CODE_MODEL,
        "max_tokens": 16,
        "messages": [{"role": "user", "content": "hi"}],
    }
    response = await own_key_client.post(
        "/v1/messages", json=payload, headers=_CLIENT_HEADERS
    )

    assert response.status_code == 200

    upstream_requests = httpx_mock.get_requests(url=_KIMI_CODE_MESSAGES_URL, method="POST")
    assert len(upstream_requests) == 1
    upstream_headers = upstream_requests[0].headers

    assert upstream_headers.get("x-api-key") == _KIMI_CODE_OWN_KEY
    _assert_no_client_credential_leaked(upstream_headers)


async def test_native_anthropic_route_still_forwards_client_credentials(
    own_key_client: httpx.AsyncClient, httpx_mock: HTTPXMock
) -> None:
    """Regression: forward_client_auth=true (native Anthropic) must still forward

    the client's own credentials unchanged -- including cookie -- this fix
    must not break the existing subscription-billed path, which legitimately
    proxies the client's full request to the client's own vendor.
    """
    httpx_mock.add_response(
        url="https://api.anthropic.com/v1/messages",
        json=_own_key_response("claude-opus-4", "msg_anthropic_test"),
        status_code=200,
        headers={"content-type": "application/json"},
    )

    payload = {
        "model": "claude-opus-4",
        "max_tokens": 16,
        "messages": [{"role": "user", "content": "hi"}],
    }
    response = await own_key_client.post(
        "/v1/messages", json=payload, headers=_CLIENT_HEADERS
    )

    assert response.status_code == 200
    upstream_requests = httpx_mock.get_requests(
        url="https://api.anthropic.com/v1/messages", method="POST"
    )
    assert len(upstream_requests) == 1
    upstream_headers = upstream_requests[0].headers
    assert upstream_headers.get("authorization") == _CLIENT_HEADERS["authorization"]
    assert upstream_headers.get("x-api-key") == _CLIENT_HEADERS["x-api-key"]
    assert upstream_headers.get("cookie") == _CLIENT_HEADERS["cookie"]
