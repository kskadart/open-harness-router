"""Tests for building the outbound connection transport (``services.http_transport``)."""

from __future__ import annotations

import ssl
from pathlib import Path

from services.http_transport import build_upstream_transport, build_upstream_verify
from settings import UpstreamSettings

_TEST_CA = Path(__file__).resolve().parents[1] / "fixtures" / "certs" / "test_ca.pem"


def _pool(transport: object) -> object:
    """Extract the connection pool from an httpx transport.

    Args:
        transport: the built ``httpx.AsyncHTTPTransport``.

    Returns:
        The internal httpcore connection pool.
    """
    return transport._pool  # type: ignore[attr-defined]


def test_force_ipv4_binds_outgoing_socket_to_ipv4() -> None:
    """With force_ipv4 enabled, the socket is bound to an IPv4 address.

    The binding leaves the connection with only IPv4, which rules out
    attempts to reach a dead IPv6 route that hang until timeout.
    """
    transport = build_upstream_transport(UpstreamSettings(force_ipv4=True))

    assert _pool(transport)._local_address == "0.0.0.0"  # type: ignore[attr-defined]


def test_without_force_ipv4_address_family_is_left_to_the_resolver() -> None:
    """With force_ipv4 disabled, there is no binding -- the resolver decides address order."""
    transport = build_upstream_transport(UpstreamSettings(force_ipv4=False))

    assert _pool(transport)._local_address is None  # type: ignore[attr-defined]


def test_pool_limits_come_from_settings() -> None:
    """Pool limits come from settings, not from httpx's defaults."""
    upstream = UpstreamSettings(
        max_connections=42, max_keepalive_connections=7, keepalive_expiry_s=12.5
    )

    pool = _pool(build_upstream_transport(upstream))

    assert pool._max_connections == 42  # type: ignore[attr-defined]
    assert pool._max_keepalive_connections == 7  # type: ignore[attr-defined]
    assert pool._keepalive_expiry == 12.5  # type: ignore[attr-defined]


def test_build_upstream_verify_default_without_bundle_is_true() -> None:
    """tls_verify_hostname=true without a bundle -> True (system trust roots)."""
    assert build_upstream_verify(None, tls_verify_hostname=True) is True


def test_build_upstream_verify_default_with_bundle_is_the_bundle_path() -> None:
    """tls_verify_hostname=true with a bundle -> the bundle path (pre-existing behavior)."""
    assert build_upstream_verify(_TEST_CA, tls_verify_hostname=True) == str(_TEST_CA)


def test_build_upstream_verify_hostname_off_keeps_chain_verification() -> None:
    """tls_verify_hostname=false skips ONLY the hostname match, never the chain.

    check_hostname is off while verify_mode stays CERT_REQUIRED -- the whole
    point of the option is that it must never degrade into verify=False.
    """
    context = build_upstream_verify(None, tls_verify_hostname=False)

    assert isinstance(context, ssl.SSLContext)
    assert context.check_hostname is False
    assert context.verify_mode == ssl.CERT_REQUIRED


def test_build_upstream_verify_hostname_off_loads_the_ca_bundle() -> None:
    """tls_verify_hostname=false with a bundle verifies the chain against that bundle."""
    context = build_upstream_verify(_TEST_CA, tls_verify_hostname=False)

    assert isinstance(context, ssl.SSLContext)
    assert context.check_hostname is False
    assert context.verify_mode == ssl.CERT_REQUIRED
    ca_certs = context.get_ca_certs()
    # A single entry confirms the custom bundle replaced the system trust
    # store rather than merely being consulted alongside it.
    assert len(ca_certs) == 1
    assert ca_certs[0]["subject"] == ((("commonName", "open-harness-router-test-ca"),),)
