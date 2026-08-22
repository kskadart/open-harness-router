"""Application factory for open-harness-router.

Run: ``uvicorn main:create_app --factory``. All state (settings,
provider registry) is created here, not at module level -- no
import side effects.
"""

from __future__ import annotations

import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from dotenv import load_dotenv
from fastapi import FastAPI
from pydantic import ValidationError

from api.router import build_root_router
from errors import ConfigError, install_exception_handlers
from log import get_logger, setup_logging
from routing.config_loader import load_routing_config
from routing.registry import ProviderRegistry
from settings import Settings


def build_runtime() -> tuple[Settings, ProviderRegistry]:
    """Assemble settings and the provider registry outside the ASGI lifecycle.

    Split out of ``create_app`` so the provider layer can be started without
    FastAPI -- for example for a forward-proxy on a raw socket or an incoming
    Responses endpoint that doesn't need ASGI but needs the same provider
    registry and the same routing configuration.

    Returns:
        A tuple of the application settings and the built provider registry.

    Raises:
        SystemExit: when required settings are missing or routing
            configuration fails to load (the only place where a hard exit
            is allowed).
    """
    # Provider secrets (api_key_env from routing.yaml) are read via
    # os.getenv, so .env must be loaded into the process environment;
    # existing variables are not overwritten.
    load_dotenv()

    try:
        settings = Settings()
    except ValidationError as exc:
        sys.exit(f"open-harness-router: invalid settings: {exc}")

    setup_logging(settings.logging)

    try:
        routing_config = load_routing_config(settings.routing.config_path)
        registry = ProviderRegistry.build(routing_config, settings)
    except ConfigError as exc:
        sys.exit(f"open-harness-router: {exc}")

    return settings, registry


def create_app(
    settings: Settings | None = None,
    registry: ProviderRegistry | None = None,
) -> FastAPI:
    """Create and configure the FastAPI application.

    With no arguments (``uvicorn main:create_app --factory`` -- exactly how
    the user's LaunchAgent runs it) behavior is unchanged: settings and the
    registry are built right here via ``build_runtime``, and the application
    owns their lifetime -- the registry is closed in lifespan on shutdown.

    ``settings``/``registry`` can be passed pre-built: this is what the
    combined entrypoint (``entrypoint.run``) does, which runs both the ASGI
    listener and the forward-proxy in one process on a single
    ``ProviderRegistry`` (one httpx client pool per upstream instead of two).
    In that case ownership of the registry stays with the caller -- lifespan
    does NOT close the passed-in registry, otherwise it would close before
    the second listener sharing the same registry finishes.

    Args:
        settings: ready-made application settings; ``None`` -- build them
            via ``build_runtime``.
        registry: ready-made provider registry; ``None`` -- build it via
            ``build_runtime``. Must be set together with ``settings`` --
            either both arguments or neither.

    Returns:
        The configured application.

    Raises:
        ValueError: if only one of ``settings``/``registry`` is set.
        SystemExit: see ``build_runtime`` -- only when neither argument is
            passed and the application builds them itself.
    """
    if (settings is None) != (registry is None):
        raise ValueError("settings and registry are set together or both left as None")
    owns_registry = registry is None
    if settings is None or registry is None:
        settings, registry = build_runtime()
    logger = get_logger(__name__)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.settings = settings
        app.state.registry = registry
        if owns_registry and settings.proxy.enabled:
            # Without the combined entrypoint (entrypoint.run) there is no
            # one to start the forward-proxy -- the uvicorn factory only
            # builds the ASGI application. Silently ignoring the enabled
            # flag is not an option, otherwise the operator would believe
            # the proxy is running while nothing listens on the port.
            logger.warning(
                "proxy_enabled_without_entrypoint",
                message=(
                    "ROUTER_PROXY_ENABLED=true is ignored: the forward-proxy is "
                    "only started by the combined entrypoint (entrypoint.run), "
                    "not by a bare 'uvicorn main:create_app --factory'"
                ),
            )
        logger.info(
            "startup",
            providers=list(registry.providers.keys()),
            rules=len(registry.rules),
            default=registry.default_provider,
            routes=registry.describe_routes(),
        )
        try:
            yield
        finally:
            if owns_registry:
                await registry.close_all()
            logger.info("shutdown")

    app = FastAPI(title="open-harness-router", version="0.1.0", lifespan=lifespan)
    app.include_router(build_root_router())
    install_exception_handlers(app)
    return app
