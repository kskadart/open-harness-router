"""Forward-proxy TCP server and entry point for the mode.

The router listens on a separate port as an HTTP proxy: it accepts
``CONNECT``, terminates TLS with its own certificate for hosts on the
allowlist and handles requests with the same provider registry as ASGI
mode, and remains a blind tunnel for every other host.

The mode exists because of a single client limitation: Claude Code disables
Remote Control when ``ANTHROPIC_BASE_URL`` points somewhere other than
``api.anthropic.com``, but it tolerates the ``HTTPS_PROXY`` variable. So the
client is started without overriding the base URL, and routing happens at
the proxy level -- and everything except ``/v1/messages`` and
``/v1/messages/count_tokens`` must reach the real upstream unmodified.

In production it is not started as a separate process, but by the combined
entry point ``entrypoint.run`` together with the ASGI listener, sharing one
``ProviderRegistry`` (``make run-proxy``, ``ROUTER_PROXY_ENABLED=true``) --
see ``ForwardProxyServer.serve_forever`` for how signal and registry
ownership is split between the modes. The standalone launch
(``PYTHONPATH=src python -m proxy.server``, the ``main`` function below)
remains functional for debugging proxy logic in isolation from the ASGI
listener, but is no longer the user-facing way to run it.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import signal
import ssl
import sys
import time
from contextlib import suppress

from errors import anthropic_error_body
from log import get_logger
from main import build_runtime
from proxy.certificates import CertificateAuthority, CertificateAuthorityError
from proxy.connect import ConnectRequest, ConnectRequestError, read_connect_request
from proxy.outbound import (
    OutboundConfigError,
    OutboundConnector,
    TunnelError,
    parse_upstream_proxy,
)
from proxy.session import MitmHttpSession, SessionTimeouts
from proxy.streams import close_stream, pump_tunnel
from proxy.tls import build_leaf_tls_context, build_upstream_tls_context
from routing.registry import ProviderRegistry
from settings import Settings

logger = get_logger(__name__)

_CONNECT_ESTABLISHED = b"HTTP/1.1 200 Connection Established\r\n\r\n"

# Stream buffer limit: it also bounds the length of the CONNECT prefix,
# which is read up to the separator.
_STREAM_LIMIT_BYTES = 64 * 1024

_HEADER_ENCODING = "latin-1"
_JSON_CONTENT_TYPE = "application/json"

_METHOD_NOT_ALLOWED_STATUS = 405
_BAD_GATEWAY_STATUS = 502

_CONNECT_ONLY_MESSAGE = "This port accepts only CONNECT requests."
_TUNNEL_FAILED_MESSAGE = "Could not establish a tunnel to the requested host."

# CONNECT headers (including Proxy-Authorization) are parsed in
# proxy.connect.ConnectRequest.headers, but never verified anywhere --
# binding to something other than loopback turns the port into an open
# relay: anyone on the network gets a blind tunnel to an arbitrary
# host:port, and MITM hosts are served by the provider registry, billed on
# the owner's api_key_env.
_INSECURE_BIND_WARNING = (
    "proxy is bound to a non-loopback host: anyone on the network can open a "
    "blind tunnel through this port, and requests to MITM hosts are billed on "
    "the owner's api_key_env, because Proxy-Authorization from CONNECT headers "
    "is parsed but never verified"
)

# Signals on which the process stops gracefully. SIGTERM is sent by
# launchd, systemd, and docker stop; without an explicit handler it kills
# the process instantly, and the registry's httpx clients are left unclosed.
_SHUTDOWN_SIGNALS = (signal.SIGTERM, signal.SIGINT)

# Wait limit for active connections on shutdown. A streaming response can
# last minutes, so waiting for it to finish fully is not an option: once
# the deadline elapses, remaining connections are cancelled.
_DRAIN_TIMEOUT_S = 10.0

_STATUS_REASONS = {
    _METHOD_NOT_ALLOWED_STATUS: "Method Not Allowed",
    _BAD_GATEWAY_STATUS: "Bad Gateway",
}


def _is_loopback_host(host: str) -> bool:
    """Check whether ``host`` restricts the proxy port to the local interface.

    Args:
        host: value of ``ProxySettings.host``, the interface the proxy
            listens on.

    Returns:
        True for ``localhost`` and IP addresses in the loopback range
        (127.0.0.0/8, ``::1``). False for everything else, including
        ``0.0.0.0`` and ``::`` -- those make the port reachable from the
        network.
    """
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _plain_error_response(status_code: int, error_type: str, message: str) -> bytes:
    """Build an error response for a connection where HTTP has not started yet.

    The response is assembled by hand: the error occurs before TLS
    termination, so there is no h11 state machine on the connection yet.

    Args:
        status_code: HTTP status of the response.
        error_type: error type in Anthropic's format.
        message: human-readable message.

    Returns:
        Ready-made response bytes together with the body.
    """
    body = json.dumps(anthropic_error_body(error_type, message)).encode()
    head = (
        f"HTTP/1.1 {status_code} {_STATUS_REASONS[status_code]}\r\n"
        f"content-type: {_JSON_CONTENT_TYPE}\r\n"
        f"content-length: {len(body)}\r\n"
        "connection: close\r\n\r\n"
    ).encode(_HEADER_ENCODING)
    return head + body


def _log_tls_handshake_failed(host: str, exc: Exception) -> None:
    """Log a failed TLS context build or handshake itself for a MITM host.

    Args:
        host: host from the ``CONNECT`` request.
        exc: exception that interrupted the context build or handshake.
    """
    logger.warning("proxy_tls_handshake_failed", host=host, error_type=type(exc).__name__)


async def _write(writer: asyncio.StreamWriter, data: bytes) -> None:
    """Write bytes to a stream and wait for them to be sent.

    Args:
        writer: write stream.
        data: bytes to send.
    """
    writer.write(data)
    await writer.drain()


class ForwardProxyServer:
    """Forward-proxy: accepts CONNECT, MITM for the allowlist, blind tunnel for the rest."""

    def __init__(self, settings: Settings, registry: ProviderRegistry) -> None:
        """Prepare PKI, TLS contexts, and the outbound connector.

        The constructor is blocking (key generation, reading and writing
        PEM files), and must be called before the event loop starts -- such
        work has no place in a connection handler. This also removes
        certificate generation from the first-request path. Leaf
        certificates, however, do not live forever (see
        ``certificates._LEAF_VALIDITY``): their reissue as the expiry date
        approaches can happen later, from a connection handler -- see
        :meth:`_tls_context_for_host`, called there via
        ``asyncio.to_thread``.

        Args:
            settings: application settings.
            registry: provider registry for routed paths.

        Raises:
            CertificateAuthorityError: if the CA material on disk is
                corrupted.
            OutboundConfigError: if the upstream proxy address is invalid.
        """
        self._settings = settings
        self._registry = registry
        self._mitm_hosts = settings.proxy.mitm_host_set()
        # Connection-handling tasks: needed so that, on shutdown, current
        # requests can be waited on instead of being cut off at the socket
        # level.
        self._connections: set[asyncio.Task[None]] = set()

        self._authority = CertificateAuthority(settings.proxy.ca_dir)
        self._root_certificate_path = self._authority.root_certificate_path()
        # Cache of TLS contexts by host, together with the serial_number of
        # the certificate the context was built from: since
        # CertificateAuthority.leaf_certificate_for_host can now reissue a
        # certificate at any moment (not only at startup), a context frozen
        # once in the dict would go stale along with the expired
        # certificate. serial_number is a cheap way to tell that a cached
        # context is still built from the current certificate, without
        # rebuilding it on every connection. No separate lock is needed for
        # the dict itself: the only source of a race -- certificate
        # generation -- is already serialized by the lock inside
        # CertificateAuthority, and dict reads/writes are atomic
        # individually in CPython -- in the worst case, a concurrent
        # context rebuild for a just-expired certificate happens twice
        # harmlessly, without corrupting state.
        self._tls_contexts: dict[str, tuple[int, ssl.SSLContext]] = {}
        for hostname in self._mitm_hosts:
            self._tls_context_for_host(hostname)
        self._upstream_tls = build_upstream_tls_context(settings.proxy.upstream_ca_bundle)

        upstream_proxy_url = settings.proxy.upstream_proxy_url
        self._connector = OutboundConnector(
            parse_upstream_proxy(upstream_proxy_url) if upstream_proxy_url else None,
            settings.proxy.no_proxy_host_set(),
            settings.proxy.connect_timeout_s,
            settings.proxy.tls_handshake_timeout_s,
            settings.proxy.retry_max_attempts,
            settings.proxy.retry_backoff_base_s,
            settings.proxy.retry_backoff_max_s,
        )
        self._upstream_proxy_configured = upstream_proxy_url is not None
        self._tls_handshake_timeout_s = settings.proxy.tls_handshake_timeout_s
        self._client_request_timeout_s = settings.proxy.client_request_timeout_s
        self._idle_timeout_s = settings.proxy.idle_timeout_s
        self._session_timeouts = SessionTimeouts(
            client_request_s=settings.proxy.client_request_timeout_s,
            upstream_headers_s=settings.proxy.upstream_headers_timeout_s,
            idle_s=settings.proxy.idle_timeout_s,
        )

    def _tls_context_for_host(self, hostname: str) -> ssl.SSLContext:
        """Build a TLS context for a MITM host, current as of the call.

        Unlike a dict built once in the constructor in a previous version,
        the host's certificate is re-checked for expiry on every call here
        (see ``CertificateAuthority.leaf_certificate_for_host``) -- the
        context is rebuilt only if the certificate was reissued, otherwise
        the cached one is returned. The method is blocking (key generation
        on reissue, writing a temp file for ``load_cert_chain``): when
        called from a connection handler it must be wrapped in
        ``asyncio.to_thread`` so as not to block the event loop -- in the
        constructor (before the loop starts) it is called directly.

        Args:
            hostname: lowercased allowlist hostname.

        Returns:
            Server TLS context with the current leaf certificate.
        """
        certificate, _private_key = self._authority.leaf_certificate_for_host(hostname)
        cached = self._tls_contexts.get(hostname)
        if cached is not None and cached[0] == certificate.serial_number:
            return cached[1]

        context = build_leaf_tls_context(self._authority, hostname)
        self._tls_contexts[hostname] = (certificate.serial_number, context)
        return context

    async def start(self) -> asyncio.Server:
        """Bring up the listening socket for the proxy port.

        Returns:
            The started asyncio server; the caller is responsible for
            closing it.
        """
        if not _is_loopback_host(self._settings.proxy.host):
            logger.warning(
                "proxy_insecure_bind",
                host=self._settings.proxy.host,
                port=self._settings.proxy.port,
                risk=_INSECURE_BIND_WARNING,
            )
        server = await asyncio.start_server(
            self._handle_client,
            host=self._settings.proxy.host,
            port=self._settings.proxy.port,
            limit=_STREAM_LIMIT_BYTES,
        )
        logger.info(
            "proxy_startup",
            host=self._settings.proxy.host,
            port=self._settings.proxy.port,
            mitm_hosts=sorted(self._mitm_hosts),
            # The upstream proxy URL may contain credentials, so only the
            # fact that it is configured goes into the log.
            upstream_proxy_configured=self._upstream_proxy_configured,
            root_certificate=str(self._root_certificate_path),
            routes=self._registry.describe_routes(),
        )
        return server

    async def serve_forever(
        self,
        *,
        stop_requested: asyncio.Event | None = None,
        close_registry: bool = True,
    ) -> None:
        """Accept connections on the proxy port until a stop signal.

        Unlike ASGI mode, where uvicorn registers the signal handlers, in a
        standalone launch (``stop_requested`` not passed) this is done
        manually: by default SIGTERM kills the process instantly, the
        ``finally`` block does not run, and the registry's httpx clients
        are left unclosed.

        In the combined process (see ``entrypoint.run``), where an ASGI
        listener is already running alongside, only the entry point handles
        signals: a handler here via ``loop.add_signal_handler`` must not be
        installed -- only one handler can be registered for a given signal
        in a process, and a repeat registration either overwrites someone
        else's or is overwritten by it (order is unpredictable). The
        built-in signal handler of ``uvicorn.Server`` itself does not fit
        either: after stopping, it re-raises the caught signal via
        ``signal.raise_signal``, and with the OS's default handler
        (``SIG_DFL``) that kills the process before the forward-proxy
        manages to finish (see the ``entrypoint._CoordinatedServer``
        docstring). So in combined mode the caller passes an already-ready
        ``stop_requested`` -- the server just waits on it, without touching
        signals itself.

        In the combined process the provider registry is shared with the
        second listener and is closed by the caller exactly once, after
        both listeners have finished -- so ``close_registry=False`` disables
        closing it here.

        Args:
            stop_requested: external stop event. ``None`` (the default) --
                standalone launch: the method creates the event itself and
                installs the SIGTERM/SIGINT handlers itself, as before.
            close_registry: whether to close the provider registry on
                shutdown. ``False`` -- in the combined process, where the
                entry point owns the registry.
        """
        server = await self.start()
        loop = asyncio.get_running_loop()
        owns_signals = stop_requested is None
        if stop_requested is None:
            stop_requested = asyncio.Event()
        handled_signals: list[signal.Signals] = []
        if owns_signals:
            for signal_number in _SHUTDOWN_SIGNALS:
                try:
                    loop.add_signal_handler(signal_number, stop_requested.set)
                except NotImplementedError:
                    # Platform without event loop signal handler support:
                    # fall back to the interpreter's default behavior.
                    continue
                handled_signals.append(signal_number)

        try:
            async with server:
                serving = asyncio.create_task(server.serve_forever())
                await stop_requested.wait()
                logger.info("proxy_stopping")
                serving.cancel()
                with suppress(asyncio.CancelledError):
                    await serving
        finally:
            for signal_number in handled_signals:
                loop.remove_signal_handler(signal_number)
            await self._drain_connections()
            if close_registry:
                await self._registry.close_all()
            logger.info("proxy_shutdown")

    async def _drain_connections(self) -> None:
        """Wait for active connections, cancelling those that miss the deadline."""
        pending = set(self._connections)
        if not pending:
            return
        logger.info("proxy_draining", connections=len(pending))
        _, unfinished = await asyncio.wait(pending, timeout=_DRAIN_TIMEOUT_S)
        for task in unfinished:
            task.cancel()
        if unfinished:
            logger.warning("proxy_drain_timeout", connections=len(unfinished))
            await asyncio.gather(*unfinished, return_exceptions=True)

    async def _handle_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        """Serve a single client connection under an error barrier.

        Args:
            reader: read stream from the client.
            writer: write stream to the client.
        """
        connection = asyncio.current_task()
        if connection is not None:
            self._connections.add(connection)
        try:
            try:
                request = await read_connect_request(reader, self._client_request_timeout_s)
            except ConnectRequestError as exc:
                logger.warning("proxy_connect_rejected", reason=str(exc))
                await _write(
                    writer,
                    _plain_error_response(
                        _METHOD_NOT_ALLOWED_STATUS,
                        "invalid_request_error",
                        _CONNECT_ONLY_MESSAGE,
                    ),
                )
                return

            if request.host.lower() in self._mitm_hosts:
                await self._serve_mitm(reader, writer, request)
            else:
                await self._serve_tunnel(reader, writer, request)
        except Exception as exc:  # noqa: BLE001 - connection barrier, see below
            # Connections are served by tasks from asyncio.start_server: an
            # exception that escaped would go unhandled in the event loop.
            # The barrier ends only the current connection, the server keeps
            # accepting new ones.
            logger.exception("proxy_connection_failed", error=str(exc))
        finally:
            await close_stream(writer)
            if connection is not None:
                self._connections.discard(connection)

    async def _serve_tunnel(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        request: ConnectRequest,
    ) -> None:
        """Serve a host outside the allowlist with a blind tunnel, without decryption.

        Args:
            reader: read stream from the client.
            writer: write stream to the client.
            request: parsed ``CONNECT`` request.
        """
        try:
            upstream_reader, upstream_writer = await self._connector.open(
                request.host, request.port
            )
        except TunnelError as exc:
            logger.warning(
                "proxy_tunnel_failed",
                host=request.host,
                port=request.port,
                reason=str(exc),
            )
            await _write(
                writer,
                _plain_error_response(
                    _BAD_GATEWAY_STATUS, "api_error", _TUNNEL_FAILED_MESSAGE
                ),
            )
            return

        await _write(writer, _CONNECT_ESTABLISHED)
        started_at = time.perf_counter()
        await pump_tunnel(
            reader, writer, upstream_reader, upstream_writer, self._idle_timeout_s
        )
        logger.info(
            "proxy_tunnel_closed",
            host=request.host,
            port=request.port,
            via_upstream_proxy=self._connector.uses_upstream_proxy(request.host),
            duration_ms=round((time.perf_counter() - started_at) * 1000, 1),
        )

    async def _serve_mitm(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        request: ConnectRequest,
    ) -> None:
        """Terminate TLS for an allowlisted host and serve its requests.

        Args:
            reader: read stream from the client.
            writer: write stream to the client.
            request: parsed ``CONNECT`` request.
        """
        # The context is built BEFORE responding to CONNECT, not after. Per
        # the protocol, the client must wait for a 2xx and only then start
        # TLS -- but that only holds as long as there is no await on
        # unrelated work between sending the 2xx and start_tls().
        # asyncio.to_thread here is exactly such an await: while it runs,
        # the event loop keeps reading the socket with the old (not yet
        # TLS) protocol, and if the client manages to send ClientHello in
        # that window, those bytes would land in the old reader's buffer
        # and never reach the new TLS layer -- the handshake would hang
        # until the client's timeout. By ordering the await before writing
        # the 2xx, rather than after, we guarantee the client physically
        # cannot have sent ClientHello yet: on its side it must first
        # receive and parse our response.
        try:
            context = await asyncio.to_thread(self._tls_context_for_host, request.host.lower())
        except (ssl.SSLError, OSError) as exc:
            _log_tls_handshake_failed(request.host, exc)
            return

        await _write(writer, _CONNECT_ESTABLISHED)
        try:
            # asyncio.wait_for on a bare coroutine (start_tls is not yet a
            # Task) does not add a scheduler switch point BEFORE the first
            # await inside it: compare with asyncio.to_thread above, which
            # hands work to a separate thread and therefore must wait its
            # turn in the event loop. Here start_tls runs synchronously up
            # to its own internal pause_reading() -- the invariant from the
            # comment above is not violated, the timeout only aborts an
            # already-running handshake.
            await asyncio.wait_for(
                writer.start_tls(context), timeout=self._tls_handshake_timeout_s
            )
        except (ssl.SSLError, OSError) as exc:
            _log_tls_handshake_failed(request.host, exc)
            return

        session = MitmHttpSession(
            reader,
            writer,
            request,
            self._registry,
            self._connector,
            self._upstream_tls,
            self._session_timeouts,
        )
        await session.serve()


def main() -> None:
    """Run the router in forward-proxy mode.

    Raises:
        SystemExit: if the CA material is corrupted or the upstream proxy
            address is invalid -- there is nothing to continue running with
            in these cases.
    """
    settings, registry = build_runtime()
    try:
        server = ForwardProxyServer(settings, registry)
    except (CertificateAuthorityError, OutboundConfigError) as exc:
        sys.exit(f"open-harness-router: {exc}")
    asyncio.run(server.serve_forever())


if __name__ == "__main__":
    main()
