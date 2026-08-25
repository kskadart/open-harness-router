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

import ssl
from pathlib import Path

import httpx

from settings import UpstreamSettings

# Binding the outgoing socket to this address restricts the connection to IPv4.
_IPV4_ANY = "0.0.0.0"


def build_upstream_verify(
    ca_bundle_path: Path | None, tls_verify_hostname: bool
) -> ssl.SSLContext | str | bool:
    """Build the ``verify`` value for a provider's outbound TLS.

    Exists for internal dev gateways whose leaf certificate does not cover
    their FQDN (``tls_verify_hostname=false`` in ``routing.yaml``): only the
    hostname match against the leaf certificate is skipped, the certificate
    chain is still FULLY verified against the bundle (or the system roots).

    Args:
        ca_bundle_path: path to a CA bundle, or ``None`` for the system's
            trusted roots.
        tls_verify_hostname: when false, hostname verification is disabled
            while ``verify_mode`` stays ``CERT_REQUIRED``.

    Returns:
        A bundle path / ``True`` for full default verification, or an
        explicit ``ssl.SSLContext`` when hostname verification is off.
    """
    if tls_verify_hostname:
        return str(ca_bundle_path) if ca_bundle_path else True
    context = ssl.create_default_context(
        cafile=str(ca_bundle_path) if ca_bundle_path else None
    )
    # CERT_REQUIRED is re-asserted explicitly (create_default_context already
    # sets it) so a future edit cannot silently downgrade this context to
    # CERT_NONE -- chain verification must survive check_hostname=False.
    context.check_hostname = False
    context.verify_mode = ssl.CERT_REQUIRED
    return context


def build_upstream_transport(
    upstream: UpstreamSettings, verify: ssl.SSLContext | str | bool = True
) -> httpx.AsyncHTTPTransport:
    """Build the provider's outgoing connection transport.

    Args:
        upstream: outgoing connection settings (IPv4 binding, pool limits).
        verify: upstream certificate verification -- a path to a CA bundle,
            ``True`` for the system's trusted roots, or a prebuilt
            ``ssl.SSLContext`` (see ``build_upstream_verify``).

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
