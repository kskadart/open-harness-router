"""Unit tests for the forward proxy's outbound connections (``proxy.outbound``).

Verifies both direct egress and chaining through a corporate proxy: the
router must send its own ``CONNECT`` and wait for ``200`` before it starts
pumping bytes.
"""

from __future__ import annotations

import asyncio
import ssl
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio

from proxy.certificates import CertificateAuthority
from proxy.outbound import (
    OutboundConfigError,
    OutboundConnector,
    TunnelError,
    parse_upstream_proxy,
)
from proxy.streams import pump_tunnel
from proxy.tls import build_leaf_tls_context, build_upstream_tls_context

_TIMEOUT_S = 2.0
_HEAD_TERMINATOR = b"\r\n\r\n"
_TLS_HOST = "localhost"


class FakeProxy:
    """Mock upstream HTTP proxy that responds to ``CONNECT``.

    Attributes:
        connect_heads: prefixes of all received ``CONNECT`` requests.
    """

    def __init__(self, response: bytes) -> None:
        """Create a mock with a predefined response to ``CONNECT``.

        Args:
            response: raw response bytes sent back for CONNECT.
        """
        self._response = response
        self._server: asyncio.Server | None = None
        self.connect_heads: list[bytes] = []

    async def start(self) -> int:
        """Bring up the mock on a free port of the loopback interface.

        Returns:
            The port that was bound.
        """
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        port: int = self._server.sockets[0].getsockname()[1]
        return port

    async def stop(self) -> None:
        """Stop the mock and wait for the listening socket to close."""
        if self._server is None:
            return
        self._server.close()
        await self._server.wait_closed()

    async def _handle(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        """Accept CONNECT, reply with the prepared response, and echo bytes."""
        self.connect_heads.append(await reader.readuntil(_HEAD_TERMINATOR))
        writer.write(self._response)
        await writer.drain()
        while True:
            chunk = await reader.read(4096)
            if not chunk:
                return
            writer.write(chunk)
            await writer.drain()


@pytest_asyncio.fixture
async def echo_port() -> AsyncIterator[int]:
    """Bring up an echo server standing in for the final connection target.

    Yields:
        The echo server's port on the loopback interface.
    """

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        while True:
            chunk = await reader.read(4096)
            if not chunk:
                return
            writer.write(b"echo:" + chunk)
            await writer.drain()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    try:
        yield int(server.sockets[0].getsockname()[1])
    finally:
        server.close()
        await server.wait_closed()


def test_upstream_proxy_url_is_parsed_into_host_and_port() -> None:
    """The upstream proxy address is parsed into host and port."""
    proxy = parse_upstream_proxy("http://proxy.corp.internal:3128")

    assert (proxy.host, proxy.port, proxy.authorization) == (
        "proxy.corp.internal",
        3128,
        None,
    )


def test_upstream_proxy_credentials_become_proxy_authorization() -> None:
    """Credentials from the URL become the Proxy-Authorization header."""
    proxy = parse_upstream_proxy("http://user:secret@proxy.corp.internal:3128")

    assert proxy.authorization == "Basic dXNlcjpzZWNyZXQ="


def test_non_http_upstream_proxy_scheme_is_rejected() -> None:
    """A scheme other than http is rejected at startup, not at runtime."""
    with pytest.raises(OutboundConfigError):
        parse_upstream_proxy("socks5://proxy.corp.internal:1080")


def test_upstream_proxy_url_without_host_is_rejected() -> None:
    """A URL without a host is rejected."""
    with pytest.raises(OutboundConfigError):
        parse_upstream_proxy("http://:3128")


def test_connector_without_upstream_proxy_never_chains() -> None:
    """Without a configured upstream proxy, connections are always direct."""
    connector = OutboundConnector(None, frozenset(), _TIMEOUT_S)

    assert connector.uses_upstream_proxy("api.anthropic.com") is False


def test_connector_chains_when_upstream_proxy_is_configured() -> None:
    """With an upstream proxy configured, the connection goes through it."""
    connector = OutboundConnector(
        parse_upstream_proxy("http://proxy.corp.internal:3128"), frozenset(), _TIMEOUT_S
    )

    assert connector.uses_upstream_proxy("api.anthropic.com") is True


def test_no_proxy_host_bypasses_configured_upstream_proxy() -> None:
    """A host from no_proxy goes directly even with an upstream proxy configured."""
    connector = OutboundConnector(
        parse_upstream_proxy("http://proxy.corp.internal:3128"),
        frozenset({"localhost"}),
        _TIMEOUT_S,
    )

    assert connector.uses_upstream_proxy("LocalHost") is False
    assert connector.uses_upstream_proxy("api.anthropic.com") is True


async def test_direct_connection_reaches_the_target_without_connect(
    echo_port: int,
) -> None:
    """Without an upstream proxy, the connection opens directly to the target."""
    connector = OutboundConnector(None, frozenset(), _TIMEOUT_S)

    reader, writer = await connector.open("127.0.0.1", echo_port)
    writer.write(b"ping")
    await writer.drain()
    received = await asyncio.wait_for(reader.readexactly(9), timeout=_TIMEOUT_S)
    writer.close()

    assert received == b"echo:ping"


async def test_chained_connection_sends_connect_before_pumping_bytes() -> None:
    """Through an upstream proxy, CONNECT is sent first, then data."""
    fake_proxy = FakeProxy(b"HTTP/1.1 200 Connection Established\r\n\r\n")
    proxy_port = await fake_proxy.start()
    connector = OutboundConnector(
        parse_upstream_proxy(f"http://127.0.0.1:{proxy_port}"), frozenset(), _TIMEOUT_S
    )

    reader, writer = await connector.open("api.anthropic.com", 443)
    writer.write(b"payload")
    await writer.drain()
    received = await asyncio.wait_for(reader.readexactly(7), timeout=_TIMEOUT_S)
    writer.close()
    await fake_proxy.stop()

    assert received == b"payload"
    assert fake_proxy.connect_heads[0].startswith(b"CONNECT api.anthropic.com:443 HTTP/1.1\r\n")
    assert b"Host: api.anthropic.com:443\r\n" in fake_proxy.connect_heads[0]


async def test_chained_connection_sends_proxy_authorization_when_configured() -> None:
    """Upstream proxy credentials are sent in the CONNECT header."""
    fake_proxy = FakeProxy(b"HTTP/1.1 200 Connection Established\r\n\r\n")
    proxy_port = await fake_proxy.start()
    connector = OutboundConnector(
        parse_upstream_proxy(f"http://user:secret@127.0.0.1:{proxy_port}"),
        frozenset(),
        _TIMEOUT_S,
    )

    _reader, writer = await connector.open("api.anthropic.com", 443)
    writer.close()
    await fake_proxy.stop()

    assert b"Proxy-Authorization: Basic dXNlcjpzZWNyZXQ=\r\n" in fake_proxy.connect_heads[0]


async def test_upstream_proxy_refusal_raises_tunnel_error() -> None:
    """A refusal from the upstream proxy becomes a TunnelError, not a hang."""
    fake_proxy = FakeProxy(b"HTTP/1.1 407 Proxy Authentication Required\r\n\r\n")
    proxy_port = await fake_proxy.start()
    connector = OutboundConnector(
        parse_upstream_proxy(f"http://127.0.0.1:{proxy_port}"), frozenset(), _TIMEOUT_S
    )

    with pytest.raises(TunnelError, match="407"):
        await connector.open("api.anthropic.com", 443)

    await fake_proxy.stop()


async def test_no_proxy_host_connects_directly_even_with_upstream_proxy(
    echo_port: int,
) -> None:
    """A host from no_proxy bypasses the upstream proxy at the connection level."""
    fake_proxy = FakeProxy(b"HTTP/1.1 200 Connection Established\r\n\r\n")
    proxy_port = await fake_proxy.start()
    connector = OutboundConnector(
        parse_upstream_proxy(f"http://127.0.0.1:{proxy_port}"),
        frozenset({"127.0.0.1"}),
        _TIMEOUT_S,
    )

    reader, writer = await connector.open("127.0.0.1", echo_port)
    writer.write(b"ping")
    await writer.drain()
    received = await asyncio.wait_for(reader.readexactly(9), timeout=_TIMEOUT_S)
    writer.close()
    await fake_proxy.stop()

    assert received == b"echo:ping"
    assert fake_proxy.connect_heads == []


async def test_unreachable_target_raises_tunnel_error() -> None:
    """An unreachable target becomes a TunnelError with a clear message."""
    connector = OutboundConnector(None, frozenset(), _TIMEOUT_S)

    # Port 1 on loopback is guaranteed to have no listener.
    with pytest.raises(TunnelError, match="failed"):
        await connector.open("127.0.0.1", 1)


class FakeTunnelingProxy:
    """Mock corporate proxy that actually builds a tunnel."""

    def __init__(self) -> None:
        """Create a mock with no server started yet."""
        self._server: asyncio.Server | None = None

    async def start(self) -> int:
        """Bring up the mock on a free port of the loopback interface.

        Returns:
            The port that was bound.
        """
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        port: int = self._server.sockets[0].getsockname()[1]
        return port

    async def stop(self) -> None:
        """Stop the mock and wait for the listening socket to close."""
        if self._server is None:
            return
        self._server.close()
        await self._server.wait_closed()

    async def _handle(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        """Accept CONNECT, connect to the target, and pump bytes blindly."""
        head = await reader.readuntil(_HEAD_TERMINATOR)
        authority = head.decode().split(" ")[1]
        host, _, port = authority.rpartition(":")
        target_reader, target_writer = await asyncio.open_connection(host, int(port))
        writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
        await writer.drain()
        await pump_tunnel(reader, writer, target_reader, target_writer, _TIMEOUT_S)


async def test_tls_to_the_target_is_established_through_the_upstream_proxy(
    tmp_path: Path,
) -> None:
    """TLS to the target is established on top of the corporate proxy tunnel.

    This is the path for transparently forwarding non-mocked MITM-host
    traffic: the router itself opens the connection, so it must go through
    the same chain.
    """
    authority = CertificateAuthority(tmp_path / "ca")

    async def serve_tls(
        reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        writer.write(b"secured:" + await reader.readexactly(4))
        await writer.drain()

    target = await asyncio.start_server(
        serve_tls, "127.0.0.1", 0, ssl=build_leaf_tls_context(authority, _TLS_HOST)
    )
    target_port = target.sockets[0].getsockname()[1]
    fake_proxy = FakeTunnelingProxy()
    proxy_port = await fake_proxy.start()
    connector = OutboundConnector(
        parse_upstream_proxy(f"http://127.0.0.1:{proxy_port}"), frozenset(), _TIMEOUT_S
    )

    reader, writer = await connector.open_tls(
        _TLS_HOST,
        target_port,
        build_upstream_tls_context(authority.root_certificate_path()),
    )
    tls_object = writer.get_extra_info("ssl_object")
    writer.write(b"ping")
    await writer.drain()
    received = await asyncio.wait_for(reader.readexactly(12), timeout=_TIMEOUT_S)
    writer.close()
    await fake_proxy.stop()
    target.close()
    await target.wait_closed()

    assert received == b"secured:ping"
    assert tls_object.selected_alpn_protocol() == "http/1.1"


async def test_tls_handshake_failure_through_proxy_raises_tunnel_error(
    tmp_path: Path,
) -> None:
    """An untrusted target certificate becomes a TunnelError, not a leaked exception."""
    authority = CertificateAuthority(tmp_path / "ca")
    foreign_authority = CertificateAuthority(tmp_path / "foreign-ca")

    async def serve_tls(
        _reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        writer.close()

    target = await asyncio.start_server(
        serve_tls,
        "127.0.0.1",
        0,
        ssl=build_leaf_tls_context(foreign_authority, _TLS_HOST),
    )
    target_port = target.sockets[0].getsockname()[1]
    connector = OutboundConnector(None, frozenset(), _TIMEOUT_S)

    with pytest.raises(TunnelError, match="TLS handshake"):
        await connector.open_tls(
            _TLS_HOST,
            target_port,
            build_upstream_tls_context(authority.root_certificate_path()),
        )

    target.close()
    await target.wait_closed()


def test_upstream_ca_bundle_is_used_for_outgoing_verification(tmp_path: Path) -> None:
    """The specified bundle replaces the system roots for outgoing verification."""
    authority = CertificateAuthority(tmp_path / "ca")

    context = build_upstream_tls_context(authority.root_certificate_path())

    subjects = {entry["subject"] for entry in context.get_ca_certs()}
    assert any("open-harness-router local CA" in str(subject) for subject in subjects)
    assert context.verify_mode is ssl.CERT_REQUIRED


async def test_connector_retries_a_transient_connect_failure(
    echo_port: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A transient connection failure is retried instead of failing the attempt immediately."""
    real_open_connection = asyncio.open_connection
    attempts = 0

    async def flaky_open_connection(
        host: str, port: int
    ) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise ConnectionRefusedError("simulated transient failure")
        return await real_open_connection(host, port)

    monkeypatch.setattr(asyncio, "open_connection", flaky_open_connection)
    connector = OutboundConnector(
        None,
        frozenset(),
        _TIMEOUT_S,
        retry_max_attempts=3,
        retry_backoff_base_s=0.01,
        retry_backoff_max_s=0.01,
    )

    reader, writer = await connector.open("127.0.0.1", echo_port)
    writer.close()

    assert attempts == 3


async def test_connector_gives_up_after_exhausting_retries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """After exhausting all attempts, the last error is raised, not an infinite retry."""
    attempts = 0

    async def always_fails(
        host: str, port: int
    ) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        nonlocal attempts
        attempts += 1
        raise ConnectionRefusedError("simulated permanent failure")

    monkeypatch.setattr(asyncio, "open_connection", always_fails)
    connector = OutboundConnector(
        None,
        frozenset(),
        _TIMEOUT_S,
        retry_max_attempts=3,
        retry_backoff_base_s=0.01,
        retry_backoff_max_s=0.01,
    )

    with pytest.raises(TunnelError, match="failed"):
        await connector.open("127.0.0.1", 1)

    assert attempts == 3


async def test_tls_handshake_times_out_on_a_silent_server() -> None:
    """An upstream that accepts TCP but never answers the TLS handshake does not hang forever."""
    release = asyncio.Event()

    async def handle(_reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await release.wait()
        writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    connector = OutboundConnector(None, frozenset(), _TIMEOUT_S, tls_handshake_timeout_s=0.1)

    with pytest.raises(TunnelError, match="TLS handshake"):
        await connector.open_tls("127.0.0.1", port, ssl.create_default_context())

    release.set()
    server.close()
    await server.wait_closed()
