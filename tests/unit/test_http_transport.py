"""Tests for building the outbound connection transport (``services.http_transport``)."""

from __future__ import annotations

from services.http_transport import build_upstream_transport
from settings import UpstreamSettings


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
