"""Unit tests for the forward proxy's HTTP session (``proxy.session``).

The session runs on top of a pair of real loopback sockets: only this way
can we verify that a streaming body is delivered to the client in chunks as
it arrives, not after full buffering.
"""

from __future__ import annotations

import asyncio
import json
import ssl
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from pathlib import Path

import h11
import pytest

from errors import ProviderError, UpstreamError
from providers.base import ClientChannel, ProviderResult
from proxy.certificates import CertificateAuthority
from proxy.connect import ConnectRequest
from proxy.outbound import OutboundConnector
from proxy.session import (
    MitmHttpSession,
    ProxyTimeoutError,
    SessionTimeouts,
    framed_headers,
    is_protocol_switch_requested,
    next_event,
    request_path,
    response_has_body,
    upstream_request_headers,
)
from proxy.tls import build_leaf_tls_context, build_upstream_tls_context
from routing.registry import ProviderRegistry
from unit.conftest import ConnectedStreams

StreamFactory = Callable[[], Awaitable[ConnectedStreams]]

_TIMEOUT_S = 3.0
_MESSAGES_BODY = json.dumps({"model": "claude-test", "stream": False}).encode()
_DEFAULT_TIMEOUTS = SessionTimeouts(
    client_request_s=_TIMEOUT_S, upstream_headers_s=_TIMEOUT_S, idle_s=_TIMEOUT_S
)

_UPSTREAM_HOST = "localhost"
_UPGRADE_REQUEST_HEAD = (
    b"GET /connect HTTP/1.1\r\n"
    b"host: localhost\r\n"
    b"upgrade: websocket\r\n"
    b"connection: Upgrade\r\n"
    b"sec-websocket-key: dGhlIHNhbXBsZSBub25jZQ==\r\n"
    b"\r\n"
)
# Bytes that h11 manages to read into its buffer along with the headers --
# exactly what ``_switch_protocol`` must forward to upstream via
# ``Connection.trailing_data``, not lose.
_ALREADY_BUFFERED_BYTES = b"already-buffered-frame"


class StubProvider:
    """Stub provider with predefined behavior.

    Attributes:
        name: the provider's name in the registry.
        seen_headers: headers of the last request received from the session.
        seen_channel: the client channel passed to ``handle_messages``.
    """

    def __init__(
        self,
        result: ProviderResult | None = None,
        error: Exception | None = None,
    ) -> None:
        """Create a stub.

        Args:
            result: the result returned for ``/v1/messages``.
            error: the exception raised instead of a result.
        """
        self.name = "stub"
        self._result = result
        self._error = error
        self.seen_headers: Mapping[str, str] = {}
        self.seen_channel: ClientChannel | None = None

    async def handle_messages(
        self,
        raw_body: bytes,
        client_headers: Mapping[str, str],
        client_channel: ClientChannel,
        upstream_model: str | None,
    ) -> ProviderResult:
        """Return the prepared result or raise the prepared error."""
        self.seen_headers = client_headers
        self.seen_channel = client_channel
        if self._error is not None:
            raise self._error
        assert self._result is not None
        return self._result

    async def count_tokens(
        self,
        raw_body: bytes,
        client_headers: Mapping[str, str],
        upstream_model: str | None,
    ) -> ProviderResult:
        """Return a fixed token count estimate."""
        return ProviderResult(
            status_code=200,
            headers={"content-type": "application/json"},
            body=json.dumps({"input_tokens": 42}).encode(),
        )

    async def aclose(self) -> None:
        """The stub has nothing to close."""


def _registry(provider: StubProvider) -> ProviderRegistry:
    """Build a registry with a single stub provider.

    Args:
        provider: the stub provider.

    Returns:
        A registry that always resolves the route to this stub.
    """
    return ProviderRegistry({"stub": provider}, [], "stub")  # type: ignore[dict-item]


def _start_session(
    streams: ConnectedStreams,
    registry: ProviderRegistry,
    *,
    target: ConnectRequest | None = None,
    connector: OutboundConnector | None = None,
    upstream_tls: ssl.SSLContext | None = None,
    timeouts: SessionTimeouts | None = None,
) -> asyncio.Task[None]:
    """Start a session on the accepting end of the stream pair.

    Args:
        streams: a pair of connected streams; the session occupies the right side.
        registry: the provider registry.
        target: the target of the original ``CONNECT``; defaults to a stub
            not involved in the non-messages paths of most tests in this file.
        connector: the outbound connection connector; defaults to direct.
        upstream_tls: the client TLS context for upstream; defaults to the
            system roots (non-messages paths do not use it).
        timeouts: the session timeouts; defaults to a margin of several
            seconds, so they don't trigger accidentally in tests that don't
            test timeouts.

    Returns:
        The task serving the session.
    """
    session = MitmHttpSession(
        streams.right_reader,
        streams.right_writer,
        target or ConnectRequest(host="api.anthropic.com", port=443),
        registry,
        connector or OutboundConnector(None, frozenset(), _TIMEOUT_S),
        upstream_tls or ssl.create_default_context(),
        timeouts or _DEFAULT_TIMEOUTS,
    )
    return asyncio.create_task(session.serve())


class RawUpstream:
    """A local TLS server with no HTTP parsing -- gives the test raw streams.

    Unlike a full HTTP server, verifying a blind tunnel requires seeing the
    request bytes as-is: the real upstream receives exactly those bytes
    during a protocol switch, without h11 interpretation.
    """

    def __init__(self, context: ssl.SSLContext) -> None:
        """Prepare a server with the given server TLS context.

        Args:
            context: the server TLS context with the host's leaf certificate.
        """
        self._context = context
        self._server: asyncio.Server | None = None
        self._connected: asyncio.Future[
            tuple[asyncio.StreamReader, asyncio.StreamWriter]
        ] = asyncio.get_event_loop().create_future()

    async def start(self) -> int:
        """Bring up the server on a free port of the loopback interface.

        Returns:
            The port that was bound.
        """
        self._server = await asyncio.start_server(
            self._on_connected, "127.0.0.1", 0, ssl=self._context
        )
        return int(self._server.sockets[0].getsockname()[1])

    def _on_connected(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        """Save the streams of the first connection."""
        self._connected.set_result((reader, writer))

    async def accept(self) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        """Wait for the streams of the first connection.

        Returns:
            The stream pair of the accepted connection.
        """
        return await asyncio.wait_for(self._connected, timeout=_TIMEOUT_S)

    async def stop(self) -> None:
        """Stop the server and wait for the listening socket to close."""
        if self._server is None:
            return
        self._server.close()
        await self._server.wait_closed()


async def _open_switched_tunnel(
    connect_streams: StreamFactory, tmp_path: Path
) -> tuple[
    ConnectedStreams,
    asyncio.Task[None],
    RawUpstream,
    asyncio.StreamReader,
    asyncio.StreamWriter,
]:
    """Drive an upgrade request through the session and wait for upstream to receive it.

    The request headers and bytes simulating the start of the already
    switched protocol are sent in a single socket write -- exactly as h11
    manages to read them into its buffer before it recognizes the request as
    an Upgrade and stops before the body.

    Args:
        connect_streams: a factory of connected client-session streams.
        tmp_path: the test's temporary directory for the root CA.

    Returns:
        A tuple of (client streams, session task, fake upstream, upstream
        read stream, upstream write stream).
    """
    ca_dir = tmp_path / "ca"
    server_context = build_leaf_tls_context(CertificateAuthority(ca_dir), _UPSTREAM_HOST)
    client_context = build_upstream_tls_context(ca_dir / "rootCA.pem")
    raw_upstream = RawUpstream(server_context)
    upstream_port = await raw_upstream.start()

    streams = await connect_streams()
    task = _start_session(
        streams,
        _registry(StubProvider()),
        target=ConnectRequest(host=_UPSTREAM_HOST, port=upstream_port),
        upstream_tls=client_context,
    )

    streams.left_writer.write(_UPGRADE_REQUEST_HEAD + _ALREADY_BUFFERED_BYTES)
    await streams.left_writer.drain()

    upstream_reader, upstream_writer = await raw_upstream.accept()
    return streams, task, raw_upstream, upstream_reader, upstream_writer


async def _send_request(
    streams: ConnectedStreams,
    conn: h11.Connection,
    target: str,
    body: bytes,
) -> None:
    """Send a POST request with a known body length on the client side.

    Args:
        streams: a pair of connected streams.
        conn: the client h11 state machine.
        target: the request target.
        body: the request body.
    """
    for event in (
        h11.Request(
            method="POST",
            target=target,
            headers=[
                ("host", "api.anthropic.com"),
                ("content-type", "application/json"),
                ("x-api-key", "secret"),
                ("content-length", str(len(body))),
            ],
        ),
        h11.Data(data=body),
        h11.EndOfMessage(),
    ):
        data = conn.send(event)
        if data is not None:
            streams.left_writer.write(data)
    await streams.left_writer.drain()


async def _read_response(
    streams: ConnectedStreams, conn: h11.Connection
) -> tuple[h11.Response, bytes]:
    """Read the full response on the client side.

    Args:
        streams: a pair of connected streams.
        conn: the client h11 state machine.

    Returns:
        A tuple of (response event, assembled body).
    """
    response = await asyncio.wait_for(
        next_event(streams.left_reader, conn), timeout=_TIMEOUT_S
    )
    assert isinstance(response, h11.Response)
    chunks: list[bytes] = []
    while True:
        event = await asyncio.wait_for(
            next_event(streams.left_reader, conn), timeout=_TIMEOUT_S
        )
        if isinstance(event, h11.EndOfMessage):
            return response, b"".join(chunks)
        assert isinstance(event, h11.Data)
        chunks.append(bytes(event.data))


def _header(response: h11.Response, name: bytes) -> bytes | None:
    """Find a response header by name.

    Args:
        response: the h11 response event.
        name: the lowercase header name.

    Returns:
        The header value, or None.
    """
    for header_name, value in response.headers:
        if header_name == name:
            return value
    return None


async def test_bytes_result_is_serialized_with_content_length(
    connect_streams: StreamFactory,
) -> None:
    """A regular provider response is served with content-length and an exact body."""
    payload = json.dumps({"id": "msg_1", "type": "message"}).encode()
    provider = StubProvider(
        ProviderResult(200, {"content-type": "application/json"}, payload)
    )
    streams = await connect_streams()
    task = _start_session(streams, _registry(provider))
    conn = h11.Connection(h11.CLIENT)

    await _send_request(streams, conn, "/v1/messages", _MESSAGES_BODY)
    response, body = await _read_response(streams, conn)

    assert response.status_code == 200
    assert body == payload
    assert _header(response, b"content-length") == str(len(payload)).encode()
    assert _header(response, b"transfer-encoding") is None
    task.cancel()


async def test_client_headers_reach_the_provider(
    connect_streams: StreamFactory,
) -> None:
    """Client headers reach the provider unchanged."""
    provider = StubProvider(ProviderResult(200, {"content-type": "application/json"}, b"{}"))
    streams = await connect_streams()
    task = _start_session(streams, _registry(provider))
    conn = h11.Connection(h11.CLIENT)

    await _send_request(streams, conn, "/v1/messages", _MESSAGES_BODY)
    await _read_response(streams, conn)

    assert provider.seen_headers["x-api-key"] == "secret"
    assert provider.seen_channel is not None
    assert await provider.seen_channel.is_disconnected() is False
    task.cancel()


async def test_streaming_result_is_delivered_chunk_by_chunk(
    connect_streams: StreamFactory,
) -> None:
    """Stream chunks are delivered to the client as they arrive, without full buffering.

    The provider yields the second chunk only after the test confirms
    receipt of the first: if the whole body were buffered, the test would
    deadlock and fail on timeout.
    """
    first_delivered = asyncio.Event()

    async def stream_body() -> AsyncIterator[bytes]:
        yield b"event: message_start\n\n"
        await asyncio.wait_for(first_delivered.wait(), timeout=_TIMEOUT_S)
        yield b"event: message_stop\n\n"

    provider = StubProvider(
        ProviderResult(200, {"content-type": "text/event-stream"}, stream_body())
    )
    streams = await connect_streams()
    task = _start_session(streams, _registry(provider))
    conn = h11.Connection(h11.CLIENT)

    await _send_request(streams, conn, "/v1/messages", _MESSAGES_BODY)
    response = await asyncio.wait_for(
        next_event(streams.left_reader, conn), timeout=_TIMEOUT_S
    )
    assert isinstance(response, h11.Response)
    first = await asyncio.wait_for(next_event(streams.left_reader, conn), timeout=_TIMEOUT_S)
    assert isinstance(first, h11.Data)
    assert bytes(first.data) == b"event: message_start\n\n"

    first_delivered.set()
    second = await asyncio.wait_for(next_event(streams.left_reader, conn), timeout=_TIMEOUT_S)

    assert isinstance(second, h11.Data)
    assert bytes(second.data) == b"event: message_stop\n\n"
    assert _header(response, b"transfer-encoding") == b"chunked"
    assert _header(response, b"content-length") is None
    task.cancel()


async def test_count_tokens_path_is_routed_to_the_provider(
    connect_streams: StreamFactory,
) -> None:
    """The count_tokens path is served by the provider, not forwarded upstream."""
    provider = StubProvider(ProviderResult(200, {}, b""))
    streams = await connect_streams()
    task = _start_session(streams, _registry(provider))
    conn = h11.Connection(h11.CLIENT)

    await _send_request(streams, conn, "/v1/messages/count_tokens", _MESSAGES_BODY)
    response, body = await _read_response(streams, conn)

    assert response.status_code == 200
    assert json.loads(body) == {"input_tokens": 42}
    task.cancel()


async def test_invalid_json_body_is_rejected_before_routing(
    connect_streams: StreamFactory,
) -> None:
    """Invalid JSON is rejected at the boundary, before calling the provider."""
    provider = StubProvider(ProviderResult(200, {}, b""))
    streams = await connect_streams()
    task = _start_session(streams, _registry(provider))
    conn = h11.Connection(h11.CLIENT)

    await _send_request(streams, conn, "/v1/messages", b"not json at all")
    response, body = await _read_response(streams, conn)

    assert response.status_code == 400
    assert json.loads(body)["error"]["type"] == "invalid_request_error"
    task.cancel()


async def test_garbage_on_a_fresh_connection_gets_a_400_not_silence(
    connect_streams: StreamFactory,
) -> None:
    """Garbage instead of a request line on a fresh connection gets a 400, not silence.

    h11 holds ``our_state == IDLE`` (not ``SEND_RESPONSE``) while the request
    line has not yet been parsed -- ``_fail`` must be able to respond from
    this state too, otherwise the client would get a hangup without a
    diagnosable response.
    """
    streams = await connect_streams()
    task = _start_session(streams, _registry(StubProvider()))

    streams.left_writer.write(b"not even http\r\n\r\n")
    await streams.left_writer.drain()
    head = await asyncio.wait_for(streams.left_reader.readuntil(b"\r\n\r\n"), timeout=_TIMEOUT_S)

    assert head.startswith(b"HTTP/1.1 400")
    task.cancel()


@pytest.mark.parametrize(
    ("error", "expected_status", "expected_type"),
    [
        (UpstreamError("upstream is down", status_code=502), 502, "api_error"),
        (
            ProviderError("client went away", status_code=499, error_type="api_error"),
            499,
            "api_error",
        ),
        (
            UpstreamError("bad key", status_code=401, error_type="authentication_error"),
            401,
            "authentication_error",
        ),
    ],
)
async def test_upstream_error_is_answered_with_anthropic_error_body(
    connect_streams: StreamFactory,
    error: UpstreamError,
    expected_status: int,
    expected_type: str,
) -> None:
    """UpstreamError and ProviderError become an Anthropic-format response."""
    streams = await connect_streams()
    task = _start_session(streams, _registry(StubProvider(error=error)))
    conn = h11.Connection(h11.CLIENT)

    await _send_request(streams, conn, "/v1/messages", _MESSAGES_BODY)
    response, body = await _read_response(streams, conn)

    assert response.status_code == expected_status
    assert json.loads(body) == {
        "type": "error",
        "error": {"type": expected_type, "message": error.message},
    }
    task.cancel()


async def test_unexpected_provider_exception_is_contained_by_the_barrier(
    connect_streams: StreamFactory,
) -> None:
    """An unexpected exception yields a 500 without internal details."""
    streams = await connect_streams()
    task = _start_session(
        streams, _registry(StubProvider(error=RuntimeError("secret internal detail")))
    )
    conn = h11.Connection(h11.CLIENT)

    await _send_request(streams, conn, "/v1/messages", _MESSAGES_BODY)
    response, body = await _read_response(streams, conn)

    assert response.status_code == 500
    assert json.loads(body) == {
        "type": "error",
        "error": {"type": "api_error", "message": "Internal router error"},
    }
    assert not task.done()
    task.cancel()


async def test_connection_survives_a_failed_request_and_serves_the_next_one(
    connect_streams: StreamFactory,
) -> None:
    """After an error response, the same connection serves the next request."""
    payload = json.dumps({"id": "msg_2"}).encode()
    provider = StubProvider(error=RuntimeError("boom"))
    streams = await connect_streams()
    task = _start_session(streams, _registry(provider))
    conn = h11.Connection(h11.CLIENT)

    await _send_request(streams, conn, "/v1/messages", _MESSAGES_BODY)
    failed, _body = await _read_response(streams, conn)
    conn.start_next_cycle()
    provider._error = None
    provider._result = ProviderResult(200, {"content-type": "application/json"}, payload)
    await _send_request(streams, conn, "/v1/messages", _MESSAGES_BODY)
    succeeded, second_body = await _read_response(streams, conn)

    assert failed.status_code == 500
    assert succeeded.status_code == 200
    assert second_body == payload
    task.cancel()


async def test_upgrade_request_reaches_the_upstream_unmodified_with_no_bad_gateway(
    connect_streams: StreamFactory, tmp_path: Path
) -> None:
    """A protocol upgrade is forwarded byte-for-byte, not a 502, including already-read bytes."""
    streams, task, raw_upstream, upstream_reader, upstream_writer = (
        await _open_switched_tunnel(connect_streams, tmp_path)
    )
    expected = _UPGRADE_REQUEST_HEAD + _ALREADY_BUFFERED_BYTES

    received = await asyncio.wait_for(
        upstream_reader.readexactly(len(expected)), timeout=_TIMEOUT_S
    )
    assert received == expected

    upstream_writer.write(b"HTTP/1.1 101 Switching Protocols\r\n\r\n")
    await upstream_writer.drain()
    client_head = await asyncio.wait_for(
        streams.left_reader.readuntil(b"\r\n\r\n"), timeout=_TIMEOUT_S
    )

    assert client_head == b"HTTP/1.1 101 Switching Protocols\r\n\r\n"
    assert not client_head.startswith(b"HTTP/1.1 502")

    upstream_writer.close()
    streams.left_writer.close()
    await raw_upstream.stop()
    task.cancel()


async def test_tunnel_pumps_bytes_in_both_directions_after_the_switch(
    connect_streams: StreamFactory, tmp_path: Path
) -> None:
    """After the switch, the tunnel stays live both ways, not just forwarding the request."""
    streams, task, raw_upstream, upstream_reader, upstream_writer = (
        await _open_switched_tunnel(connect_streams, tmp_path)
    )
    await asyncio.wait_for(
        upstream_reader.readexactly(len(_UPGRADE_REQUEST_HEAD) + len(_ALREADY_BUFFERED_BYTES)),
        timeout=_TIMEOUT_S,
    )

    upstream_writer.write(b"server-to-client-frame")
    await upstream_writer.drain()
    from_upstream = await asyncio.wait_for(
        streams.left_reader.readexactly(len(b"server-to-client-frame")), timeout=_TIMEOUT_S
    )
    assert from_upstream == b"server-to-client-frame"

    streams.left_writer.write(b"client-to-server-frame")
    await streams.left_writer.drain()
    from_client = await asyncio.wait_for(
        upstream_reader.readexactly(len(b"client-to-server-frame")), timeout=_TIMEOUT_S
    )
    assert from_client == b"client-to-server-frame"

    upstream_writer.close()
    streams.left_writer.close()
    await raw_upstream.stop()
    task.cancel()


@pytest.mark.parametrize(
    ("headers", "expected"),
    [
        ([(b"upgrade", b"websocket")], True),
        ([(b"connection", b"Upgrade")], True),
        ([(b"connection", b"keep-alive, Upgrade")], True),
        ([(b"connection", b"keep-alive")], False),
        ([(b"content-type", b"application/json")], False),
        ([], False),
    ],
)
def test_protocol_switch_is_recognised_from_upgrade_or_connection_header(
    headers: list[tuple[bytes, bytes]], expected: bool
) -> None:
    """An upgrade is recognized both by Upgrade and by the upgrade directive in Connection."""
    assert is_protocol_switch_requested(headers) is expected


@pytest.mark.parametrize(
    ("target", "expected"),
    [
        (b"/v1/messages", "/v1/messages"),
        (b"/v1/messages?beta=true", "/v1/messages"),
        (b"/api/feature_flags/v1?app=cli&x=1", "/api/feature_flags/v1"),
    ],
)
def test_query_string_is_stripped_from_the_routed_path(target: bytes, expected: str) -> None:
    """The routing decision is made on the path, not the target with a query part."""
    assert request_path(target) == expected


@pytest.mark.parametrize(
    ("status_code", "method", "expected"),
    [
        (200, b"POST", True),
        (204, b"POST", False),
        (304, b"GET", False),
        (200, b"HEAD", False),
        (200, b"head", False),
    ],
)
def test_bodyless_responses_are_recognised(
    status_code: int, method: bytes, expected: bool
) -> None:
    """Bodyless responses are recognized: chunked must not be added to them."""
    assert response_has_body(status_code, method) is expected


def test_bodyless_response_gets_no_framing_headers() -> None:
    """A bodyless response gets neither content-length nor chunked added."""
    headers = framed_headers(
        [(b"content-type", b"application/json"), (b"transfer-encoding", b"chunked")],
        None,
        allow_body=False,
    )

    assert headers == [(b"content-type", b"application/json")]


def test_known_length_replaces_the_upstream_framing() -> None:
    """The upstream's framing is discarded, the length is set anew."""
    headers = framed_headers(
        [(b"content-type", b"application/json"), (b"content-length", b"999")],
        12,
        allow_body=True,
    )

    assert headers == [(b"content-type", b"application/json"), (b"content-length", b"12")]


def test_hop_by_hop_headers_are_dropped_from_the_response() -> None:
    """Hop-by-hop headers of one connection leg are not forwarded further."""
    headers = framed_headers(
        [
            (b"content-type", b"text/event-stream"),
            (b"connection", b"keep-alive"),
            (b"keep-alive", b"timeout=5"),
            (b"content-encoding", b"gzip"),
        ],
        None,
        allow_body=True,
    )

    assert headers == [
        (b"content-type", b"text/event-stream"),
        (b"content-encoding", b"gzip"),
        (b"transfer-encoding", b"chunked"),
    ]


def test_request_framing_is_preserved_for_the_upstream() -> None:
    """Request body framing is preserved, hop-by-hop headers are removed."""
    headers = upstream_request_headers(
        [
            (b"host", b"api.anthropic.com"),
            (b"authorization", b"Bearer token"),
            (b"content-length", b"17"),
            (b"proxy-connection", b"keep-alive"),
            (b"connection", b"keep-alive"),
        ]
    )

    assert headers == [
        (b"host", b"api.anthropic.com"),
        (b"authorization", b"Bearer token"),
        (b"content-length", b"17"),
        (b"proxy-connection", b"keep-alive"),
    ]


async def test_next_event_times_out_when_no_data_arrives() -> None:
    """Reading the next h11 event does not wait for data forever."""
    reader = asyncio.StreamReader()
    conn = h11.Connection(h11.SERVER)

    with pytest.raises(ProxyTimeoutError):
        await next_event(reader, conn, timeout_s=0.05)


async def test_client_idle_before_a_request_gets_a_request_timeout(
    connect_streams: StreamFactory,
) -> None:
    """A client that opens a connection and sends no request gets a 408, not a hang."""
    streams = await connect_streams()
    timeouts = SessionTimeouts(
        client_request_s=0.05, upstream_headers_s=_TIMEOUT_S, idle_s=_TIMEOUT_S
    )
    task = _start_session(streams, _registry(StubProvider()), timeouts=timeouts)
    conn = h11.Connection(h11.CLIENT)

    response, body = await _read_response(streams, conn)

    assert response.status_code == 408
    assert json.loads(body)["error"]["type"] == "invalid_request_error"
    await asyncio.wait_for(task, timeout=_TIMEOUT_S)


async def test_client_body_stall_times_out_before_routing(
    connect_streams: StreamFactory,
) -> None:
    """A client that never finishes sending the request body does not block the session forever."""
    streams = await connect_streams()
    timeouts = SessionTimeouts(
        client_request_s=0.05, upstream_headers_s=_TIMEOUT_S, idle_s=_TIMEOUT_S
    )
    task = _start_session(streams, _registry(StubProvider()), timeouts=timeouts)
    conn = h11.Connection(h11.CLIENT)

    # content-length is larger than what actually arrives -- the client goes
    # silent mid-body, never reaching EndOfMessage.
    for event in (
        h11.Request(
            method="POST",
            target="/v1/messages",
            headers=[
                ("host", "api.anthropic.com"),
                ("content-type", "application/json"),
                ("content-length", str(len(_MESSAGES_BODY))),
            ],
        ),
        h11.Data(data=b'{"model":'),
    ):
        data = conn.send(event)
        if data is not None:
            streams.left_writer.write(data)
    await streams.left_writer.drain()

    response, body = await _read_response(streams, conn)

    assert response.status_code == 504
    assert json.loads(body)["error"]["type"] == "api_error"
    task.cancel()


async def test_upstream_headers_stall_times_out_separately_from_body(
    connect_streams: StreamFactory, tmp_path: Path
) -> None:
    """An upstream that never sends a status and headers does not hold the session forever."""
    ca_dir = tmp_path / "ca"
    server_context = build_leaf_tls_context(CertificateAuthority(ca_dir), _UPSTREAM_HOST)
    client_context = build_upstream_tls_context(ca_dir / "rootCA.pem")
    release = asyncio.Event()

    async def handle(_reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await release.wait()
        writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0, ssl=server_context)
    port = server.sockets[0].getsockname()[1]

    streams = await connect_streams()
    timeouts = SessionTimeouts(
        client_request_s=_TIMEOUT_S, upstream_headers_s=0.05, idle_s=_TIMEOUT_S
    )
    task = _start_session(
        streams,
        _registry(StubProvider()),
        target=ConnectRequest(host=_UPSTREAM_HOST, port=port),
        upstream_tls=client_context,
        timeouts=timeouts,
    )
    conn = h11.Connection(h11.CLIENT)
    await _send_request(streams, conn, "/api/telemetry", b"{}")

    response, body = await _read_response(streams, conn)

    assert response.status_code == 504
    assert json.loads(body)["error"]["type"] == "api_error"

    release.set()
    task.cancel()
    server.close()
    await server.wait_closed()


async def test_forwarded_response_survives_regular_chunks_slower_than_idle_check(
    connect_streams: StreamFactory, tmp_path: Path
) -> None:
    """Regular chunks more often than the idle timeout do not abort a legitimately long response.

    Three chunks with a 0.06s pause between them at a 0.2s idle timeout: each
    pause is smaller than idle, but the total transfer time (0.18s) exceeds
    the idle timeout itself -- confirming the limit applies to idleness, not
    to the whole transfer.
    """
    ca_dir = tmp_path / "ca"
    server_context = build_leaf_tls_context(CertificateAuthority(ca_dir), _UPSTREAM_HOST)
    client_context = build_upstream_tls_context(ca_dir / "rootCA.pem")

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        upstream_conn = h11.Connection(h11.SERVER)
        await next_event(reader, upstream_conn)
        while True:
            event = await next_event(reader, upstream_conn)
            if isinstance(event, h11.EndOfMessage):
                break
        for event in (
            h11.Response(
                status_code=200,
                headers=[
                    ("content-type", "text/event-stream"),
                    ("transfer-encoding", "chunked"),
                ],
            ),
            *(h11.Data(data=b"chunk") for _ in range(3)),
        ):
            data = upstream_conn.send(event)
            if data is not None:
                writer.write(data)
            await writer.drain()
            await asyncio.sleep(0.06)
        data = upstream_conn.send(h11.EndOfMessage())
        if data is not None:
            writer.write(data)
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0, ssl=server_context)
    port = server.sockets[0].getsockname()[1]

    streams = await connect_streams()
    timeouts = SessionTimeouts(
        client_request_s=_TIMEOUT_S, upstream_headers_s=_TIMEOUT_S, idle_s=0.2
    )
    task = _start_session(
        streams,
        _registry(StubProvider()),
        target=ConnectRequest(host=_UPSTREAM_HOST, port=port),
        upstream_tls=client_context,
        timeouts=timeouts,
    )
    conn = h11.Connection(h11.CLIENT)
    await _send_request(streams, conn, "/api/telemetry", b"{}")

    response, body = await _read_response(streams, conn)

    assert response.status_code == 200
    assert body == b"chunk" * 3
    server.close()
    await server.wait_closed()
    task.cancel()


async def test_response_body_idle_timeout_closes_the_session_after_headers_sent(
    connect_streams: StreamFactory, tmp_path: Path
) -> None:
    """Silence in the response body after headers are sent does not replay the response.

    The headers have already been sent to the client when upstream goes
    silent -- by contract ``_fail`` does not write a second response on top
    of the first, and the session finishes on its own, without leaving the
    task hanging on a read.
    """
    ca_dir = tmp_path / "ca"
    server_context = build_leaf_tls_context(CertificateAuthority(ca_dir), _UPSTREAM_HOST)
    client_context = build_upstream_tls_context(ca_dir / "rootCA.pem")
    release = asyncio.Event()

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        upstream_conn = h11.Connection(h11.SERVER)
        await next_event(reader, upstream_conn)
        while True:
            event = await next_event(reader, upstream_conn)
            if isinstance(event, h11.EndOfMessage):
                break
        data = upstream_conn.send(
            h11.Response(
                status_code=200,
                headers=[
                    ("content-type", "text/event-stream"),
                    ("transfer-encoding", "chunked"),
                ],
            )
        )
        if data is not None:
            writer.write(data)
        await writer.drain()
        await release.wait()
        writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0, ssl=server_context)
    port = server.sockets[0].getsockname()[1]

    streams = await connect_streams()
    timeouts = SessionTimeouts(
        client_request_s=_TIMEOUT_S, upstream_headers_s=_TIMEOUT_S, idle_s=0.05
    )
    task = _start_session(
        streams,
        _registry(StubProvider()),
        target=ConnectRequest(host=_UPSTREAM_HOST, port=port),
        upstream_tls=client_context,
        timeouts=timeouts,
    )
    conn = h11.Connection(h11.CLIENT)
    await _send_request(streams, conn, "/api/telemetry", b"{}")

    response = await asyncio.wait_for(next_event(streams.left_reader, conn), timeout=_TIMEOUT_S)
    assert isinstance(response, h11.Response)
    assert response.status_code == 200

    # The session finishes on its own (no held connection) without a second response.
    await asyncio.wait_for(task, timeout=_TIMEOUT_S)

    release.set()
    server.close()
    await server.wait_closed()


async def test_upstream_disconnect_mid_body_is_not_retried(
    connect_streams: StreamFactory, tmp_path: Path
) -> None:
    """An upstream that disconnects mid-response-body is not retried.

    The client has already received the status, headers, and part of the
    body -- a second attempt would splice two independent responses into
    one stream. Verified by directly counting upstream connections: there
    must be exactly one.
    """
    ca_dir = tmp_path / "ca"
    server_context = build_leaf_tls_context(CertificateAuthority(ca_dir), _UPSTREAM_HOST)
    client_context = build_upstream_tls_context(ca_dir / "rootCA.pem")
    connection_count = 0
    aborted = asyncio.Event()

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        nonlocal connection_count
        connection_count += 1
        upstream_conn = h11.Connection(h11.SERVER)
        await next_event(reader, upstream_conn)
        while True:
            event = await next_event(reader, upstream_conn)
            if isinstance(event, h11.EndOfMessage):
                break
        for event in (
            h11.Response(
                status_code=200,
                headers=[
                    ("content-type", "text/event-stream"),
                    ("transfer-encoding", "chunked"),
                ],
            ),
            h11.Data(data=b"partial"),
        ):
            data = upstream_conn.send(event)
            if data is not None:
                writer.write(data)
        await writer.drain()
        # Disconnect mid-body -- no EndOfMessage, chunked framing is not closed.
        writer.close()
        aborted.set()

    server = await asyncio.start_server(handle, "127.0.0.1", 0, ssl=server_context)
    port = server.sockets[0].getsockname()[1]

    streams = await connect_streams()
    task = _start_session(
        streams,
        _registry(StubProvider()),
        target=ConnectRequest(host=_UPSTREAM_HOST, port=port),
        upstream_tls=client_context,
    )
    conn = h11.Connection(h11.CLIENT)
    await _send_request(streams, conn, "/api/telemetry", b"{}")

    response = await asyncio.wait_for(next_event(streams.left_reader, conn), timeout=_TIMEOUT_S)
    assert isinstance(response, h11.Response)
    assert response.status_code == 200

    await asyncio.wait_for(aborted.wait(), timeout=_TIMEOUT_S)
    await asyncio.wait_for(task, timeout=_TIMEOUT_S)

    assert connection_count == 1

    server.close()
    await server.wait_closed()
