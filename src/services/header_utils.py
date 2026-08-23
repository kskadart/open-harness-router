"""HTTP header filtering utilities for proxying.

For passthrough, hop-by-hop headers are dropped (httpx sets them again
itself), but authorization headers and the Anthropic protocol version are
kept -- except when the provider injects its own key (see
``own_key_headers`` below), where an allowlist is used instead: the request
goes to a vendor the client did not choose, so nothing beyond documented
protocol/negotiation headers and the provider's own key travels there --
not the client's own credentials, and not any OTHER credential the client
happens to carry either (a denylist over just authorization/x-api-key would
miss e.g. a session cookie).
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping

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

# Allowlist for forward_client_auth: false (own-key mode). Everything else
# from the client -- including any credential that isn't authorization or
# x-api-key (e.g. a cookie session token), which a denylist would miss -- is
# dropped, not just the two known auth headers:
#   - anthropic-version / anthropic-beta: Anthropic protocol headers the
#     upstream needs to answer requests in Anthropic wire format.
#   - content-type / accept: content negotiation, needed to parse the
#     request body and shape the response.
#   - accept-encoding: the streaming path forwards the upstream's
#     content-encoding to the client unmodified (see
#     providers/passthrough.py _proxy_stream), so the upstream must be told
#     which encodings the client can actually decode.
#   - user-agent: MUST be forwarded, not rewritten or dropped. Kimi's terms
#     of service treat client-identifier tampering as grounds for
#     suspending the membership, and this proxy is legitimately fronting a
#     real Claude Code client.
_OWN_KEY_ALLOWED_HEADERS = frozenset(
    {
        "anthropic-version",
        "anthropic-beta",
        "content-type",
        "accept",
        "accept-encoding",
        "user-agent",
    }
)


def _collect_headers(
    items: Iterable[tuple[str, str]], keep: Callable[[str], bool]
) -> dict[str, str]:
    """Build a header dict from raw (name, value) pairs, joining duplicate lines.

    HTTP allows a single header field to be sent as several field lines with
    the same name (RFC 9110 SS5.3); Starlette's ``Headers.items()`` (what
    FastAPI hands the router for the incoming request) yields one tuple PER
    RAW LINE rather than merging them first. Building a plain dict directly
    from ``.items()`` -- ``{name: value for name, value in items}`` -- keeps
    only the LAST line for a repeated name and silently drops the earlier
    ones. Per RFC 9110, combining field lines with the same name is
    equivalent to joining their values with a comma, so that is what this
    does instead. Matching is case-insensitive (header names aren't
    case-sensitive); the casing of the FIRST occurrence of a name is kept
    for the resulting key.

    Args:
        items: raw (name, value) pairs, e.g. from ``client_headers.items()``.
        keep: called with the lowercased name; the header line is kept only
            if this returns True.

    Returns:
        A dict with duplicate header lines for the same name joined by ", ".
    """
    outgoing_headers: dict[str, str] = {}
    canonical_name_by_lower: dict[str, str] = {}
    for name, value in items:
        lower = name.lower()
        if not keep(lower):
            continue
        if lower in canonical_name_by_lower:
            canonical_name = canonical_name_by_lower[lower]
            outgoing_headers[canonical_name] = f"{outgoing_headers[canonical_name]}, {value}"
        else:
            canonical_name_by_lower[lower] = name
            outgoing_headers[name] = value
    return outgoing_headers


def forward_headers(client_headers: Mapping[str, str]) -> dict[str, str]:
    """Filter client headers for sending to the upstream.

    Used for passthrough providers with ``forward_client_auth: true``: the
    request goes to the client's own vendor (native Anthropic), so it is
    legitimate to proxy the client's full request, including headers this
    router doesn't know about -- narrowing this path risks breaking Claude
    Code features that ride uncommon headers.

    Args:
        client_headers: headers of the incoming request.

    Returns:
        Headers for the upstream with hop-by-hop removed, keeping
        authorization and Anthropic protocol headers. Repeated header lines
        with the same name are joined with a comma (RFC 9110), not
        collapsed to the last one.
    """
    return _collect_headers(client_headers.items(), lambda lower: lower not in _HOP_BY_HOP)


def own_key_headers(
    client_headers: Mapping[str, str], header_name: str, value: str
) -> dict[str, str]:
    """Build outgoing headers for a provider injecting its own key.

    Used for passthrough providers with ``forward_client_auth: false``.
    Operates directly on the RAW client headers (not on ``forward_headers``'
    output): only ``_OWN_KEY_ALLOWED_HEADERS`` survive, so no client
    credential -- known or not -- reaches a third-party upstream the client
    did not choose. The provider's own key is then set under exactly one
    header name.

    Args:
        client_headers: the raw incoming request headers.
        header_name: the header to inject the provider's own key into
            (``authorization`` or ``x-api-key``).
        value: the header value, e.g. ``Bearer sk-...`` or the raw key.

    Returns:
        A new dict containing only the allowlisted headers (repeated lines
        for the same name joined with a comma, per RFC 9110) plus the
        provider's own auth header.
    """
    outgoing_headers = _collect_headers(
        client_headers.items(), lambda lower: lower in _OWN_KEY_ALLOWED_HEADERS
    )
    outgoing_headers[header_name] = value
    return outgoing_headers


def merge_extra_headers(
    headers: Mapping[str, str], extra_headers: Mapping[str, str]
) -> dict[str, str]:
    """Merge provider-configured extra headers into an outgoing header set.

    Case-insensitive: client headers arrive already lowercased (Starlette
    parses the raw request with lowercased names), while ``routing.yaml``
    naturally uses canonical casing (e.g. ``User-Agent``, matching HTTP
    convention). A case-sensitive ``dict.update`` would leave BOTH
    ``user-agent`` and ``User-Agent`` in the result, and httpx would then
    send two separate, conflicting raw header lines to the upstream instead
    of one. Matching case-insensitively first and dropping whichever casing
    ``headers`` happens to hold makes ``extra_headers`` override outright,
    not merely supplement.

    Args:
        headers: outgoing headers built so far (from ``forward_headers`` or
            ``own_key_headers``).
        extra_headers: provider config from ``routing.yaml``
            (``cfg.extra_headers``).

    Returns:
        A new dict: ``headers`` minus any keys colliding case-insensitively
        with ``extra_headers``, plus ``extra_headers`` itself.
    """
    if not extra_headers:
        return dict(headers)
    extra_names_lower = {name.lower() for name in extra_headers}
    outgoing_headers = {
        name: value for name, value in headers.items() if name.lower() not in extra_names_lower
    }
    outgoing_headers.update(extra_headers)
    return outgoing_headers


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


__all__ = ["forward_headers", "merge_extra_headers", "own_key_headers", "response_headers"]
