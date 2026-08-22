"""Integration smoke test for the ``GET /health`` endpoint."""

from __future__ import annotations

import httpx


async def test_health_returns_registry_summary(client: httpx.AsyncClient) -> None:
    response = await client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "healthy"
    assert set(body["providers"]) == {"anthropic", "openai_compatible", "openai", "openai_chat"}
    assert isinstance(body["rules_count"], int)
    assert body["rules_count"] == 4
