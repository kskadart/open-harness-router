"""Unit tests for ``ProviderRegistry`` ownership in ``main.create_app``.

``httpx.ASGITransport`` (see ``tests/conftest.py``) does not run the
lifespan, so here the lifespan is invoked directly via
``app.router.lifespan_context`` -- the same context manager that
FastAPI/Starlette use internally for the ASGI ``lifespan`` protocol.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

import pytest

import main
from routing.config_loader import load_routing_config
from routing.registry import ProviderRegistry
from settings import Settings


def _spy_close_all(registry: ProviderRegistry) -> list[int]:
    """Wrap the registry's ``close_all`` with a call counter without losing the original.

    Args:
        registry: the registry whose ``close_all`` should be counted.

    Returns:
        A list that grows by one element on each ``close_all`` call.
    """
    calls: list[int] = []
    original: Callable[[], Awaitable[None]] = registry.close_all

    async def counting_close_all() -> None:
        calls.append(1)
        await original()

    registry.close_all = counting_close_all  # type: ignore[method-assign]
    return calls


def _build_registry(settings: Settings) -> ProviderRegistry:
    """Build the registry from the test ``routing_test.yaml``."""
    return ProviderRegistry.build(load_routing_config(settings.routing.config_path), settings)


async def test_create_app_without_args_owns_and_closes_registry(_env: None) -> None:
    """Without arguments, create_app builds its own registry and closes it in the lifespan.

    This is the prior behavior that the user's LaunchAgent relies on
    (``uvicorn main:create_app --factory``) -- a signature change must not
    affect it.
    """
    app = main.create_app()
    async with app.router.lifespan_context(app):
        calls = _spy_close_all(app.state.registry)
    assert calls == [1]


async def test_create_app_with_external_registry_does_not_close_it(_env: None) -> None:
    """With a registry passed in, the lifespan does not close it -- the caller owns it.

    This is how the combined entry point (``entrypoint.run``) works: the
    registry is shared with the second listener, and the entry point must
    close it, not the ASGI app's lifespan -- otherwise it would close before
    the forward-proxy finishes.
    """
    settings = Settings()
    registry = _build_registry(settings)
    calls = _spy_close_all(registry)

    app = main.create_app(settings=settings, registry=registry)
    async with app.router.lifespan_context(app):
        assert app.state.registry is registry
    assert calls == []

    await registry.close_all()
    assert calls == [1]


async def test_create_app_rejects_partial_arguments(_env: None) -> None:
    """``settings`` and ``registry`` are set together -- a partial call is forbidden."""
    settings = Settings()
    registry = _build_registry(settings)
    try:
        with pytest.raises(ValueError, match="together"):
            main.create_app(settings=settings)
        with pytest.raises(ValueError, match="together"):
            main.create_app(registry=registry)
    finally:
        await registry.close_all()
