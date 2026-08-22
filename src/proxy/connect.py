"""Parsing the ``CONNECT`` request on the forward-proxy port.

A client configured via ``HTTPS_PROXY`` opens the dialog with a
``CONNECT host:port HTTP/1.1`` line and a set of headers terminated by an
empty line. Such a request has no body or continuation, so a full HTTP
state machine is unnecessary here -- it is enough to read the prefix up to
the empty line and parse it. Parsing of the already-decrypted HTTP traffic
is done with h11 (see ``proxy.session``).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

# Default port when the client sent a target without an explicit port.
# CONNECT is used almost exclusively for HTTPS, and RFC 9110 only allows
# omitting the port for the default scheme.
_DEFAULT_PORT = 443

_MIN_PORT = 1
_MAX_PORT = 65535

_CONNECT_METHOD = "CONNECT"
_REQUEST_LINE_PARTS = 3
_HEAD_TERMINATOR = b"\r\n\r\n"
_LINE_SEPARATOR = "\r\n"

# CONNECT headers arrive as ASCII, but per RFC 9110 values are read as
# latin-1: decoding must not fail on a non-standard byte, or the connection
# would be dropped over an insignificant header.
_HEADER_ENCODING = "latin-1"


class ConnectRequestError(Exception):
    """Malformed request on the proxy port: not CONNECT, or a broken target."""


@dataclass(frozen=True, slots=True)
class ConnectRequest:
    """Parsed ``CONNECT`` request.

    Attributes:
        host: hostname or IP literal of the tunnel target (without square
            brackets for IPv6).
        port: tunnel target port.
        headers: request headers with lowercased names.
    """

    host: str
    port: int
    headers: dict[str, str] = field(default_factory=dict)


def _split_authority(authority: str) -> tuple[str, int]:
    """Parse a CONNECT target of the form ``host:port`` into host and port.

    Args:
        authority: target from the request line; an IPv6 literal is
            expected in square brackets (``[::1]:443``), the port may be
            absent.

    Returns:
        Tuple of (host, port).

    Raises:
        ConnectRequestError: if the host is empty or the port is not a
            number within the valid range.
    """
    if authority.startswith("["):
        closing = authority.find("]")
        if closing == -1:
            raise ConnectRequestError("unterminated IPv6 literal in CONNECT target")
        host = authority[1:closing]
        remainder = authority[closing + 1 :]
        if remainder and not remainder.startswith(":"):
            raise ConnectRequestError("unexpected characters after IPv6 literal")
        port_part = remainder[1:]
    else:
        host, _, port_part = authority.partition(":")

    if not host:
        raise ConnectRequestError("empty host in CONNECT target")
    if not port_part:
        return host, _DEFAULT_PORT
    if not port_part.isdigit():
        raise ConnectRequestError("non-numeric port in CONNECT target")

    port = int(port_part)
    if not _MIN_PORT <= port <= _MAX_PORT:
        raise ConnectRequestError("port out of range in CONNECT target")
    return host, port


def normalize_host(host: str) -> str:
    """Normalize a hostname for comparison against the allowlist.

    A trailing dot in a DNS name (``api.anthropic.com.``) denotes an
    absolute record and is permitted in ``CONNECT``. Compared literally,
    such a host would not match the allowlist and would go through a blind
    tunnel instead of routing -- silently, with no trace in the logs.

    Args:
        host: hostname from the request or from settings.

    Returns:
        Lowercased name without trailing dots.
    """
    return host.lower().rstrip(".")


def parse_connect_request(head: bytes) -> ConnectRequest:
    """Parse the ``CONNECT`` request prefix up to and including the empty line.

    Args:
        head: raw request bytes, ending with ``\\r\\n\\r\\n``.

    Returns:
        Parsed request with the tunnel target and headers.

    Raises:
        ConnectRequestError: if the request line does not match the
            ``CONNECT host:port HTTP/x.y`` format, or the target does not
            parse.
    """
    lines = head.decode(_HEADER_ENCODING).split(_LINE_SEPARATOR)
    parts = lines[0].split(" ")
    if len(parts) != _REQUEST_LINE_PARTS:
        raise ConnectRequestError("malformed request line on the proxy port")

    method, authority, version = parts
    if method != _CONNECT_METHOD:
        raise ConnectRequestError(f"unsupported method on the proxy port: {method}")
    if not version.startswith("HTTP/"):
        raise ConnectRequestError("malformed protocol version in request line")

    host, port = _split_authority(authority)

    headers: dict[str, str] = {}
    for line in lines[1:]:
        if not line:
            break
        name, separator, value = line.partition(":")
        if not separator:
            raise ConnectRequestError("malformed header line in CONNECT request")
        headers[name.strip().lower()] = value.strip()

    return ConnectRequest(host=normalize_host(host), port=port, headers=headers)


async def read_connect_request(reader: asyncio.StreamReader, timeout_s: float) -> ConnectRequest:
    """Read a ``CONNECT`` request from the stream and parse it.

    Without a timeout, a client that opened a TCP connection and never sent
    a single byte would hold the handler task and socket forever -- a
    trivial slow-loris against the proxy port.

    Args:
        reader: read stream from the proxy client.
        timeout_s: maximum time to wait for the whole request prefix.

    Returns:
        Parsed ``CONNECT`` request.

    Raises:
        ConnectRequestError: if the connection closed before the end of the
            prefix, the prefix exceeded the stream buffer limit, the
            request does not parse, or the prefix did not arrive in time.
    """
    try:
        head = await asyncio.wait_for(reader.readuntil(_HEAD_TERMINATOR), timeout=timeout_s)
    except TimeoutError as exc:
        raise ConnectRequestError("client did not send the CONNECT request in time") from exc
    except asyncio.IncompleteReadError as exc:
        raise ConnectRequestError("connection closed before the request head ended") from exc
    except asyncio.LimitOverrunError as exc:
        raise ConnectRequestError("request head exceeds the stream buffer limit") from exc
    return parse_connect_request(head)
