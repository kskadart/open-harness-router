"""Utility endpoint ``GET /health``."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from dependencies import RegistryDep

router = APIRouter()


@router.get("/health")
async def health(registry: RegistryDep) -> dict[str, Any]:
    """Return the router's status and a registry summary.

    Args:
        registry: the provider registry.

    Returns:
        A dict with status, the list of providers, and the rule count.
    """
    return {
        "status": "healthy",
        "providers": list(registry.providers.keys()),
        "rules_count": len(registry.rules),
    }
