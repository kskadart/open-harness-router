"""Unit tests for ``PassthroughProvider``'s auth-mode header handling.

Covers ``_build_headers``: the strip-then-set security fix for
``forward_client_auth: false`` (own-key mode, including the Bearer-prefix
formatting decided by ``auth_header``, and the allowlist that keeps ANY
client credential -- not just authorization/x-api-key -- from reaching a
third-party upstream) and the regression guard for
``forward_client_auth: true`` (native Anthropic, behavior must not change).
"""

from __future__ import annotations

import pytest
from pydantic import SecretStr

from providers.passthrough import PassthroughProvider
from routing.schema import ProviderCfg
from settings import UpstreamSettings

_CLIENT_HEADERS = {
    "authorization": "Bearer client-oauth-token",
    "x-api-key": "client-api-key",
    "cookie": "sessionKey=sk-ant-sid01-client-session-token",
    "anthropic-version": "2023-06-01",
    "anthropic-beta": "prompt-caching-2024-07-31",
    "content-type": "application/json",
    "accept": "application/json",
    "accept-encoding": "gzip, deflate",
    "user-agent": "claude-cli/2.1.196 (external, cli)",
    "x-unknown-custom-header": "should-not-travel-to-a-third-party-vendor",
}


def _own_key_provider(
    auth_header: str, extra_headers: dict[str, str] | None = None
) -> PassthroughProvider:
    """Build a passthrough provider in own-key mode (forward_client_auth=false)."""
    cfg = ProviderCfg(
        type="passthrough",
        base_url="https://api.moonshot.ai/anthropic",
        forward_client_auth=False,
        api_key_env="MOONSHOT_API_KEY",
        auth_header=auth_header,  # type: ignore[arg-type]
        extra_headers=extra_headers or {},
    )
    return PassthroughProvider(
        name="moonshot",
        cfg=cfg,
        upstream=UpstreamSettings(),
        api_key=SecretStr("moonshot-own-key"),
        ca_bundle_path=None,
    )


def _native_provider() -> PassthroughProvider:
    """Build a passthrough provider in native mode (forward_client_auth=true)."""
    cfg = ProviderCfg(
        type="passthrough",
        base_url="https://api.anthropic.com",
        forward_client_auth=True,
    )
    return PassthroughProvider(
        name="anthropic",
        cfg=cfg,
        upstream=UpstreamSettings(),
        api_key=None,
        ca_bundle_path=None,
    )


async def test_build_headers_bearer_replaces_client_creds_with_own_key() -> None:
    provider = _own_key_provider("bearer")
    try:
        fwd = provider._build_headers(dict(_CLIENT_HEADERS))
    finally:
        await provider.aclose()

    assert fwd["authorization"] == "Bearer moonshot-own-key"
    assert "x-api-key" not in fwd
    # Allowlisted protocol/negotiation headers pass through untouched.
    assert fwd["anthropic-version"] == "2023-06-01"
    assert fwd["anthropic-beta"] == "prompt-caching-2024-07-31"
    assert fwd["content-type"] == "application/json"
    assert fwd["accept"] == "application/json"
    assert fwd["accept-encoding"] == "gzip, deflate"
    # user-agent MUST be forwarded unmodified (Kimi ToS: no client-identifier tampering).
    assert fwd["user-agent"] == "claude-cli/2.1.196 (external, cli)"


async def test_build_headers_x_api_key_replaces_client_creds_with_own_key() -> None:
    provider = _own_key_provider("x-api-key")
    try:
        fwd = provider._build_headers(dict(_CLIENT_HEADERS))
    finally:
        await provider.aclose()

    assert fwd["x-api-key"] == "moonshot-own-key"
    assert "authorization" not in fwd
    assert fwd["anthropic-version"] == "2023-06-01"
    assert fwd["user-agent"] == "claude-cli/2.1.196 (external, cli)"


async def test_build_headers_own_key_drops_non_allowlisted_headers() -> None:
    """Own-key mode is an ALLOWLIST: cookie and any unrecognized header are dropped.

    Regression for the denylist-leak finding: a denylist over just
    authorization/x-api-key misses e.g. a cookie session token, so own-key
    mode must actively allowlist instead of merely blocking the two known
    credential headers.
    """
    provider = _own_key_provider("bearer")
    try:
        fwd = provider._build_headers(dict(_CLIENT_HEADERS))
    finally:
        await provider.aclose()

    assert "cookie" not in fwd
    assert "x-unknown-custom-header" not in fwd


@pytest.mark.parametrize("style", ["bearer", "x-api-key"])
async def test_build_headers_own_key_never_leaks_client_creds_in_either_style(style: str) -> None:
    """Neither header style may let any client credential slip through, under any name."""
    provider = _own_key_provider(style)
    try:
        fwd = provider._build_headers(dict(_CLIENT_HEADERS))
    finally:
        await provider.aclose()
    outgoing_values = set(fwd.values())
    assert _CLIENT_HEADERS["authorization"] not in outgoing_values
    assert _CLIENT_HEADERS["x-api-key"] not in outgoing_values
    assert _CLIENT_HEADERS["cookie"] not in outgoing_values


async def test_build_headers_forward_client_auth_true_forwards_headers_unchanged() -> None:
    """Regression: native-Anthropic behavior (forward_client_auth=true) must not change.

    Unlike own-key mode, this path forwards the client's full header set,
    including cookie and unrecognized headers -- it is the client's own
    vendor, so narrowing here risks breaking Claude Code features that ride
    uncommon headers.
    """
    provider = _native_provider()
    try:
        fwd = provider._build_headers(dict(_CLIENT_HEADERS))
    finally:
        await provider.aclose()

    assert fwd == _CLIENT_HEADERS
    assert fwd["authorization"] == "Bearer client-oauth-token"
    assert fwd["x-api-key"] == "client-api-key"
    assert fwd["cookie"] == "sessionKey=sk-ant-sid01-client-session-token"
    assert fwd["x-unknown-custom-header"] == "should-not-travel-to-a-third-party-vendor"


async def test_build_headers_merges_extra_headers_beyond_the_own_key_allowlist() -> None:
    """extra_headers is merged in AFTER the allowlist filter, adding vendor-required headers.

    E.g. an OpenRouter-style HTTP-Referer/X-Title pair -- not in
    _OWN_KEY_ALLOWED_HEADERS, and not derived from the client's request, so
    it survives the allowlist filter that just stripped everything else.
    """
    provider = _own_key_provider(
        "bearer", extra_headers={"HTTP-Referer": "https://example.com", "X-Title": "my-router"}
    )
    try:
        fwd = provider._build_headers(dict(_CLIENT_HEADERS))
    finally:
        await provider.aclose()

    assert fwd["HTTP-Referer"] == "https://example.com"
    assert fwd["X-Title"] == "my-router"
    # The security invariant from the earlier tests still holds alongside it.
    assert "x-api-key" not in fwd
    assert "cookie" not in fwd


async def test_build_headers_extra_headers_override_an_allowlisted_value_in_own_key_mode() -> None:
    """extra_headers wins over the client's own value for an allowlisted header.

    Confirms the merge order (auth handling first, extra_headers last):
    provider config is the operator's own explicit choice and takes
    priority over whatever the client happened to send. Uses a
    canonically-cased name ("Accept", matching routing.yaml/HTTP
    convention) against the client's lowercase "accept" -- a case-sensitive
    merge would leave BOTH keys in the result and cause httpx to send two
    conflicting raw header lines; this is the regression case for that bug.
    """
    provider = _own_key_provider(
        "bearer", extra_headers={"Accept": "application/vnd.custom+json"}
    )
    try:
        fwd = provider._build_headers(dict(_CLIENT_HEADERS))
    finally:
        await provider.aclose()

    assert fwd == {
        "authorization": "Bearer moonshot-own-key",
        "anthropic-version": "2023-06-01",
        "anthropic-beta": "prompt-caching-2024-07-31",
        "content-type": "application/json",
        "Accept": "application/vnd.custom+json",
        "accept-encoding": "gzip, deflate",
        "user-agent": "claude-cli/2.1.196 (external, cli)",
    }
    assert "accept" not in fwd


def test_init_rejects_api_key_given_when_forward_client_auth_is_true() -> None:
    """The (api_key is None) == cfg.forward_client_auth invariant is enforced at construction.

    Documented in three places (schema validator, factory.py, this
    constructor's own docstring) but, before this test, never actually
    checked -- a mismatched call site would build successfully and only
    misbehave at request time.
    """
    cfg = ProviderCfg(
        type="passthrough",
        base_url="https://api.anthropic.com",
        forward_client_auth=True,
    )
    with pytest.raises(ValueError, match="api_key must be provided if and only if"):
        PassthroughProvider(
            name="anthropic",
            cfg=cfg,
            upstream=UpstreamSettings(),
            api_key=SecretStr("should-not-be-provided-here"),
            ca_bundle_path=None,
        )


def test_init_rejects_missing_api_key_when_forward_client_auth_is_false() -> None:
    """The same invariant, the other way around: own-key mode without a key."""
    cfg = ProviderCfg(
        type="passthrough",
        base_url="https://api.moonshot.ai/anthropic",
        forward_client_auth=False,
        api_key_env="MOONSHOT_API_KEY",
    )
    with pytest.raises(ValueError, match="api_key must be provided if and only if"):
        PassthroughProvider(
            name="moonshot",
            cfg=cfg,
            upstream=UpstreamSettings(),
            api_key=None,
            ca_bundle_path=None,
        )
