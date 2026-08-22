"""Building forward-proxy TLS contexts.

The server context terminates client TLS with a leaf certificate from
``proxy.certificates``; the client context is used for connecting to the
real upstream when a request is proxied byte-for-byte.

Both functions are blocking (key generation, writing and reading a file)
and are meant to be called once at process startup, before the event loop
starts -- not from a connection handler.
"""

from __future__ import annotations

import ssl
import tempfile
from pathlib import Path

from cryptography.hazmat.primitives import serialization

from proxy.certificates import CertificateAuthority

_CHAIN_FILENAME = "leaf-chain.pem"

# TLS 1.0/1.1 are disabled: clients behind the proxy (Node.js, modern curl
# builds) do not use them, and allowing older versions only widens the
# attack surface on a local port.
_MINIMUM_TLS_VERSION = ssl.TLSVersion.TLSv1_2

# The single ALPN protocol the proxy negotiates with the client. Without
# this restriction the client would choose HTTP/2 and send binary frames,
# while the router's parser (h11) only understands HTTP/1.x.
_ALPN_PROTOCOLS = ["http/1.1"]


def build_leaf_tls_context(
    authority: CertificateAuthority, hostname: str
) -> ssl.SSLContext:
    """Build a server TLS context for MITM termination of the given host.

    Args:
        authority: root CA holder that issues the leaf certificate.
        hostname: hostname TLS is being terminated for.

    Returns:
        Context with the certificate chain loaded and ALPN restricted to
        ``http/1.1``.
    """
    certificate, private_key = authority.leaf_certificate_for_host(hostname)
    chain_pem = certificate.public_bytes(serialization.Encoding.PEM) + private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )

    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = _MINIMUM_TLS_VERSION
    context.set_alpn_protocols(_ALPN_PROTOCOLS)

    # ``load_cert_chain`` can only read a file, and the leaf key exists only
    # in process memory. The temp directory is created with 0700
    # permissions, so the key is inaccessible to other users, and it is
    # removed right after the chain is loaded into the context.
    with tempfile.TemporaryDirectory() as tmp_dir:
        chain_path = Path(tmp_dir) / _CHAIN_FILENAME
        chain_path.write_bytes(chain_pem)
        context.load_cert_chain(chain_path)
    return context


def build_upstream_tls_context(ca_bundle: Path | None = None) -> ssl.SSLContext:
    """Build a client TLS context for connecting to the real upstream.

    Chain and hostname verification stays enabled: the proxy decrypts
    traffic for the client, but it must still speak trusted TLS to the
    upstream itself.

    Args:
        ca_bundle: PEM bundle of trusted roots for the outbound side.
            Needed when a corporate proxy with TLS inspection sits between
            the router and the internet: the upstream's certificate is
            signed by a corporate CA that is not in the system trust
            store. None -- use the system roots.

    Returns:
        Client context with ALPN restricted to ``http/1.1``.
    """
    context = ssl.create_default_context(cafile=str(ca_bundle) if ca_bundle else None)
    context.set_alpn_protocols(_ALPN_PROTOCOLS)
    return context
