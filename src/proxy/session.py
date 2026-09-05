"""HTTP/1.1 handling on top of a decrypted MITM connection.

For a host on the allowlist, the forward-proxy terminates TLS and parses
requests with the h11 state machine. From there, a request follows one of
two paths:

* ``/v1/messages`` and ``/v1/messages/count_tokens`` -- through the
  provider registry, i.e. exactly the way the ASGI router handles them,
  but without FastAPI;
* any other path -- as a transparent byte passthrough to the real
  upstream. This path is critical: feature flags, telemetry, and client
  token refresh live on it, and breaking it breaks the client itself, not
  just routing.

A special case on both paths is a request with ``Upgrade`` (or an
``upgrade`` directive in ``Connection``): h11 cannot parse the protocol
after a switch, so such a request immediately goes as a blind tunnel to
the upstream, byte-for-byte, including headers that would normally be
treated as hop-by-hop for a regular forward. Claude Code Remote Control's
transport goes over the same host as regular requests, and its protocol is
not publicly documented -- the tunnel is safe regardless of whether the
upgrade turns out to actually be needed.
"""

from __future__ import annotations

import asyncio
import json
import ssl
import time
from collections.abc import AsyncGenerator, AsyncIterator, Iterable, Mapping
from dataclasses import dataclass

import h11

from api.adapters import parse_model
from const import UPSTREAM_REQUEST_TIMEOUT_MESSAGE
from errors import UpstreamError, anthropic_error_body
from log import get_logger
from providers.base import ProviderResult
from proxy.connect import ConnectRequest
from proxy.outbound import OutboundConnector, TunnelError
from proxy.streams import pump_tunnel
from routing.registry import ProviderRegistry
from services.header_utils import response_headers

logger = get_logger(__name__)

_MESSAGES_PATH = "/v1/messages"
_COUNT_TOKENS_PATH = "/v1/messages/count_tokens"
_ROUTED_PATHS = frozenset({_MESSAGES_PATH, _COUNT_TOKENS_PATH})
_ROUTED_METHOD = b"POST"

_READ_CHUNK_BYTES = 64 * 1024
_HEADER_ENCODING = "latin-1"
_JSON_CONTENT_TYPE = "application/json"

# Headers that apply to a single hop only: they must not be forwarded
# further (RFC 9110, 7.6.1), connection liveness is managed by h11.
_HOP_BY_HOP_HEADERS: frozenset[bytes] = frozenset(
    {
        b"connection",
        b"keep-alive",
        b"proxy-authenticate",
        b"proxy-authorization",
        b"te",
        b"trailer",
        b"upgrade",
    }
)

# Body-framing headers. For the response they are rebuilt from scratch: h11
# hands back the body already decoded from chunked, so the upstream's
# original framing does not apply to the outgoing response.
_FRAMING_HEADERS: frozenset[bytes] = frozenset({b"content-length", b"transfer-encoding"})

# Statuses that have no body by definition; h11 frames them with zero
# length regardless of headers, so chunked must not be added to them.
_BODYLESS_STATUSES: frozenset[int] = frozenset({204, 304})
_HEAD_METHOD = b"HEAD"

_BAD_REQUEST_STATUS = 400
_BAD_GATEWAY_STATUS = 502
_INTERNAL_ERROR_STATUS = 500

# Synthetic status for the protocol-switch log entry: the response itself
# is produced by the upstream inside the already-blind tunnel, the proxy
# does not parse it and never sees a code. 101 is the standard Switching
# Protocols code, the closest semantic match to what actually happened.
_SWITCHED_PROTOCOLS_STATUS = 101

_INTERNAL_ERROR_MESSAGE = "Internal router error"
_MALFORMED_REQUEST_MESSAGE = "Malformed HTTP request"
_UPSTREAM_UNREACHABLE_MESSAGE = "Upstream is unreachable. Retry the request."
_CLIENT_TIMEOUT_MESSAGE = "Client did not send a complete request in time."

_REQUEST_TIMEOUT_STATUS = 408
_GATEWAY_TIMEOUT_STATUS = 504


class ProxyProtocolError(Exception):
    """Unexpected sequence of HTTP events on the proxied connection."""


class ProxyTimeoutError(Exception):
    """A read (from the client, from the upstream, or in the tunnel) timed out.

    Does not inherit from ``ProxyProtocolError``: that branch's handler
    replies to the client with ``400`` as an HTTP syntax violation, which is
    semantically wrong for a timeout -- both cases get separate handling
    (see ``_read_request`` and ``_dispatch``).
    """


@dataclass(frozen=True, slots=True)
class SessionTimeouts:
    """Read timeouts on the decrypted MITM connection.

    Attributes:
        client_request_s: how long to wait for the next read from the
            client -- request headers and body, including idle time in
            keep-alive before the next request on the same connection
            (this is the same read: there is no separate idle setting).
        upstream_headers_s: how long to wait for the upstream response's
            status and headers -- separate from the body, so a long
            streaming response is not cut off by this timeout.
        idle_s: maximum silence between chunks of an already-started
            upstream response body, or of the tunnel after a protocol
            switch.
    """

    client_request_s: float
    upstream_headers_s: float
    idle_s: float


class SocketClientChannel:
    """``ClientChannel`` implementation over a pair of raw socket streams.

    The provider needs exactly one operation -- knowing whether the client
    has left, in order to cooperatively abort the stream. On a raw socket
    there are two signs of that: the transport is already closing, or the
    client sent EOF.
    """

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        """Bind the channel to the client connection's streams.

        Args:
            reader: read stream from the client.
            writer: write stream to the client.
        """
        self._reader = reader
        self._writer = writer

    async def is_disconnected(self) -> bool:
        """Check whether the client has dropped the connection.

        Returns:
            True if the transport is closing or the client has closed its
            side.
        """
        return self._writer.is_closing() or self._reader.at_eof()


async def next_event(
    reader: asyncio.StreamReader, conn: h11.Connection, timeout_s: float | None = None
) -> h11.Event:
    """Get the next h11 event, reading more data from the stream if needed.

    ``timeout_s`` bounds the silence BETWEEN individual socket reads, not
    the time to receive the whole event: if an event is assembled from
    several chunks (e.g. a chunked request body), the timeout restarts on
    each subsequent read. For the default of None (used by tests that drive
    both sides of the connection directly), behavior is unchanged -- the
    read waits for data with no limit.

    Args:
        reader: read stream of the corresponding side of the connection.
        conn: the h11 state machine for that side.
        timeout_s: maximum silence before the next socket read; None -- no
            limit.

    Returns:
        The next protocol event.

    Raises:
        ProxyProtocolError: if the connection switched to another protocol
            (``PAUSED``) -- the proxy does not support that.
        ProxyTimeoutError: if the next read did not fit within
            ``timeout_s``.
        h11.RemoteProtocolError: if the incoming bytes violate HTTP/1.1.
    """
    while True:
        event = conn.next_event()
        if isinstance(event, h11.Event):
            return event
        if event is h11.PAUSED:
            raise ProxyProtocolError("connection switched to an unsupported protocol")
        try:
            # An empty read means EOF: h11 turns it into ConnectionClosed.
            chunk = await asyncio.wait_for(reader.read(_READ_CHUNK_BYTES), timeout=timeout_s)
        except TimeoutError as exc:
            raise ProxyTimeoutError("no data received within the read timeout") from exc
        conn.receive_data(chunk)


def request_path(target: bytes) -> str:
    """Extract the path from the request target, discarding the query string.

    Trailing slashes are stripped: ``/v1/messages/`` denotes the same
    resource as ``/v1/messages``, but compared literally against the route
    table it would not match and would go to the real upstream -- meaning
    the request would be billed to the Anthropic account instead of the
    configured provider.

    Args:
        target: request target in origin-form (``/v1/messages?beta=true``).

    Returns:
        Path without the query part and without trailing slashes; ``/`` for
        the root.
    """
    path = target.split(b"?", 1)[0].decode(_HEADER_ENCODING)
    return path.rstrip("/") or "/"


def client_headers_mapping(headers: Iterable[tuple[bytes, bytes]]) -> dict[str, str]:
    """Convert h11 headers into a string mapping for the provider.

    Args:
        headers: request headers as returned by h11 (lowercased names).

    Returns:
        Mapping of name -> value as strings.
    """
    return {
        name.decode(_HEADER_ENCODING): value.decode(_HEADER_ENCODING)
        for name, value in headers
    }


def upstream_request_headers(
    headers: Iterable[tuple[bytes, bytes]],
) -> list[tuple[bytes, bytes]]:
    """Select the request headers to send to the real upstream.

    Body framing (``content-length``/``transfer-encoding``) is kept: h11
    uses it to decide how to relay the body onward, and the body itself is
    pumped through without interpretation.

    Args:
        headers: headers of the original request from the client.

    Returns:
        List of headers with hop-by-hop ones removed.
    """
    return [(name, value) for name, value in headers if name not in _HOP_BY_HOP_HEADERS]


def response_has_body(status_code: int, method: bytes) -> bool:
    """Determine whether a body is allowed for a response with this status and method.

    Args:
        status_code: response status.
        method: method of the original request.

    Returns:
        False for ``HEAD``, ``204``, and ``304`` -- they have no body per
        the protocol.
    """
    return status_code not in _BODYLESS_STATUSES and method.upper() != _HEAD_METHOD


def framed_headers(
    headers: Iterable[tuple[bytes, bytes]],
    content_length: int | None,
    allow_body: bool,
) -> list[tuple[bytes, bytes]]:
    """Rebuild response headers with correct body framing.

    Args:
        headers: original response headers.
        content_length: known body length, or None if it is not known
            ahead of time (a stream) -- chunked is used in that case.
        allow_body: False for responses that cannot have a body; in that
            case no framing is added at all.

    Returns:
        List of headers without hop-by-hop entries and without the old
        framing, with ``content-length`` or ``transfer-encoding: chunked``
        added.
    """
    result = [
        (name, value)
        for name, value in headers
        if name not in _HOP_BY_HOP_HEADERS and name not in _FRAMING_HEADERS
    ]
    if not allow_body:
        return result
    if content_length is None:
        # The body length is not known ahead of time (an SSE stream):
        # chunked allows events to be sent as they arrive and keeps the
        # connection alive after the response.
        result.append((b"transfer-encoding", b"chunked"))
    else:
        result.append((b"content-length", str(content_length).encode("ascii")))
    return result


def _encode_headers(headers: Mapping[str, str]) -> list[tuple[bytes, bytes]]:
    """Convert a string header mapping into h11 byte pairs.

    Args:
        headers: headers as strings.

    Returns:
        List of byte pairs with lowercased names.
    """
    return [
        (name.lower().encode(_HEADER_ENCODING), value.encode(_HEADER_ENCODING))
        for name, value in headers.items()
    ]


def _header_value(headers: Iterable[tuple[bytes, bytes]], name: bytes) -> bytes | None:
    """Find a header's value by name.

    Args:
        headers: headers as returned by h11.
        name: the name being looked up, lowercased.

    Returns:
        The header's value, or None if it is absent.
    """
    for header_name, value in headers:
        if header_name == name:
            return value
    return None


def is_protocol_switch_requested(headers: Iterable[tuple[bytes, bytes]]) -> bool:
    """Determine whether the request asks to switch the protocol on this connection.

    h11 puts the client side into the ``MIGHT_SWITCH_PROTOCOL`` state right
    after parsing request headers with ``Upgrade`` or with an ``upgrade``
    directive in ``Connection`` -- the very next call to ``next_event``
    returns ``PAUSED`` instead of the body, without waiting for the
    connection to end (see h11._connection, ``_process_event`` and
    ``_extract_next_receive_event``). This must be checked BEFORE that
    call, in order to choose a blind tunnel instead of a
    ``ProxyProtocolError``.

    Args:
        headers: request headers as returned by h11.

    Returns:
        True if the request asks for a protocol switch.
    """
    if _header_value(headers, b"upgrade") is not None:
        return True
    connection = _header_value(headers, b"connection")
    if connection is None:
        return False
    tokens = {token.strip().lower() for token in connection.split(b",")}
    return b"upgrade" in tokens


def _raw_request_head(request: h11.Request, trailing_body: bytes) -> bytes:
    """Reassemble the request in raw form for an unparsed forward to the upstream.

    Unlike ``upstream_request_headers``, nothing is stripped: the upstream
    needs exactly the request the client sent, including ``Upgrade`` and
    ``Connection`` -- without them the upstream would not understand it is
    being asked to switch protocol. ``trailing_body`` is the bytes h11 has
    already read from the socket past the headers (see
    ``Connection.trailing_data`` in ``is_protocol_switch_requested``), they
    must not be dropped, or the request would reach the upstream truncated.

    Args:
        request: the request event parsed by h11.
        trailing_body: the unconsumed remainder of h11's buffer at the time
            of the call.

    Returns:
        Request bytes from the request line through the end of the data
        already read.
    """
    request_line = b"%s %s HTTP/%s\r\n" % (
        request.method,
        request.target,
        request.http_version,
    )
    header_lines = b"".join(
        b"%s: %s\r\n" % (name, value) for name, value in request.headers.raw_items()
    )
    return request_line + header_lines + b"\r\n" + trailing_body


class MitmHttpSession:
    """HTTP/1.1 session on a decrypted connection with a MITM host."""

    def __init__(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        target: ConnectRequest,
        registry: ProviderRegistry,
        connector: OutboundConnector,
        upstream_tls: ssl.SSLContext,
        timeouts: SessionTimeouts,
    ) -> None:
        """Create a session on top of an already-decrypted connection.

        Args:
            reader: read stream from the client (after TLS termination).
            writer: write stream to the client (after TLS termination).
            target: target of the original ``CONNECT`` -- also the address
                of the real upstream for non-routed paths.
            registry: provider registry for routed paths.
            connector: outbound connector (direct or via an upstream
                proxy).
            upstream_tls: client TLS context for talking to the upstream.
            timeouts: read timeouts for this connection.
        """
        self._reader = reader
        self._writer = writer
        self._target = target
        self._registry = registry
        self._connector = connector
        self._upstream_tls = upstream_tls
        self._timeouts = timeouts
        self._conn = h11.Connection(h11.SERVER)
        self._channel = SocketClientChannel(reader, writer)

    async def serve(self) -> None:
        """Serve requests on the connection while it remains fit for the next one.

        Each request is handled under its own error barrier: a failure on
        one request ends the connection, but does not propagate beyond the
        session.
        """
        while True:
            request = await self._read_request()
            if request is None:
                return

            started_at = time.perf_counter()
            path = request_path(request.target)
            status = await self._dispatch(request, path)

            logger.info(
                "proxy_request",
                host=self._target.host,
                path=path,
                status=status,
                duration_ms=round((time.perf_counter() - started_at) * 1000, 1),
            )

            if self._conn.our_state is not h11.DONE or self._conn.their_state is not h11.DONE:
                return
            self._conn.start_next_cycle()

    async def _read_request(self) -> h11.Request | None:
        """Read the start of the next request on the connection.

        The timeout here also bounds keep-alive idle time: waiting for the
        NEXT request on an already-served connection is the same read as
        finishing the current one, no separate idle timeout is needed.

        Returns:
            The request event, or None if the client closed the connection
            or sent something unparseable (in the latter case it has
            already been sent a ``400``/``408`` response).
        """
        try:
            event = await next_event(
                self._reader, self._conn, timeout_s=self._timeouts.client_request_s
            )
        except ProxyTimeoutError:
            logger.warning("proxy_client_read_timeout", host=self._target.host)
            await self._fail(
                _REQUEST_TIMEOUT_STATUS, "invalid_request_error", _CLIENT_TIMEOUT_MESSAGE
            )
            return None
        except (h11.ProtocolError, ProxyProtocolError) as exc:
            logger.warning(
                "proxy_bad_request", host=self._target.host, error_type=type(exc).__name__
            )
            await self._fail(
                _BAD_REQUEST_STATUS, "invalid_request_error", _MALFORMED_REQUEST_MESSAGE
            )
            return None
        if isinstance(event, h11.Request):
            return event
        return None

    async def _dispatch(self, request: h11.Request, path: str) -> int:
        """Handle a single request under an error barrier.

        Args:
            request: request event from the client.
            path: request path without the query part.

        Returns:
            The status sent to the client (or an error status, if the
            response had not started yet).
        """
        try:
            if is_protocol_switch_requested(request.headers):
                return await self._switch_protocol(request)
            if request.method.upper() == _ROUTED_METHOD and path in _ROUTED_PATHS:
                return await self._handle_routed(request, path)
            return await self._forward_upstream(request)
        except UpstreamError as exc:
            # ProviderError inherits from UpstreamError, so it is covered
            # by this branch.
            logger.error(
                "proxy_upstream_error",
                host=self._target.host,
                path=path,
                status=exc.status_code,
                message=exc.message,
            )
            return await self._fail(exc.status_code, exc.error_type, exc.message)
        except (
            TunnelError,
            ssl.SSLError,
            OSError,
            h11.ProtocolError,
            ProxyProtocolError,
            ProxyTimeoutError,
        ) as exc:
            # ProxyTimeoutError -- the silence could have come from either
            # side (client not finishing sending the body, upstream not
            # responding, or going quiet mid-body); status and message for
            # it are separate, but the handling is the same: _fail itself
            # figures out whether response headers already went out to the
            # client, and in that case writes nothing again -- the
            # connection is closed normally by the caller's loop barrier.
            if isinstance(exc, ProxyTimeoutError):
                logger.warning(
                    "proxy_read_timeout",
                    host=self._target.host,
                    path=path,
                    error_type=type(exc).__name__,
                )
                status_code, message = _GATEWAY_TIMEOUT_STATUS, UPSTREAM_REQUEST_TIMEOUT_MESSAGE
            else:
                logger.error(
                    "proxy_forward_failed",
                    host=self._target.host,
                    path=path,
                    error_type=type(exc).__name__,
                )
                status_code, message = _BAD_GATEWAY_STATUS, _UPSTREAM_UNREACHABLE_MESSAGE
            return await self._fail(status_code, "api_error", message)
        except Exception as exc:  # noqa: BLE001 - connection barrier, see below
            # There are no FastAPI exception handlers on a raw socket, and
            # an exception escaping would abort the connection without a
            # response. The barrier logs the details and returns a neutral
            # error to the client; the server keeps accepting new
            # connections.
            logger.exception(
                "proxy_unexpected_error", host=self._target.host, path=path, error=str(exc)
            )
            return await self._fail(
                _INTERNAL_ERROR_STATUS, "api_error", _INTERNAL_ERROR_MESSAGE
            )

    async def _switch_protocol(self, request: h11.Request) -> int:
        """Switch the connection to a blind tunnel at the client's request.

        h11 cannot parse bytes after a protocol switch (see
        ``is_protocol_switch_requested``), so the request and everything
        that follows it go on byte-for-byte to the real upstream -- the
        same path ``_forward_upstream`` uses for the connection, but
        without h11 involved on either side. Headers are not filtered
        here: the upstream needs the original request, including
        ``Upgrade`` and ``Connection``.

        Args:
            request: the request event that triggered the protocol switch.

        Returns:
            Synthetic status for the log (see
            ``_SWITCHED_PROTOCOLS_STATUS``).

        Raises:
            TunnelError: if the connection to the upstream is not
                established.
        """
        trailing_body, _closed = self._conn.trailing_data
        logger.info(
            "proxy_protocol_switch",
            host=self._target.host,
            method=request.method.decode(_HEADER_ENCODING),
            target=request.target.decode(_HEADER_ENCODING),
            upgrade=(_header_value(request.headers, b"upgrade") or b"").decode(
                _HEADER_ENCODING
            ),
        )
        upstream_reader, upstream_writer = await self._connector.open_tls(
            self._target.host, self._target.port, self._upstream_tls
        )
        upstream_writer.write(_raw_request_head(request, trailing_body))
        await upstream_writer.drain()
        await pump_tunnel(
            self._reader, self._writer, upstream_reader, upstream_writer, self._timeouts.idle_s
        )
        return _SWITCHED_PROTOCOLS_STATUS

    async def _handle_routed(self, request: h11.Request, path: str) -> int:
        """Handle a request through the provider registry.

        Args:
            request: request event from the client.
            path: ``/v1/messages`` or ``/v1/messages/count_tokens``.

        Returns:
            Status of the response sent to the client.
        """
        raw_body = await self._read_body()
        model = parse_model(raw_body)
        if not isinstance(model, str):
            # parse_model returns a ready JSONResponse; on a raw socket
            # only its status and already-rendered body are used -- ASGI
            # does not reach this far.
            return await self._send_bytes(
                model.status_code, _JSON_CONTENT_TYPE, bytes(model.body)
            )

        decision = self._registry.resolve(model)
        logger.info(
            "route",
            endpoint="count_tokens" if path == _COUNT_TOKENS_PATH else "messages",
            model=model,
            provider=decision.provider.name,
            upstream_model=decision.upstream_model,
        )

        headers = client_headers_mapping(request.headers)
        if path == _COUNT_TOKENS_PATH:
            result = await decision.provider.count_tokens(
                raw_body, headers, decision.upstream_model, decision.limits
            )
        else:
            result = await decision.provider.handle_messages(
                raw_body, headers, self._channel, decision.upstream_model, decision.limits
            )
        return await self._send_result(result)

    async def _forward_upstream(self, request: h11.Request) -> int:
        """Forward the request to the real upstream byte-for-byte.

        The request body and the response body are pumped through as a
        stream and are not parsed: only the headers are read, from which
        h11 determines the framing.

        Args:
            request: request event from the client.

        Returns:
            Status of the upstream's response.

        Raises:
            TunnelError: if the connection to the upstream is not
                established.
        """
        upstream_reader, upstream_writer = await self._connector.open_tls(
            self._target.host, self._target.port, self._upstream_tls
        )
        upstream_conn = h11.Connection(h11.CLIENT)
        try:
            await _send_to(
                upstream_writer,
                upstream_conn,
                h11.Request(
                    method=request.method,
                    target=request.target,
                    headers=upstream_request_headers(request.headers),
                    http_version=request.http_version,
                ),
            )
            await self._pump_request_body(upstream_writer, upstream_conn)
            return await self._relay_response(upstream_reader, upstream_conn, request)
        finally:
            upstream_writer.close()

    async def _pump_request_body(
        self, upstream_writer: asyncio.StreamWriter, upstream_conn: h11.Connection
    ) -> None:
        """Pump the client's request body into the upstream connection.

        Args:
            upstream_writer: write stream to the upstream.
            upstream_conn: client-side h11 state machine.

        Raises:
            ProxyProtocolError: on an unexpected event instead of the
                request body.
        """
        while True:
            event = await next_event(
                self._reader, self._conn, timeout_s=self._timeouts.client_request_s
            )
            if isinstance(event, h11.Data):
                await _send_to(upstream_writer, upstream_conn, h11.Data(data=event.data))
                continue
            if isinstance(event, h11.EndOfMessage):
                await _send_to(upstream_writer, upstream_conn, h11.EndOfMessage())
                return
            raise ProxyProtocolError("unexpected event while reading the request body")

    async def _relay_response(
        self,
        upstream_reader: asyncio.StreamReader,
        upstream_conn: h11.Connection,
        request: h11.Request,
    ) -> int:
        """Relay the upstream's response to the client, preserving status, headers, and body.

        Args:
            upstream_reader: read stream from the upstream.
            upstream_conn: client-side h11 state machine.
            request: the original request (needed to decide whether a body
                is present).

        Returns:
            Status of the upstream's response.

        Raises:
            ProxyProtocolError: if an unexpected event arrived instead of a
                response.
        """
        response = await _read_response(
            upstream_reader, upstream_conn, self._timeouts.upstream_headers_s
        )
        allow_body = response_has_body(response.status_code, request.method)
        length = _header_value(response.headers, b"content-length")
        await self._send(
            h11.Response(
                status_code=response.status_code,
                headers=framed_headers(
                    response.headers,
                    int(length) if length is not None and allow_body else None,
                    allow_body,
                ),
            )
        )
        while True:
            event = await next_event(
                upstream_reader, upstream_conn, timeout_s=self._timeouts.idle_s
            )
            if isinstance(event, h11.Data):
                await self._send(h11.Data(data=event.data))
                continue
            if isinstance(event, h11.EndOfMessage):
                await self._send(h11.EndOfMessage())
                return response.status_code
            raise ProxyProtocolError("unexpected event while reading the upstream response")

    async def _read_body(self) -> bytes:
        """Read the request body in full.

        Routed paths need the body in full: the model name lives in the
        JSON, and providers accept raw request bytes.

        Returns:
            The request body.

        Raises:
            ProxyProtocolError: on an unexpected event instead of the
                request body.
        """
        chunks: list[bytes] = []
        while True:
            event = await next_event(
                self._reader, self._conn, timeout_s=self._timeouts.client_request_s
            )
            if isinstance(event, h11.Data):
                chunks.append(bytes(event.data))
                continue
            if isinstance(event, h11.EndOfMessage):
                return b"".join(chunks)
            raise ProxyProtocolError("unexpected event while reading the request body")

    async def _send_result(self, result: ProviderResult) -> int:
        """Serialize a ``ProviderResult`` into an HTTP/1.1 response on the socket.

        Args:
            result: provider-neutral result of handling the request.

        Returns:
            Status sent to the client.
        """
        headers = _encode_headers(response_headers(result.headers))
        if isinstance(result.body, bytes):
            await self._send(
                h11.Response(
                    status_code=result.status_code,
                    headers=framed_headers(headers, len(result.body), allow_body=True),
                )
            )
            await self._send(h11.Data(data=result.body))
            await self._send(h11.EndOfMessage())
            return result.status_code

        await self._send(
            h11.Response(
                status_code=result.status_code,
                headers=framed_headers(headers, None, allow_body=True),
            )
        )
        await self._stream_body(result.body)
        await self._send(h11.EndOfMessage())
        return result.status_code

    async def _stream_body(self, body: AsyncIterator[bytes]) -> None:
        """Send a streaming body chunk by chunk as it arrives.

        Each chunk goes into the socket and is flushed separately:
        buffering the whole body would turn SSE into a regular response.

        Args:
            body: async byte stream from the provider.
        """
        try:
            async for chunk in body:
                if chunk:
                    await self._send(h11.Data(data=chunk))
        finally:
            # A client disconnect mid-stream breaks the loop with an
            # exception, and the provider's suspended generator would keep
            # the connection to the upstream open until garbage collected
            # -- close it explicitly.
            if isinstance(body, AsyncGenerator):
                await body.aclose()

    async def _send_bytes(self, status_code: int, content_type: str, body: bytes) -> int:
        """Send the client a ready-made response with a body of known length.

        Args:
            status_code: response status.
            content_type: value of the ``content-type`` header.
            body: response body.

        Returns:
            The status sent.
        """
        await self._send(
            h11.Response(
                status_code=status_code,
                headers=framed_headers(
                    [(b"content-type", content_type.encode(_HEADER_ENCODING))],
                    len(body),
                    allow_body=True,
                ),
            )
        )
        await self._send(h11.Data(data=body))
        await self._send(h11.EndOfMessage())
        return status_code

    async def _fail(self, status_code: int, error_type: str, message: str) -> int:
        """Send the client an error in Anthropic's format, if the response has not started yet.

        Args:
            status_code: HTTP status of the error.
            error_type: error type in Anthropic's format.
            message: human-readable message without internal details.

        Returns:
            The error status.
        """
        if self._conn.our_state not in (h11.IDLE, h11.SEND_RESPONSE):
            # IDLE -- before the Request was parsed (a failure before the
            # first byte of the request, including a client read timeout
            # on an empty connection) -- and also SEND_RESPONSE -- the
            # request was parsed, but response headers have not gone out
            # yet. h11 allows send(Response) from both states. Any state
            # further along (SEND_BODY etc.) means the status and headers
            # have already gone to the client -- the response cannot be
            # replayed, the connection will end abruptly (the client will
            # see it as a broken stream).
            return status_code
        body = json.dumps(anthropic_error_body(error_type, message)).encode()
        return await self._send_bytes(status_code, _JSON_CONTENT_TYPE, body)

    async def _send(self, event: h11.Event) -> None:
        """Send an h11 event to the client and flush the socket buffer.

        Args:
            event: outgoing protocol event.
        """
        await _send_to(self._writer, self._conn, event)


async def _send_to(
    writer: asyncio.StreamWriter, conn: h11.Connection, event: h11.Event
) -> None:
    """Serialize an h11 event and send it to the stream immediately.

    Args:
        writer: write stream of the corresponding side.
        conn: the h11 state machine for that side.
        event: outgoing protocol event.
    """
    data = conn.send(event)
    if data is None:
        return
    writer.write(data)
    await writer.drain()


async def _read_response(
    reader: asyncio.StreamReader, conn: h11.Connection, timeout_s: float
) -> h11.Response:
    """Wait for the upstream's final response, skipping informational ones.

    The timeout bounds waiting for the status and headers specifically --
    separately from the body timeout, which applies further on, in the
    ``Data`` read loop (see ``MitmHttpSession._relay_response``): a
    streaming response can run for minutes AFTER the headers arrived in
    time.

    Args:
        reader: read stream from the upstream.
        conn: client-side h11 state machine.
        timeout_s: maximum time to wait for the status and headers.

    Returns:
        The final response event.

    Raises:
        ProxyProtocolError: if the upstream sent something other than a
            response.
        ProxyTimeoutError: if the upstream did not send status and headers
            within the allotted time.
    """
    while True:
        event = await next_event(reader, conn, timeout_s=timeout_s)
        if isinstance(event, h11.InformationalResponse):
            continue
        if isinstance(event, h11.Response):
            return event
        raise ProxyProtocolError("upstream sent no response")
