"""Aggregator for the public API routers.

The ``/v1/messages`` and ``/v1/messages/count_tokens`` paths are mounted at
the root without an ``/api/v1`` prefix -- the contract is fixed by the
Claude Code protocol. ``/health`` is wired up as a separate utility router.
"""

from __future__ import annotations

from fastapi import APIRouter

from api.count_tokens import router as count_tokens_router
from api.health import router as health_router
from api.messages import router as messages_router


def build_root_router() -> APIRouter:
    """Assemble the root router from all sub-routers.

    Returns:
        The aggregated API router.
    """
    root = APIRouter()
    root.include_router(messages_router)
    root.include_router(count_tokens_router)
    root.include_router(health_router)
    return root
