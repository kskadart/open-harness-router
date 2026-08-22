"""Establishing forward-proxy outbound connections.

The module handles a single question: how the router itself reaches an
external host. In a corporate network, direct outbound access may be
closed, in which case the connection is built as a chain of
``client -> router -> corporate proxy -> internet``: the router itself acts
as an HTTP proxy client and sends its own ``CONNECT``. Without an upstream
proxy in the settings, behavior is unchanged -- a direct TCP connection.

The scope is limited to raw connections opened by the forward-proxy itself:
a blind tunnel for hosts outside the allowlist, and transparent passthrough
of non-routed paths on a MITM host. Provider outbound requests go through
their own httpx clients and are not part of this chain.
"""

from __future__ import annotations

import asyncio
import base64
import ssl
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TypeVar
from urllib.parse import urlsplit

from log import get_logger
from services.retry import exponential_delays, retry_connect

logger = get_logger(__name__)

_ConnectResult = TypeVar("_ConnectResult")

_HEAD_TERMINATOR = b"\r\n\r\n"
_LINE_SEPARATOR = "\r\n"
_HEADER_ENCODING = "latin-1"
_STATUS_LINE_PARTS = 3

_HTTP_SCHEME = "http"
_DEFAULT_PROXY_PORT = 3128

_SUCCESS_MIN_STATUS = 200
_SUCCESS_MAX_STATUS = 299


class OutboundConfigError(Exception):
    """Malformed upstream proxy address in settings."""


class TunnelError(Exception):
    """Failed to establish an outbound connection to the target."""


def _format_authority(host: str, port: int) -> str:
    """Build a target of the form ``host:port``, accounting for IPv6 literals.

    Args:
        host: hostname or IP literal.
        port: target port.

    Returns:
        Target string; an IPv6 literal is wrapped in square brackets.
    """
    return f"[{host}]:{port}" if ":" in host else f"{host}:{port}"


@dataclass(frozen=True, slots=True)
class UpstreamProxy:
    """Parsed upstream HTTP proxy address.

    Attributes:
        host: proxy hostname.
        port: proxy port.
        authorization: ready-to-use ``Proxy-Authorization`` header value if
            credentials were present in the URL, otherwise None.
    """

    host: str
    port: int
    authorization: str | None


def parse_upstream_proxy(url: str) -> UpstreamProxy:
    """Parse the upstream proxy URL from settings.

    Args:
        url: address of the form ``http://proxy.corp.internal:3128``;
            credentials are allowed (``http://user:secret@proxy:3128``) --
            they are turned into a ``Proxy-Authorization`` header and never
            end up in the log.

    Returns:
        Parsed upstream proxy address.

    Raises:
        OutboundConfigError: if the scheme is not ``http`` or the host is
            missing. HTTPS to the proxy itself is intentionally not
            supported: corporate proxies accept ``CONNECT`` over plain
            HTTP, and supporting a second TLS layer would complicate the
            chain without practical benefit.
    """
    parts = urlsplit(url)
    if parts.scheme != _HTTP_SCHEME:
        raise OutboundConfigError(
            f"unsupported upstream proxy scheme {parts.scheme!r}, expected 'http'"
        )
    if not parts.hostname:
        raise OutboundConfigError("upstream proxy URL has no host")

    authorization: str | None = None
    if parts.username:
        credentials = f"{parts.username}:{parts.password or ''}".encode(_HEADER_ENCODING)
        authorization = f"Basic {base64.b64encode(credentials).decode('ascii')}"

    return UpstreamProxy(
        host=parts.hostname,
        port=parts.port or _DEFAULT_PROXY_PORT,
        authorization=authorization,
    )


def _parse_connect_status(head: bytes) -> int:
    """Extract the status code from the upstream proxy's response to ``CONNECT``.

    Args:
        head: raw proxy response up to and including the empty line.

    Returns:
        HTTP response code.

    Raises:
        TunnelError: if the status line does not parse.
    """
    status_line = head.decode(_HEADER_ENCODING).split(_LINE_SEPARATOR)[0]
    parts = status_line.split(" ", _STATUS_LINE_PARTS - 1)
    if len(parts) < 2 or not parts[1].isdigit():
        raise TunnelError("malformed status line from the upstream proxy")
    return int(parts[1])


class OutboundConnector:
    """Opens outbound connections directly or via an upstream proxy."""

    def __init__(
        self,
        upstream_proxy: UpstreamProxy | None,
        no_proxy_hosts: frozenset[str],
        connect_timeout_s: float,
        tls_handshake_timeout_s: float = 10.0,
        retry_max_attempts: int = 1,
        retry_backoff_base_s: float = 0.25,
        retry_backoff_max_s: float = 2.0,
    ) -> None:
        """Initialize the connector.

        Args:
            upstream_proxy: upstream proxy address, or None for direct
                connections.
            no_proxy_hosts: lowercased hosts that always go direct, even
                when an upstream proxy is configured.
            connect_timeout_s: maximum time to establish a connection and
                wait for the upstream proxy's response, in seconds.
            tls_handshake_timeout_s: maximum time for the TLS handshake with
                the target.
            retry_max_attempts: total number of attempts (including the
                first) to establish a connection to the target before
                surfacing the last error to the caller. Default is 1 -- no
                retries: production passes the value from settings
                explicitly, one here preserves the behavior of existing
                calls without retries (tests, places where a retry is not
                needed).
            retry_backoff_base_s: base delay before the first retry.
            retry_backoff_max_s: ceiling on the delay between retries.
        """
        self._upstream_proxy = upstream_proxy
        self._no_proxy_hosts = no_proxy_hosts
        self._connect_timeout_s = connect_timeout_s
        self._tls_handshake_timeout_s = tls_handshake_timeout_s
        self._retry_max_attempts = retry_max_attempts
        self._retry_delays = exponential_delays(
            retry_backoff_base_s, retry_backoff_max_s, retry_max_attempts - 1
        )

    def uses_upstream_proxy(self, host: str) -> bool:
        """Determine whether the connection to the host goes via an upstream proxy.

        Args:
            host: target hostname.

        Returns:
            True if an upstream proxy is configured and the host is not in
            the direct-exclusion list.
        """
        return self._upstream_proxy is not None and host.lower() not in self._no_proxy_hosts

    async def open(
        self, host: str, port: int
    ) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        """Open a TCP connection to the target, via a proxy if configured.

        The connection is established before ``200 Connection Established``
        is sent to the client: the target being unreachable must turn into
        a failure at the proxy level, not into a break in an already
        "established" tunnel. Failure of this step is retried (see
        :meth:`_with_retry`) -- it always happens before a single byte has
        gone out to either the client or the upstream, so retrying here is
        safe without buffering anything.

        Args:
            host: hostname or IP literal of the target.
            port: target port.

        Returns:
            Pair of read/write streams leading to the target.

        Raises:
            TunnelError: if the connection is not established, does not
                fit within the timeout, or the upstream proxy refused the
                tunnel -- after all retries are exhausted.
        """
        return await self._with_retry(host, port, lambda: self._connect_once(host, port))

    async def open_tls(
        self, host: str, port: int, context: ssl.SSLContext
    ) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        """Open a TLS connection to the target, via a proxy if configured.

        TLS is layered on top of an already-established connection the same
        way for a direct exit and for a tunnel through an upstream proxy,
        so the certificate is verified against the final target's name, not
        the proxy's. The retry covers the whole chain -- the TCP/CONNECT
        tunnel and the TLS handshake itself -- atomically in one attempt:
        a handshake failure on an already-established socket does not retry
        the handshake on that same socket, it reopens the connection from
        scratch.

        Args:
            host: target hostname (also the expected name in the
                certificate).
            port: target port.
            context: client TLS context.

        Returns:
            Pair of read/write streams with TLS established.

        Raises:
            TunnelError: if the connection is not established or the TLS
                handshake fails (including due to an untrusted certificate
                or a timeout) -- after all retries are exhausted.
        """
        return await self._with_retry(
            host, port, lambda: self._connect_tls_once(host, port, context)
        )

    async def _connect_once(
        self, host: str, port: int
    ) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        """Perform a single attempt at establishing a TCP connection to the target.

        Args:
            host: hostname or IP literal of the target.
            port: target port.

        Returns:
            Pair of read/write streams leading to the target.

        Raises:
            TunnelError: if the connection is not established, does not fit
                within the timeout, or the upstream proxy refused the
                tunnel.
        """
        proxy = self._upstream_proxy
        if proxy is None or host.lower() in self._no_proxy_hosts:
            return await self._open_direct(host, port)

        reader, writer = await self._open_direct(proxy.host, proxy.port)
        try:
            await self._request_tunnel(reader, writer, proxy, host, port)
        except (TunnelError, OSError):
            writer.close()
            raise
        return reader, writer

    async def _connect_tls_once(
        self, host: str, port: int, context: ssl.SSLContext
    ) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        """Perform a single attempt at establishing a TLS connection to the target.

        Args:
            host: target hostname (also the expected name in the
                certificate).
            port: target port.
            context: client TLS context.

        Returns:
            Pair of read/write streams with TLS established.

        Raises:
            TunnelError: if the connection is not established or the TLS
                handshake fails, including on timeout (``TimeoutError`` is
                a subclass of ``OSError``, no separate branch is needed for
                it).
        """
        reader, writer = await self._connect_once(host, port)
        try:
            await asyncio.wait_for(
                writer.start_tls(context, server_hostname=host),
                timeout=self._tls_handshake_timeout_s,
            )
        except (ssl.SSLError, OSError) as exc:
            writer.close()
            raise TunnelError(f"TLS handshake with {host}:{port} failed") from exc
        return reader, writer

    async def _with_retry(
        self,
        host: str,
        port: int,
        attempt: Callable[[], Awaitable[_ConnectResult]],
    ) -> _ConnectResult:
        """Retry establishing a connection with exponential backoff.

        A retry is acceptable only at this step: at the time of the call,
        not a single byte has gone out to either the client or the
        upstream, so every attempt starts from a clean slate and requires
        no buffering for a retry.

        Args:
            host: target hostname -- for logging only.
            port: target port -- for logging only.
            attempt: coroutine factory for a single connection attempt.

        Returns:
            The result of the first successful attempt.

        Raises:
            TunnelError: if all attempts are exhausted and the last error
                was a TunnelError.
            OSError: if all attempts are exhausted and the last error was
                not converted to a TunnelError (see ``_request_tunnel``).
        """
        return await retry_connect(
            attempt,
            delays=self._retry_delays,
            retryable=(TunnelError, OSError),
            on_retry=lambda exc, attempt_no, delay: logger.warning(
                "proxy_outbound_retry",
                host=host,
                port=port,
                attempt=attempt_no,
                max_attempts=self._retry_max_attempts,
                delay_s=delay,
                error_type=type(exc).__name__,
            ),
        )

    async def _open_direct(
        self, host: str, port: int
    ) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        """Open a direct TCP connection to the given address.

        Args:
            host: hostname or IP literal.
            port: port.

        Returns:
            Pair of read/write streams.

        Raises:
            TunnelError: if the host does not resolve, the connection is
                refused, or it is not established within the allotted
                time.
        """
        try:
            return await asyncio.wait_for(
                asyncio.open_connection(host, port), timeout=self._connect_timeout_s
            )
        except TimeoutError as exc:
            raise TunnelError(f"connection to {host}:{port} timed out") from exc
        except OSError as exc:
            raise TunnelError(f"connection to {host}:{port} failed") from exc

    async def _request_tunnel(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        proxy: UpstreamProxy,
        host: str,
        port: int,
    ) -> None:
        """Request a tunnel to the target from the upstream proxy and await the response.

        Args:
            reader: read stream from the upstream proxy.
            writer: write stream to the upstream proxy.
            proxy: upstream proxy address and credentials.
            host: hostname of the final target.
            port: port of the final target.

        Raises:
            TunnelError: if the proxy responded with a non-2xx status,
                closed the connection, or did not respond in time.
        """
        authority = _format_authority(host, port)
        lines = [f"CONNECT {authority} HTTP/1.1", f"Host: {authority}"]
        if proxy.authorization is not None:
            lines.append(f"Proxy-Authorization: {proxy.authorization}")
        request = _LINE_SEPARATOR.join(lines) + _HEAD_TERMINATOR.decode(_HEADER_ENCODING)

        writer.write(request.encode(_HEADER_ENCODING))
        await writer.drain()

        try:
            head = await asyncio.wait_for(
                reader.readuntil(_HEAD_TERMINATOR), timeout=self._connect_timeout_s
            )
        except TimeoutError as exc:
            raise TunnelError("upstream proxy did not answer CONNECT in time") from exc
        except (asyncio.IncompleteReadError, asyncio.LimitOverrunError) as exc:
            raise TunnelError("upstream proxy closed the connection on CONNECT") from exc

        status = _parse_connect_status(head)
        if not _SUCCESS_MIN_STATUS <= status <= _SUCCESS_MAX_STATUS:
            raise TunnelError(f"upstream proxy refused CONNECT to {authority} with {status}")
