"""Unit tests for settings parsing: ProxySettings.enabled, UpstreamSettings backoff."""

from __future__ import annotations

import pytest

from settings import ProxySettings, UpstreamSettings


def test_proxy_enabled_defaults_to_false(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without the environment variable, forward-proxy is disabled -- prior behavior."""
    monkeypatch.delenv("ROUTER_PROXY_ENABLED", raising=False)
    assert ProxySettings().enabled is False


@pytest.mark.parametrize("raw_value", ["true", "1", "yes", "True"])
def test_proxy_enabled_reads_truthy_values_from_env(
    monkeypatch: pytest.MonkeyPatch, raw_value: str
) -> None:
    """``ROUTER_PROXY_ENABLED`` enables forward-proxy."""
    monkeypatch.setenv("ROUTER_PROXY_ENABLED", raw_value)
    assert ProxySettings().enabled is True


def test_proxy_enabled_reads_falsy_value_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """An explicit ``false`` is no different from the variable being absent."""
    monkeypatch.setenv("ROUTER_PROXY_ENABLED", "false")
    assert ProxySettings().enabled is False


def test_upstream_retry_backoff_parses_comma_separated_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ROUTER_UPSTREAM_RETRY_BACKOFF_S accepts the plain comma-separated form."""
    monkeypatch.setenv("ROUTER_UPSTREAM_RETRY_BACKOFF_S", "0.5, 2,3")
    assert UpstreamSettings().retry_backoff_s == [0.5, 2.0, 3.0]


def test_upstream_retry_backoff_empty_env_disables_retries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An empty ROUTER_UPSTREAM_RETRY_BACKOFF_S turns retries off."""
    monkeypatch.setenv("ROUTER_UPSTREAM_RETRY_BACKOFF_S", "")
    assert UpstreamSettings().retry_backoff_s == []


def test_upstream_retry_backoff_defaults_without_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without the variable the default pause ladder applies."""
    monkeypatch.delenv("ROUTER_UPSTREAM_RETRY_BACKOFF_S", raising=False)
    assert UpstreamSettings().retry_backoff_s == [1, 2, 3, 5, 8]
