"""HTTP header filtering utilities for proxying.

For passthrough, hop-by-hop headers are dropped (httpx sets them again
itself), but authorization headers and the Anthropic protocol version are
kept.
"""

from __future__ import annotations

from collections.abc import Mapping

# Headers that must not be proxied verbatim: httpx sets them itself.
_HOP_BY_HOP = frozenset(
    {
        "host",
        "content-length",
        "connection",
        "keep-alive",
        "transfer-encoding",
        "upgrade",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailer",
    }
)

# Headers that must always be kept when proxying to Anthropic.
_PRESERVE = frozenset(
    {
        "authorization",
        "x-api-key",
        "anthropic-version",
        "anthropic-beta",
        "content-type",
        "accept",
    }
)


def forward_headers(client_headers: Mapping[str, str]) -> dict[str, str]:
    """Filter client headers for sending to the upstream.

    Args:
        client_headers: headers of the incoming request.

    Returns:
        Headers for the upstream with hop-by-hop removed, keeping
        authorization and Anthropic protocol headers.
    """
    result: dict[str, str] = {}
    for name, value in client_headers.items():
        lower = name.lower()
        if lower in _HOP_BY_HOP:
            continue
        result[name] = value
    return result


def response_headers(upstream_headers: Mapping[str, str]) -> dict[str, str]:
    """Filter the upstream response headers for the client.

    Args:
        upstream_headers: headers of the upstream response.

    Returns:
        Headers with hop-by-hop removed (content-length/transfer-encoding
        are set by the ASGI server), keeping content-type and
        content-encoding.
    """
    result: dict[str, str] = {}
    for name, value in upstream_headers.items():
        if name.lower() in _HOP_BY_HOP:
            continue
        result[name] = value
    return result


__all__ = ["forward_headers", "response_headers", "_PRESERVE"]
