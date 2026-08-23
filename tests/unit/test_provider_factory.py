"""Unit tests for ``providers.factory.build_provider`` (passthrough key/CA resolution).

Before this fix, ``build_provider`` returned a ``PassthroughProvider``
before resolving any key or CA bundle at all: a missing environment
variable for an own-key passthrough provider went unnoticed until the
first request, and ``ca_bundle`` on a passthrough provider was silently
ignored (the transport always verified against the system trust roots).
These tests cover the startup-time ``ConfigError`` for both, plus the CA
bundle actually reaching the provider's transport.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from errors import ConfigError
from providers.factory import build_provider
from providers.passthrough import PassthroughProvider
from routing.schema import ProviderCfg
from settings import Settings

_FIXTURES_CERTS_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "certs"


def _own_key_cfg(ca_bundle: str | None = None) -> ProviderCfg:
    return ProviderCfg(
        type="passthrough",
        base_url="https://api.moonshot.ai/anthropic",
        forward_client_auth=False,
        api_key_env="MOONSHOT_API_KEY",
        auth_header="bearer",
        ca_bundle=ca_bundle,
    )


@pytest.fixture
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Minimal environment for constructing Settings (config_path is required).

    Also sets ROUTER_CERTS_DIR to the test fixtures' certs/ directory, so
    ca_bundle values resolve against tests/fixtures/certs/ rather than the
    project's real (gitignored) certs/.
    """
    monkeypatch.setenv("ROUTER_CONFIG_PATH", "unused.yaml")
    monkeypatch.setenv("ROUTER_CERTS_DIR", str(_FIXTURES_CERTS_DIR))


def test_build_provider_raises_config_error_when_own_key_env_var_missing(
    monkeypatch: pytest.MonkeyPatch, _settings_env: None
) -> None:
    """forward_client_auth=false with an unset env var fails at startup, not request time."""
    monkeypatch.delenv("MOONSHOT_API_KEY", raising=False)
    settings = Settings()

    with pytest.raises(ConfigError, match="env 'MOONSHOT_API_KEY' with API key is not set"):
        build_provider("moonshot", _own_key_cfg(), settings)


async def test_build_provider_resolves_own_key_when_env_var_set(
    monkeypatch: pytest.MonkeyPatch, _settings_env: None
) -> None:
    """With the env var set, the provider builds and carries the resolved key."""
    monkeypatch.setenv("MOONSHOT_API_KEY", "moonshot-secret")
    settings = Settings()

    provider = build_provider("moonshot", _own_key_cfg(), settings)
    try:
        assert isinstance(provider, PassthroughProvider)
        assert provider._api_key is not None
        assert provider._api_key.get_secret_value() == "moonshot-secret"
    finally:
        await provider.aclose()


async def test_build_provider_native_passthrough_needs_no_key(_settings_env: None) -> None:
    """forward_client_auth=true builds without touching any environment variable."""
    settings = Settings()
    cfg = ProviderCfg(
        type="passthrough",
        base_url="https://api.anthropic.com",
        forward_client_auth=True,
    )

    provider = build_provider("anthropic", cfg, settings)
    try:
        assert isinstance(provider, PassthroughProvider)
        assert provider._api_key is None
    finally:
        await provider.aclose()


def test_build_provider_passthrough_ca_bundle_missing_raises_config_error(
    monkeypatch: pytest.MonkeyPatch, _settings_env: None
) -> None:
    """A ca_bundle naming a nonexistent file fails at startup on passthrough too.

    Matches the pre-existing openai-translate behavior: previously
    build_provider returned a PassthroughProvider before this check ran at
    all, so a typo'd ca_bundle went unnoticed until every request failed
    TLS with an opaque 502.
    """
    monkeypatch.setenv("MOONSHOT_API_KEY", "moonshot-secret")
    settings = Settings()
    cfg = _own_key_cfg(ca_bundle="does_not_exist.pem")

    with pytest.raises(ConfigError, match="CA bundle not found"):
        build_provider("moonshot", cfg, settings)


async def test_build_provider_passthrough_ca_bundle_is_wired_into_the_transport(
    monkeypatch: pytest.MonkeyPatch, _settings_env: None
) -> None:
    """A valid ca_bundle replaces the system trust roots for this provider's transport.

    Regression for the CA-bundle-silently-ignored finding: passthrough now
    reaches arbitrary Anthropic-compatible upstreams, including
    corporate/self-hosted ones behind a private CA -- the resolved bundle
    must actually reach ``build_upstream_transport``'s ``verify`` argument,
    not just pass startup validation.
    """
    monkeypatch.setenv("MOONSHOT_API_KEY", "moonshot-secret")
    settings = Settings()
    cfg = _own_key_cfg(ca_bundle="test_ca.pem")

    provider = build_provider("moonshot", cfg, settings)
    try:
        assert isinstance(provider, PassthroughProvider)
        ssl_context = provider._client._transport._pool._ssl_context  # type: ignore[attr-defined]
        ca_certs = ssl_context.get_ca_certs()
        # The system trust store carries far more than one root; a single
        # entry confirms the custom bundle replaced it rather than merely
        # being consulted alongside it.
        assert len(ca_certs) == 1
        assert ca_certs[0]["subject"] == (
            (("commonName", "open-harness-router-test-ca"),),
        )
    finally:
        await provider.aclose()
