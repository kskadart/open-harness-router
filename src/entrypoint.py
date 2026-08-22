"""Combined entrypoint: ASGI listener plus an optional forward-proxy.

The two modes used to run as two independent processes (``uvicorn
main:create_app --factory`` and ``python -m proxy.server``), each building
its own ``ProviderRegistry`` -- i.e. two httpx client pools for the same
upstreams. Here both listeners run in one process, in one event loop
(``asyncio.TaskGroup``), on one registry: ``build_runtime`` is called once,
and the result is handed to both ``create_app`` and ``ForwardProxyServer``.

Which listeners to start is not decided by a separate mode but by
``ProxySettings.enabled``: the ASGI listener is always needed (cheap, gets
in no one's way), while the forward-proxy is optional -- it requires the
client to separately trust a self-signed CA (``NODE_EXTRA_CA_CERTS``, see
README), so it's enabled on request and off by default, to avoid changing
the behavior of an already running service (the user's LaunchAgent runs
``uvicorn main:create_app --factory`` directly -- this module doesn't touch
that).

The provider registry is built and closed right here, exactly once, after
both listeners have finished -- neither ``create_app`` (see its docstring on
``owns_registry``) nor ``ForwardProxyServer.serve_forever`` (see
``close_registry=False``) owns a registry passed in from outside.

Only the entrypoint installs SIGTERM/SIGINT handlers in the combined
process, not uvicorn. It would seem natural to rely on the stock
``uvicorn.Server.serve()`` (it calls ``signal.signal`` under the hood
itself), but it has a side effect that's invisible when uvicorn runs as the
sole process: after a normal stop, ``capture_signals()`` restores the
previous signal handler and then RE-RAISES the caught signal via
``signal.raise_signal`` -- built on the assumption that uvicorn is alone in
the process and an external supervisor needs to see a real SIGTERM. The
handler in place before startup is ``SIG_DFL`` (verified:
``signal.getsignal(signal.SIGTERM) == 0``), so the re-raised signal
terminates the process the OS-standard way -- killing it before the
forward-proxy running in the same event loop can shut down cleanly.
That's why :class:`_CoordinatedServer` turns ``capture_signals`` into a
no-op (it doesn't touch OS signals at all), and stopping both listeners is
instead coordinated by a shared ``asyncio.Event``, set by the single
handler that the entrypoint itself installs.

Run: ``python -m entrypoint`` (see ``make run-proxy``, which sets
``ROUTER_PROXY_ENABLED=true``).
"""

from __future__ import annotations

import asyncio
import contextlib
import signal
from collections.abc import Iterator

import uvicorn

from log import get_logger
from main import build_runtime, create_app
from proxy.certificates import CertificateAuthorityError
from proxy.outbound import OutboundConfigError
from proxy.server import ForwardProxyServer

logger = get_logger(__name__)

# Normal shutdown signals -- the same ones ForwardProxyServer listens for
# when running standalone (``proxy.server._SHUTDOWN_SIGNALS``).
_SHUTDOWN_SIGNALS = (signal.SIGTERM, signal.SIGINT)


class _CoordinatedServer(uvicorn.Server):
    """``uvicorn.Server`` without its own OS signal capture.

    See the rationale in the module docstring: in the combined process
    signals are handled only by the entrypoint, not by ``capture_signals``
    -- otherwise uvicorn's shutdown could re-raise the signal with the
    restored default handler (``SIG_DFL``) and kill the process before the
    second listener finishes.
    """

    def __init__(self, config: uvicorn.Config, stop_requested: asyncio.Event) -> None:
        """Bind a uvicorn config to the shared stop event.

        Args:
            config: ASGI server configuration.
            stop_requested: event set by the entrypoint's signal handler --
                the forward-proxy waits on it, if it's running alongside.
        """
        super().__init__(config)
        self._stop_requested = stop_requested

    @contextlib.contextmanager
    def capture_signals(self) -> Iterator[None]:
        """Do not capture OS signals -- the entrypoint handles that."""
        yield

    def request_stop(self) -> None:
        """Stop the server and set the shared stop event.

        Called from the signal handler installed by the entrypoint
        (``entrypoint.run``), not automatically on signal receipt -- see
        ``capture_signals``.
        """
        self._stop_requested.set()
        self.should_exit = True


async def run() -> None:
    """Start the ASGI listener and, if enabled, the forward-proxy in one process.

    Raises:
        SystemExit: see ``build_runtime``, or on corrupt CA material or an
            invalid upstream proxy address for the forward-proxy.
    """
    settings, registry = build_runtime()
    try:
        stop_requested = asyncio.Event()

        app = create_app(settings=settings, registry=registry)
        config = uvicorn.Config(
            app=app,
            host=settings.server.host,
            port=settings.server.port,
            # setup_logging (inside build_runtime, already ran above) configured
            # structlog/JSON for the whole process -- uvicorn's own
            # logging.dictConfig (otherwise run when Config is created, before
            # this line) would overwrite it with a default colored console
            # formatter.
            log_config=None,
        )
        server = _CoordinatedServer(config, stop_requested)

        proxy_server: ForwardProxyServer | None = None
        if settings.proxy.enabled:
            try:
                proxy_server = ForwardProxyServer(settings, registry)
            except (CertificateAuthorityError, OutboundConfigError) as exc:
                raise SystemExit(f"open-harness-router: {exc}") from exc

        loop = asyncio.get_running_loop()
        handled_signals: list[signal.Signals] = []
        for signal_number in _SHUTDOWN_SIGNALS:
            try:
                loop.add_signal_handler(signal_number, server.request_stop)
            except NotImplementedError:
                # Platform without event-loop signal handler support: falls
                # back to the interpreter's default behavior.
                continue
            handled_signals.append(signal_number)

        try:
            async with asyncio.TaskGroup() as tg:
                tg.create_task(server.serve())
                if proxy_server is not None:
                    tg.create_task(
                        proxy_server.serve_forever(
                            stop_requested=stop_requested, close_registry=False
                        )
                    )
        finally:
            for signal_number in handled_signals:
                loop.remove_signal_handler(signal_number)
    finally:
        await registry.close_all()
        logger.info("entrypoint_shutdown")


def main() -> None:
    """Run the combined entrypoint."""
    asyncio.run(run())


if __name__ == "__main__":
    main()
