"""End-to-end tests for the forward proxy (``proxy.server``).

The tests bring up a real server, go through a real ``CONNECT`` and a real
TLS handshake with a certificate issued by the router's root CA. Local
servers play the role of the outside world: a TLS upstream for the MITM
host and a plain echo for a host outside the allowlist.
"""

from __future__ import annotations

import asyncio
import datetime
import json
import ssl
import time
from collections.abc import AsyncIterator, Mapping
from pathlib import Path

import h11
import pytest
import pytest_asyncio
import structlog.testing

from providers.base import ClientChannel, ProviderResult
from proxy import certificates
from proxy.certificates import CertificateAuthority
from proxy.server import ForwardProxyServer
from proxy.session import next_event
from proxy.tls import build_leaf_tls_context
from routing.registry import ProviderRegistry
from routing.schema import ProviderCfg, RouteLimits
from settings import Settings

_TIMEOUT_S = 5.0
_MITM_HOST = "localhost"
_TUNNEL_HOST = "127.0.0.1"
_UPSTREAM_PAYLOAD = json.dumps({"flags": {"remote_control": True}}).encode()
_PROVIDER_PAYLOAD = json.dumps({"id": "msg_routed", "type": "message"}).encode()
_MESSAGES_BODY = json.dumps({"model": "claude-test"}).encode()


class StubProvider:
    """Stub provider that returns a fixed response for ``/v1/messages``."""

    name = "stub"
    cfg = ProviderCfg(type="passthrough", base_url="https://upstream.test")

    async def handle_messages(
        self,
        raw_body: bytes,
        client_headers: Mapping[str, str],
        client_channel: ClientChannel,
        upstream_model: str | None,
        limits: RouteLimits,
    ) -> ProviderResult:
        """Return a fixed response instead of calling a real model."""
        return ProviderResult(
            status_code=200,
            headers={"content-type": "application/json"},
            body=_PROVIDER_PAYLOAD,
        )

    async def count_tokens(
        self,
        raw_body: bytes,
        client_headers: Mapping[str, str],
        upstream_model: str | None,
        limits: RouteLimits,
    ) -> ProviderResult:
        """Return a fixed token count estimate."""
        return ProviderResult(status_code=200, headers={}, body=b"{}")

    async def aclose(self) -> None:
        """The stub has nothing to close."""


class FakeUpstream:
    """Local TLS server standing in for the real ``api.anthropic.com``.

    Attributes:
        requests: parsed requests that reached the upstream.
    """

    def __init__(self, ca_dir: Path) -> None:
        """Prepare a server with a certificate from the same root CA.

        Args:
            ca_dir: the router's root CA directory.
        """
        self._context = build_leaf_tls_context(CertificateAuthority(ca_dir), _MITM_HOST)
        self._server: asyncio.Server | None = None
        self.requests: list[tuple[str, str, bytes]] = []

    async def start(self) -> int:
        """Bring up the server on a free port of the loopback interface.

        Returns:
            The port that was bound.
        """
        self._server = await asyncio.start_server(
            self._handle, _TUNNEL_HOST, 0, ssl=self._context
        )
        port: int = self._server.sockets[0].getsockname()[1]
        return port

    async def stop(self) -> None:
        """Stop the server and wait for the listening socket to close."""
        if self._server is None:
            return
        self._server.close()
        await self._server.wait_closed()

    async def _handle(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        """Accept a single request, record it, and reply with the fixed body."""
        conn = h11.Connection(h11.SERVER)
        request = await next_event(reader, conn)
        if not isinstance(request, h11.Request):
            return
        chunks: list[bytes] = []
        while True:
            event = await next_event(reader, conn)
            if isinstance(event, h11.EndOfMessage):
                break
            if isinstance(event, h11.Data):
                chunks.append(bytes(event.data))
        self.requests.append(
            (
                request.method.decode(),
                request.target.decode(),
                b"".join(chunks),
            )
        )
        for event in (
            h11.Response(
                status_code=200,
                headers=[
                    ("content-type", "application/json"),
                    ("content-length", str(len(_UPSTREAM_PAYLOAD))),
                ],
            ),
            h11.Data(data=_UPSTREAM_PAYLOAD),
            h11.EndOfMessage(),
        ):
            data = conn.send(event)
            if data is not None:
                writer.write(data)
        await writer.drain()


@pytest_asyncio.fixture
async def echo_port() -> AsyncIterator[int]:
    """Bring up a plain echo server standing in for a host outside the allowlist.

    Yields:
        The echo server's port.
    """

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        while True:
            chunk = await reader.read(4096)
            if not chunk:
                return
            writer.write(b"echo:" + chunk)
            await writer.drain()

    server = await asyncio.start_server(handle, _TUNNEL_HOST, 0)
    try:
        yield int(server.sockets[0].getsockname()[1])
    finally:
        server.close()
        await server.wait_closed()


@pytest_asyncio.fixture
async def upstream(tmp_path: Path) -> AsyncIterator[FakeUpstream]:
    """Bring up a TLS upstream with a certificate from the router's root CA.

    Yields:
        The running fake upstream.
    """
    fake = FakeUpstream(tmp_path / "ca")
    await fake.start()
    try:
        yield fake
    finally:
        await fake.stop()


@pytest_asyncio.fixture
async def proxy_port(
    _env: None,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[int]:
    """Bring up the forward proxy with a MITM allowlist of one local host.

    Yields:
        The port on which the proxy accepts CONNECT.
    """
    ca_dir = tmp_path / "ca"
    monkeypatch.setenv("ROUTER_PROXY_CA_DIR", str(ca_dir))
    monkeypatch.setenv("ROUTER_PROXY_MITM_HOSTS", _MITM_HOST)
    monkeypatch.setenv("ROUTER_PROXY_HOST", _TUNNEL_HOST)
    monkeypatch.setenv("ROUTER_PROXY_PORT", "0")
    monkeypatch.setenv(
        "ROUTER_PROXY_UPSTREAM_CA_BUNDLE", str(ca_dir / "rootCA.pem")
    )

    registry = ProviderRegistry({"stub": StubProvider()}, [], "stub")  # type: ignore[dict-item]
    server = ForwardProxyServer(Settings(), registry)
    listener = await server.start()
    try:
        yield int(listener.sockets[0].getsockname()[1])
    finally:
        listener.close()
        await listener.wait_closed()


def _root_certificate(tmp_path: Path) -> Path:
    """Return the path to the root certificate created by the proxy.

    Args:
        tmp_path: the test's temporary directory.

    Returns:
        The path to the root certificate's PEM file.
    """
    return tmp_path / "ca" / "rootCA.pem"


def _configure_proxy_env(monkeypatch: pytest.MonkeyPatch, ca_dir: Path, host: str) -> None:
    """Set the ``ROUTER_PROXY_*`` environment variables for a single test.

    Args:
        monkeypatch: the fixture for setting environment variables.
        ca_dir: the root CA directory for this test.
        host: the value of ``ROUTER_PROXY_HOST``.
    """
    monkeypatch.setenv("ROUTER_PROXY_CA_DIR", str(ca_dir))
    monkeypatch.setenv("ROUTER_PROXY_MITM_HOSTS", _MITM_HOST)
    monkeypatch.setenv("ROUTER_PROXY_HOST", host)
    monkeypatch.setenv("ROUTER_PROXY_PORT", "0")
    monkeypatch.setenv("ROUTER_PROXY_UPSTREAM_CA_BUNDLE", str(ca_dir / "rootCA.pem"))


async def _open_tunnel(
    proxy_port: int, host: str, port: int
) -> tuple[asyncio.StreamReader, asyncio.StreamWriter, bytes]:
    """Open a tunnel through the proxy and return its streams along with the response.

    Args:
        proxy_port: the forward proxy's port.
        host: the target host in the CONNECT request.
        port: the target port in the CONNECT request.

    Returns:
        A tuple of (read stream, write stream, CONNECT response prefix).
    """
    reader, writer = await asyncio.open_connection(_TUNNEL_HOST, proxy_port)
    authority = f"{host}:{port}"
    writer.write(f"CONNECT {authority} HTTP/1.1\r\nHost: {authority}\r\n\r\n".encode())
    await writer.drain()
    head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=_TIMEOUT_S)
    return reader, writer, head


async def _open_mitm_connection(
    proxy_port: int, upstream_port: int, root_certificate: Path
) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    """Go through CONNECT and a TLS handshake to the MITM host via the proxy.

    Args:
        proxy_port: the forward proxy's port.
        upstream_port: the target port specified in CONNECT.
        root_certificate: the router's root certificate for verifying the MITM chain.

    Returns:
        A pair of streams with TLS established.
    """
    reader, writer, head = await _open_tunnel(proxy_port, _MITM_HOST, upstream_port)
    assert head.startswith(b"HTTP/1.1 200")
    context = ssl.create_default_context(cafile=str(root_certificate))
    context.set_alpn_protocols(["http/1.1"])
    await writer.start_tls(context, server_hostname=_MITM_HOST)
    return reader, writer


async def _exchange(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    request: h11.Request,
    body: bytes,
) -> tuple[h11.Response, bytes]:
    """Send a request over HTTP/1.1 and read the full response.

    Args:
        reader: the read stream.
        writer: the write stream.
        request: the request event.
        body: the request body.

    Returns:
        A tuple of (response event, response body).
    """
    conn = h11.Connection(h11.CLIENT)
    events: list[h11.Event] = [request]
    if body:
        events.append(h11.Data(data=body))
    events.append(h11.EndOfMessage())
    for event in events:
        data = conn.send(event)
        if data is not None:
            writer.write(data)
    await writer.drain()

    response = await asyncio.wait_for(next_event(reader, conn), timeout=_TIMEOUT_S)
    assert isinstance(response, h11.Response)
    chunks: list[bytes] = []
    while True:
        event = await asyncio.wait_for(next_event(reader, conn), timeout=_TIMEOUT_S)
        if isinstance(event, h11.EndOfMessage):
            return response, b"".join(chunks)
        assert isinstance(event, h11.Data)
        chunks.append(bytes(event.data))


async def test_messages_path_is_served_by_the_provider_registry(
    proxy_port: int, upstream: FakeUpstream, tmp_path: Path
) -> None:
    """``/v1/messages`` is served by the registry and never reaches the real upstream."""
    upstream_port = await upstream.start()
    reader, writer = await _open_mitm_connection(
        proxy_port, upstream_port, _root_certificate(tmp_path)
    )

    response, body = await _exchange(
        reader,
        writer,
        h11.Request(
            method="POST",
            target="/v1/messages",
            headers=[
                ("host", _MITM_HOST),
                ("content-type", "application/json"),
                ("content-length", str(len(_MESSAGES_BODY))),
            ],
        ),
        _MESSAGES_BODY,
    )
    writer.close()

    assert response.status_code == 200
    assert body == _PROVIDER_PAYLOAD
    assert upstream.requests == []


async def test_other_paths_are_forwarded_to_the_real_upstream(
    proxy_port: int, upstream: FakeUpstream, tmp_path: Path
) -> None:
    """A non-messages path reaches the real upstream unchanged."""
    upstream_port = await upstream.start()
    reader, writer = await _open_mitm_connection(
        proxy_port, upstream_port, _root_certificate(tmp_path)
    )
    telemetry = json.dumps({"event": "startup"}).encode()

    response, body = await _exchange(
        reader,
        writer,
        h11.Request(
            method="POST",
            target="/api/telemetry?source=cli",
            headers=[
                ("host", _MITM_HOST),
                ("content-type", "application/json"),
                ("content-length", str(len(telemetry))),
            ],
        ),
        telemetry,
    )
    writer.close()

    assert response.status_code == 200
    assert body == _UPSTREAM_PAYLOAD
    assert upstream.requests == [("POST", "/api/telemetry?source=cli", telemetry)]


async def test_tls_termination_negotiates_http11_over_alpn(
    proxy_port: int, upstream: FakeUpstream, tmp_path: Path
) -> None:
    """The proxy negotiates only HTTP/1.1, excluding HTTP/2 frames."""
    upstream_port = await upstream.start()
    reader, writer = await _open_mitm_connection(
        proxy_port, upstream_port, _root_certificate(tmp_path)
    )

    tls_object = writer.get_extra_info("ssl_object")
    writer.close()

    assert tls_object.selected_alpn_protocol() == "http/1.1"
    assert reader is not None


async def test_host_outside_allowlist_is_tunnelled_without_decryption(
    proxy_port: int, echo_port: int
) -> None:
    """A host outside the allowlist gets a blind tunnel: bytes pass through as-is."""
    reader, writer, head = await _open_tunnel(proxy_port, _TUNNEL_HOST, echo_port)

    assert head.startswith(b"HTTP/1.1 200 Connection Established")
    writer.write(b"\x16\x03\x01raw-tls-bytes")
    await writer.drain()
    received = await asyncio.wait_for(reader.readexactly(21), timeout=_TIMEOUT_S)
    writer.close()

    assert received == b"echo:\x16\x03\x01raw-tls-bytes"


async def test_tunnel_to_unreachable_host_answers_bad_gateway(proxy_port: int) -> None:
    """An unreachable tunnel target becomes a 502 with an Anthropic-format body."""
    # Port 1 on loopback is guaranteed to have no listener.
    reader, writer, head = await _open_tunnel(proxy_port, _TUNNEL_HOST, 1)
    body = await asyncio.wait_for(reader.read(), timeout=_TIMEOUT_S)
    writer.close()

    assert head.startswith(b"HTTP/1.1 502 Bad Gateway")
    assert json.loads(body)["error"]["type"] == "api_error"


async def test_non_connect_request_on_the_proxy_port_is_rejected(
    proxy_port: int,
) -> None:
    """A regular request on the proxy port is rejected with 405, not silently."""
    reader, writer = await asyncio.open_connection(_TUNNEL_HOST, proxy_port)
    writer.write(b"GET /v1/messages HTTP/1.1\r\nHost: api.anthropic.com\r\n\r\n")
    await writer.drain()
    head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=_TIMEOUT_S)
    body = await asyncio.wait_for(reader.read(), timeout=_TIMEOUT_S)
    writer.close()

    assert head.startswith(b"HTTP/1.1 405 Method Not Allowed")
    assert json.loads(body)["error"]["type"] == "invalid_request_error"


async def test_server_keeps_serving_after_a_rejected_connection(
    proxy_port: int, echo_port: int
) -> None:
    """A rejection on one connection does not prevent serving the next one."""
    _reader, rejected_writer = await asyncio.open_connection(_TUNNEL_HOST, proxy_port)
    rejected_writer.write(b"GARBAGE\r\n\r\n")
    await rejected_writer.drain()
    rejected_writer.close()

    reader, writer, head = await _open_tunnel(proxy_port, _TUNNEL_HOST, echo_port)
    writer.write(b"still-alive")
    await writer.drain()
    received = await asyncio.wait_for(reader.readexactly(16), timeout=_TIMEOUT_S)
    writer.close()

    assert head.startswith(b"HTTP/1.1 200 Connection Established")
    assert received == b"echo:still-alive"


async def test_start_on_non_loopback_host_logs_insecure_bind_warning(
    _env: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Binding to a non-loopback address logs an explicit open-relay warning.

    ``structlog.testing.capture_logs`` intercepts the event directly,
    bypassing stdlib logging: without explicit ``setup_logging()`` (not
    called in these tests), structlog by default does NOT go through
    ``logging`` handlers, and ``caplog`` would see nothing.
    """
    _configure_proxy_env(monkeypatch, tmp_path / "ca", "0.0.0.0")

    registry = ProviderRegistry({"stub": StubProvider()}, [], "stub")  # type: ignore[dict-item]
    server = ForwardProxyServer(Settings(), registry)
    with structlog.testing.capture_logs() as captured:
        listener = await server.start()
    try:
        assert any(entry["event"] == "proxy_insecure_bind" for entry in captured)
    finally:
        listener.close()
        await listener.wait_closed()


async def test_start_on_loopback_host_does_not_log_insecure_bind_warning(
    _env: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The default loopback bind does not produce an open-relay warning."""
    _configure_proxy_env(monkeypatch, tmp_path / "ca", _TUNNEL_HOST)

    registry = ProviderRegistry({"stub": StubProvider()}, [], "stub")  # type: ignore[dict-item]
    server = ForwardProxyServer(Settings(), registry)
    with structlog.testing.capture_logs() as captured:
        listener = await server.start()
    try:
        assert not any(entry["event"] == "proxy_insecure_bind" for entry in captured)
    finally:
        listener.close()
        await listener.wait_closed()


async def test_serve_mitm_presents_a_certificate_reissued_since_startup(
    _env: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A certificate that expired during uptime is reissued by the proxy itself, without a restart.

    The constructor builds the contexts once at startup -- a certificate
    issued at that moment with artificially zero validity simulates a host
    for which 29+ days of process uptime have passed without a restart. A
    connection arriving later must get a fresh certificate, not the one
    frozen at startup.
    """
    ca_dir = tmp_path / "ca"
    _configure_proxy_env(monkeypatch, ca_dir, _TUNNEL_HOST)

    registry = ProviderRegistry({"stub": StubProvider()}, [], "stub")  # type: ignore[dict-item]
    monkeypatch.setattr(certificates, "_LEAF_VALIDITY", datetime.timedelta(seconds=0))
    server = ForwardProxyServer(Settings(), registry)
    monkeypatch.setattr(certificates, "_LEAF_VALIDITY", datetime.timedelta(days=30))

    listener = await server.start()
    proxy_port = int(listener.sockets[0].getsockname()[1])
    try:
        # The target port is unreachable and unneeded: for a MITM host the
        # proxy does not open an outbound connection at this step, it
        # terminates TLS itself right away.
        _reader, writer, head = await _open_tunnel(proxy_port, _MITM_HOST, 1)
        assert head.startswith(b"HTTP/1.1 200")

        context = ssl.create_default_context(cafile=str(_root_certificate(tmp_path)))
        context.set_alpn_protocols(["http/1.1"])
        await writer.start_tls(context, server_hostname=_MITM_HOST)

        peer_certificate = writer.get_extra_info("ssl_object").getpeercert()
        writer.close()
    finally:
        listener.close()
        await listener.wait_closed()

    not_after = ssl.cert_time_to_seconds(peer_certificate["notAfter"])
    assert not_after > time.time() + 20 * 24 * 3600
