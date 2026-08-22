"""Building the httpx transport for outgoing connections to upstreams.

Both providers reach out through their own transport rather than httpx's
defaults: only that way can IPv4 binding and connection pool limits be set.
Settings come from :class:`settings.UpstreamSettings`.

Important: with an explicit transport, ``httpx.AsyncClient`` ignores the
``verify`` and ``trust_env`` passed to it -- they only apply to a transport
the client builds itself. So both parameters are set here, otherwise the
provider would silently lose the upstream's CA bundle and the guard against
a forward-proxy loop.
"""

from __future__ import annotations

import httpx

from settings import UpstreamSettings

# Binding the outgoing socket to this address restricts the connection to IPv4.
_IPV4_ANY = "0.0.0.0"


def build_upstream_transport(
    upstream: UpstreamSettings, verify: str | bool = True
) -> httpx.AsyncHTTPTransport:
    """Build the provider's outgoing connection transport.

    Args:
        upstream: outgoing connection settings (IPv4 binding, pool limits).
        verify: upstream certificate verification -- a path to a CA bundle
            or ``True`` for the system's trusted roots.

    Returns:
        A transport with the configured pool limits; bound to IPv4 when
        ``force_ipv4`` is enabled.
    """
    limits = httpx.Limits(
        max_connections=upstream.max_connections,
        max_keepalive_connections=upstream.max_keepalive_connections,
        keepalive_expiry=upstream.keepalive_expiry_s,
    )
    return httpx.AsyncHTTPTransport(
        verify=verify,
        limits=limits,
        # trust_env=False: otherwise httpx reads HTTPS_PROXY/HTTP_PROXY from
        # the environment. In forward-proxy mode the router itself listens
        # as a proxy, and the client sets HTTPS_PROXY to its port -- the
        # router's outgoing requests would loop back into itself.
        trust_env=False,
        local_address=_IPV4_ANY if upstream.force_ipv4 else None,
    )
