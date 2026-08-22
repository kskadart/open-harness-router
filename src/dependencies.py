"""FastAPI dependencies: access to settings, the logger, and the provider registry.

Objects are placed into ``app.state`` by the ``create_app`` factory at
startup; here they are pulled out via typed providers and injected into
handlers.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated

import structlog
from fastapi import Depends, Request

from log import get_logger
from settings import Settings

if TYPE_CHECKING:
    from routing.registry import ProviderRegistry


def get_settings(request: Request) -> Settings:
    """Return the application settings from state."""
    settings: Settings = request.app.state.settings
    return settings


def get_registry(request: Request) -> ProviderRegistry:
    """Return the provider registry from state."""
    registry: ProviderRegistry = request.app.state.registry
    return registry


def get_request_logger() -> structlog.stdlib.BoundLogger:
    """Return the logger for request handlers."""
    return get_logger("llm_router.api")


SettingsDep = Annotated[Settings, Depends(get_settings)]
RegistryDep = Annotated["ProviderRegistry", Depends(get_registry)]
LoggerDep = Annotated[structlog.stdlib.BoundLogger, Depends(get_request_logger)]
