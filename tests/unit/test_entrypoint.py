"""Unit tests for the combined entry point (``entrypoint.run``).

Verify three things that cannot be seen individually in the ``proxy.server``
or ``main`` tests: which listeners come up depending on
``ProxySettings.enabled``, that both listeners share the SAME
``ProviderRegistry`` (not two separate ones), and that the registry is
closed exactly once, after both listeners have finished -- not earlier.

The stop signal is not sent for real (``os.kill``): instead,
``_CoordinatedServer.request_stop`` is called directly on the intercepted
instance -- the same method the entry point's signal handler would call on
receiving SIGTERM, without risking crashing the test runner process. This is
exactly why entrypoint._CoordinatedServer does not capture OS signals
itself at all (``capture_signals`` is a no-op, see its docstring):
``uvicorn.Server``'s normal handler re-raises the caught signal via
``signal.raise_signal`` after stopping, and with the default OS handler that
would kill the test process.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

import entrypoint
from main import create_app as real_create_app
from proxy.server import ForwardProxyServer
from routing.config_loader import load_routing_config
from routing.registry import ProviderRegistry
from settings import Settings

_POLL_ATTEMPTS = 200


def _spy_close_all(registry: ProviderRegistry) -> list[int]:
    """Wrap the registry's ``close_all`` with a call counter without losing the original."""
    calls: list[int] = []
    original = registry.close_all

    async def counting_close_all() -> None:
        calls.append(1)
        await original()

    registry.close_all = counting_close_all  # type: ignore[method-assign]
    return calls


@pytest.fixture
def entrypoint_settings(
    _env: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Settings:
    """Entry point settings on free loopback ports (not 8787/8788)."""
    monkeypatch.setenv("ROUTER_SERVER_HOST", "127.0.0.1")
    monkeypatch.setenv("ROUTER_SERVER_PORT", "0")
    monkeypatch.setenv("ROUTER_PROXY_HOST", "127.0.0.1")
    monkeypatch.setenv("ROUTER_PROXY_PORT", "0")
    monkeypatch.setenv("ROUTER_PROXY_CA_DIR", str(tmp_path / "ca"))
    return Settings()


def _build_registry(settings: Settings) -> ProviderRegistry:
    """Build the registry from the test ``routing_test.yaml``."""
    return ProviderRegistry.build(load_routing_config(settings.routing.config_path), settings)


async def _poll_until(predicate: object, condition: str) -> None:
    """Wait for ``predicate()`` to become true, polling the event loop.

    Args:
        predicate: a no-argument readiness check callable.
        condition: description of the condition for the timeout error message.

    Raises:
        AssertionError: if the condition does not hold within ``_POLL_ATTEMPTS`` ticks.
    """
    for _ in range(_POLL_ATTEMPTS):
        if predicate():  # type: ignore[operator]
            return
        await asyncio.sleep(0.01)
    pytest.fail(f"timed out waiting for: {condition}")


async def test_run_starts_only_asgi_listener_when_proxy_disabled(
    entrypoint_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With the flag disabled, only the ASGI listener comes up."""
    settings = entrypoint_settings
    registry = _build_registry(settings)
    close_calls = _spy_close_all(registry)
    monkeypatch.setattr(entrypoint, "build_runtime", lambda: (settings, registry))

    proxy_instances: list[ForwardProxyServer] = []

    class _RecordingForwardProxyServer(ForwardProxyServer):
        def __init__(self, settings: Settings, registry: ProviderRegistry) -> None:
            super().__init__(settings, registry)
            proxy_instances.append(self)

    monkeypatch.setattr(entrypoint, "ForwardProxyServer", _RecordingForwardProxyServer)

    asgi_servers: list[entrypoint._CoordinatedServer] = []

    class _RecordingCoordinatedServer(entrypoint._CoordinatedServer):
        def __init__(self, config: object, stop_requested: asyncio.Event) -> None:
            super().__init__(config, stop_requested)  # type: ignore[arg-type]
            asgi_servers.append(self)

    monkeypatch.setattr(entrypoint, "_CoordinatedServer", _RecordingCoordinatedServer)

    task = asyncio.create_task(entrypoint.run())
    try:
        await _poll_until(lambda: asgi_servers and asgi_servers[0].started, "asgi server started")
        assert proxy_instances == []  # forward-proxy never came up at all

        asgi_servers[0].request_stop()
        assert close_calls == []  # registry not yet closed right after the signal
        await asyncio.wait_for(task, timeout=5.0)
    finally:
        if not task.done():
            task.cancel()

    assert close_calls == [1]


async def test_run_starts_both_listeners_on_one_registry_and_closes_it_once(
    entrypoint_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With the flag enabled, both listeners share one registry and close it once."""
    settings = entrypoint_settings
    settings.proxy.enabled = True
    registry = _build_registry(settings)
    close_calls = _spy_close_all(registry)
    monkeypatch.setattr(entrypoint, "build_runtime", lambda: (settings, registry))

    create_app_registries: list[ProviderRegistry | None] = []

    def spying_create_app(
        settings: Settings | None = None, registry: ProviderRegistry | None = None
    ) -> object:
        create_app_registries.append(registry)
        return real_create_app(settings=settings, registry=registry)

    monkeypatch.setattr(entrypoint, "create_app", spying_create_app)

    proxy_started = asyncio.Event()
    proxy_instances: list[ForwardProxyServer] = []

    class _RecordingForwardProxyServer(ForwardProxyServer):
        def __init__(self, settings: Settings, registry: ProviderRegistry) -> None:
            super().__init__(settings, registry)
            proxy_instances.append(self)

        async def start(self) -> asyncio.Server:
            server = await super().start()
            proxy_started.set()
            return server

    monkeypatch.setattr(entrypoint, "ForwardProxyServer", _RecordingForwardProxyServer)

    asgi_servers: list[entrypoint._CoordinatedServer] = []

    class _RecordingCoordinatedServer(entrypoint._CoordinatedServer):
        def __init__(self, config: object, stop_requested: asyncio.Event) -> None:
            super().__init__(config, stop_requested)  # type: ignore[arg-type]
            asgi_servers.append(self)

    monkeypatch.setattr(entrypoint, "_CoordinatedServer", _RecordingCoordinatedServer)

    task = asyncio.create_task(entrypoint.run())
    try:
        await _poll_until(lambda: asgi_servers and asgi_servers[0].started, "asgi server started")
        await _poll_until(proxy_started.is_set, "forward-proxy started")

        # The same registry object reached both the ASGI app and the
        # forward-proxy -- not two independent instances.
        assert create_app_registries == [registry]
        assert len(proxy_instances) == 1
        assert proxy_instances[0]._registry is registry  # noqa: SLF001

        asgi_servers[0].request_stop()
        assert close_calls == []  # not closed yet -- both listeners are still shutting down
        await asyncio.wait_for(task, timeout=5.0)
    finally:
        if not task.done():
            task.cancel()

    assert close_calls == [1]
