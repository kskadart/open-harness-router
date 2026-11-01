"""Integration tests: ``GET /dashboard`` and ``GET /dashboard/state``.

The passthrough upstream is mocked with pytest-httpx; the state must
reflect the routed request, its usage and the cost at the prices of
``routing_test.yaml``.
"""

from __future__ import annotations

import gzip
import json
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from pytest_httpx import HTTPXMock

import api.dashboard as dashboard_module
from services.monitor import Monitor
from services.request_store import RequestStore

_ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
_PAYLOAD = {
    "model": "claude-opus-4-8",
    "max_tokens": 16,
    "messages": [{"role": "user", "content": "hi"}],
}
_HEADERS = {"x-api-key": "test-client-key", "anthropic-version": "2023-06-01"}


async def test_dashboard_page_is_served(client: httpx.AsyncClient) -> None:
    response = await client.get("/dashboard")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "open-harness-router" in response.text
    assert "/dashboard/state" in response.text


async def test_dashboard_state_starts_empty_with_routes_and_proxy_facts(
    client: httpx.AsyncClient,
) -> None:
    response = await client.get("/dashboard/state")
    assert response.status_code == 200
    state = response.json()
    assert state["totals"]["requests"] == 0
    assert state["active"] == []
    assert state["version"]
    assert state["routes"][-1] == {"match_type": "default", "provider": "anthropic"}
    pricing = state["pricing"]
    assert pricing["unit"] == "USD per 1M tokens"
    assert pricing["providers"]["anthropic"] == {
        "input": 3,
        "output": 15,
        "cache_write": 3.75,
        "cache_read": 0.3,
    }
    assert pricing["routes"][-1] == {
        "match_type": "default",
        "provider": "anthropic",
        "pricing": {
            "input": 3,
            "output": 15,
            "cache_write": 3.75,
            "cache_read": 0.3,
        },
        "pricing_source": "provider",
    }
    assert state["proxy"]["enabled"] is False
    assert state["proxy"]["mitm_hosts"] == ["api.anthropic.com"]
    assert state["proxy"]["root_certificate"].endswith("rootCA.pem")
    assert state["windows"] == ["24h", "7d", "30d", "all", "custom"]
    # The ASGI test app runs without the lifespan, so no store is attached
    # and the persisted history degrades to None rather than breaking state.
    assert state["history"] is None


async def test_dashboard_state_answers_the_standard_windows(client: httpx.AsyncClient) -> None:
    """Every named window is accepted; without a store the history is None.

    The window resolution itself (including the hourly bucket of ``24h``)
    is covered by ``test_request_store`` and by the persisted-window test
    below, which has a store to answer from.
    """
    for window in ("24h", "7d", "30d", "all"):
        response = await client.get("/dashboard/state", params={"window": window})
        assert response.status_code == 200
        assert response.json()["history"] is None

    bad = await client.get("/dashboard/state", params={"window": "90d"})
    # Without a store the window is never resolved, so an unknown name is
    # not rejected here -- storage carries that validation.
    assert bad.status_code == 200


async def test_dashboard_state_serves_a_persisted_window(
    client: httpx.AsyncClient, tmp_path: Path
) -> None:
    """With a store attached the state carries the windowed history."""
    store = RequestStore(db_path=tmp_path / "m.db", retention_days=365, window_days=30)
    Monitor.current().attach_store(store)
    store.record(
        {
            "ts": datetime.now(UTC).isoformat(),
            "kind": "request",
            "status": 200,
            "provider": "anthropic",
            "model": "claude-opus-4-8",
            "duration_ms": 12.0,
            "usage": {"input_tokens": 100, "output_tokens": 20},
            "cost_usd": 0.001,
        }
    )
    try:
        response = await client.get("/dashboard/state", params={"window": "24h"})
        assert response.status_code == 200
        history = response.json()["history"]
        assert history["window"]["label"] == "24h"
        assert history["window"]["bucket"] == "hour"
        assert history["totals"]["requests"] == 1
        assert history["totals"]["usage"]["input_tokens"] == 100
        assert [row["bucket"] for row in history["series"]] == [
            datetime.now(UTC).strftime("%Y-%m-%dT%H")
        ]

        broken = await client.get("/dashboard/state", params={"window": "90d"})
        assert broken.status_code == 400
        assert "unknown window" in broken.json()["detail"]
    finally:
        Monitor.current().attach_store(None)
        await store.aclose()


async def test_dashboard_state_serves_the_throughput_figures(
    client: httpx.AsyncClient, tmp_path: Path
) -> None:
    """The window carries the generation throughput of every model."""
    store = RequestStore(db_path=tmp_path / "m.db", retention_days=365, window_days=30)
    Monitor.current().attach_store(store)
    store.record(
        {
            "ts": datetime.now(UTC).isoformat(),
            "kind": "request",
            "status": 200,
            "endpoint": "messages",
            "stream": 1,
            "provider": "anthropic",
            "model": "claude-opus-4-8",
            "duration_ms": 2000.0,
            "ttft_ms": 500.0,
            "usage": {"input_tokens": 100, "output_tokens": 300},
            "cost_usd": 0.001,
        }
    )
    try:
        response = await client.get("/dashboard/state", params={"window": "24h"})
        assert response.status_code == 200
        tps = response.json()["history"]["tps"]

        assert list(tps) == ["models", "series", "by_model"]
        assert tps["models"] == ["claude-opus-4-8"]
        assert len(tps["series"]) == 1
        row = tps["series"][0]
        assert row["bucket"] == datetime.now(UTC).strftime("%Y-%m-%dT%H")
        assert row["model"] == "claude-opus-4-8"
        # 300 output tokens over 1.5 s of generation (2 s minus the 0.5 s
        # to the first token).
        assert row["tps"] == 200.0
        assert row["output_tokens"] == 300
        assert row["generation_ms"] == 1500.0
        assert row["requests"] == 1
        model = tps["by_model"]["claude-opus-4-8"]
        assert model["tps"] == 200.0
        assert model["requests"] == 1
        assert model["avg_ttft_ms"] == 500.0
    finally:
        Monitor.current().attach_store(None)
        await store.aclose()


async def test_dashboard_state_serves_the_breakdown_and_the_data_since(
    client: httpx.AsyncClient, tmp_path: Path
) -> None:
    """The Providers table and the "data since" line read the window."""
    store = RequestStore(db_path=tmp_path / "m.db", retention_days=365, window_days=30)
    Monitor.current().attach_store(store)
    recorded = datetime.now(UTC).isoformat()
    store.record(
        {
            "ts": recorded,
            "kind": "request",
            "status": 200,
            "endpoint": "messages",
            "stream": 1,
            "provider": "anthropic",
            "model": "claude-opus-4-8",
            "duration_ms": 2000.0,
            "ttft_ms": 500.0,
            "usage": {"input_tokens": 100, "output_tokens": 300},
            "cost_usd": 0.001,
        }
    )
    store.record(
        {
            "ts": recorded,
            "kind": "request",
            "status": 200,
            "provider": "openai_compatible",
            "model": "GLM-5.2-FP8",
            "duration_ms": 30.0,
            "usage": {"input_tokens": 10},
        }
    )
    try:
        response = await client.get("/dashboard/state", params={"window": "24h"})
        assert response.status_code == 200
        history = response.json()["history"]

        assert history["data_since"] == recorded

        rows = history["breakdown"]
        assert len(rows) == 2
        row = rows[0]
        assert row["provider"] == "anthropic"
        assert row["model"] == "claude-opus-4-8"
        assert row["requests"] == 1
        assert row["errors"] == 0
        assert row["usage"]["output_tokens"] == 300
        assert row["tokens"] == 400
        assert row["cost_usd"] == 0.001
        # 300 output tokens over 1.5 s of generation.
        assert row["tps"] == 200.0
        assert row["tps_requests"] == 1
        assert row["avg_ttft_ms"] == 500.0
        # The prices come from the registry's own resolution of the model.
        assert row["pricing"] == {
            "input": 3,
            "output": 15,
            "cache_write": 3.75,
            "cache_read": 0.3,
        }
        assert row["pricing_source"] == "provider"

        # A rule that sets its own prices wins over its provider's.
        rule_row = rows[1]
        assert rule_row["model"] == "GLM-5.2-FP8"
        assert rule_row["pricing"] == {
            "input": 0.5,
            "output": 2,
            "cache_write": 0.5,
            "cache_read": 0.05,
        }
        assert rule_row["pricing_source"] == "rule"
    finally:
        Monitor.current().attach_store(None)
        await store.aclose()


async def test_dashboard_static_serves_a_whitelisted_file(
    client: httpx.AsyncClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A file of the whitelist is served with its own media type."""
    monkeypatch.setattr(dashboard_module, "_STATIC_DIR", tmp_path)
    (tmp_path / "app.js").write_text("export const app = 1;\n", encoding="utf-8")

    response = await client.get("/dashboard/static/app.js")

    assert response.status_code == 200
    assert response.headers["content-type"] == "text/javascript; charset=utf-8"
    assert response.headers["cache-control"] == "no-cache"
    assert response.text == "export const app = 1;\n"


async def test_dashboard_static_serves_a_stylesheet(
    client: httpx.AsyncClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The stylesheet is served as CSS, not as a script."""
    monkeypatch.setattr(dashboard_module, "_STATIC_DIR", tmp_path)
    (tmp_path / "app.css").write_text("body { color: red; }\n", encoding="utf-8")

    response = await client.get("/dashboard/static/app.css")

    assert response.status_code == 200
    assert response.headers["content-type"] == "text/css; charset=utf-8"


async def test_dashboard_static_refuses_an_unknown_name(
    client: httpx.AsyncClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The route serves a fixed whitelist, not the directory."""
    monkeypatch.setattr(dashboard_module, "_STATIC_DIR", tmp_path)
    (tmp_path / "secret.txt").write_text("nope\n", encoding="utf-8")

    response = await client.get("/dashboard/static/secret.txt")

    assert response.status_code == 404


async def test_dashboard_static_answers_404_for_a_missing_file(
    client: httpx.AsyncClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A whitelisted name whose file is not there is a 404, not a crash."""
    monkeypatch.setattr(dashboard_module, "_STATIC_DIR", tmp_path)

    response = await client.get("/dashboard/static/app.js")

    assert response.status_code == 404


async def test_dashboard_static_refuses_a_traversal(
    client: httpx.AsyncClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A path out of the static directory is a 404, not a file."""
    monkeypatch.setattr(dashboard_module, "_STATIC_DIR", tmp_path)
    outside = tmp_path.parent / "outside.txt"
    outside.write_text("nope\n", encoding="utf-8")

    # Percent-encoded, so the request reaches the route instead of being
    # normalised away by the client.
    response = await client.get("/dashboard/static/%2e%2e%2foutside.txt")

    assert response.status_code == 404


async def test_dashboard_state_answers_a_custom_range(
    client: httpx.AsyncClient, tmp_path: Path
) -> None:
    store = RequestStore(db_path=tmp_path / "m.db", retention_days=365, window_days=30)
    Monitor.current().attach_store(store)
    try:
        response = await client.get(
            "/dashboard/state",
            params={"window": "custom", "start": "2026-10-01", "end": "2026-10-07"},
        )
        assert response.status_code == 200
        window = response.json()["history"]["window"]
        assert window["label"] == "custom"
        assert window["start"].startswith("2026-10-01")
        assert window["end"].startswith("2026-10-08")

        broken = await client.get(
            "/dashboard/state",
            params={"window": "custom", "start": "2026-10-10", "end": "2026-10-01"},
        )
        assert broken.status_code == 400
    finally:
        Monitor.current().attach_store(None)
        await store.aclose()


async def test_dashboard_state_reflects_a_routed_request(
    client: httpx.AsyncClient, httpx_mock: HTTPXMock
) -> None:
    upstream_body = {
        "id": "msg_1",
        "type": "message",
        "role": "assistant",
        "model": "claude-opus-4-8",
        "content": [{"type": "text", "text": "hi"}],
        "stop_reason": "end_turn",
        "usage": {
            "input_tokens": 1_000_000,
            "output_tokens": 100_000,
            "cache_read_input_tokens": 1_000_000,
        },
    }
    httpx_mock.add_response(url=_ANTHROPIC_URL, method="POST", json=upstream_body)
    response = await client.post("/v1/messages", json=_PAYLOAD, headers=_HEADERS)
    assert response.status_code == 200

    state = (await client.get("/dashboard/state")).json()
    assert state["totals"]["requests"] == 1
    assert state["totals"]["errors"] == 0
    assert state["providers"]["anthropic"]["usage"]["input_tokens"] == 1_000_000
    assert state["models"]["claude-opus-4-8"]["provider"] == "anthropic"
    # 1M input at $3 + 100K output at $15 + 1M cache read at $0.30.
    assert state["totals"]["cost_usd"] == 3 + 1.5 + 0.3
    entry = state["events"][0]
    assert entry["kind"] == "request"
    assert entry["model"] == "claude-opus-4-8"
    assert entry["status"] == 200


async def test_dashboard_state_reads_usage_off_a_passthrough_stream(
    client: httpx.AsyncClient, httpx_mock: HTTPXMock
) -> None:
    def sse(event: str, payload: dict[str, object]) -> bytes:
        return f"event: {event}\ndata: {json.dumps(payload)}\n\n".encode()

    stream = (
        sse("message_start", {"type": "message_start", "message": {"usage": {"input_tokens": 50}}})
        + sse("message_delta", {"type": "message_delta", "usage": {"output_tokens": 8}})
        + sse("message_stop", {"type": "message_stop"})
    )
    httpx_mock.add_response(
        url=_ANTHROPIC_URL,
        method="POST",
        content=stream,
        headers={"content-type": "text/event-stream"},
    )
    response = await client.post(
        "/v1/messages", json={**_PAYLOAD, "stream": True}, headers=_HEADERS
    )
    assert response.status_code == 200
    assert response.content == stream  # the wrapper changes no byte

    state = (await client.get("/dashboard/state")).json()
    assert state["totals"]["usage"]["input_tokens"] == 50
    assert state["totals"]["usage"]["output_tokens"] == 8
    assert state["events"][0]["stream"] is True
    assert state["active"] == []


async def test_dashboard_state_reads_usage_off_a_gzipped_passthrough_stream(
    client: httpx.AsyncClient, httpx_mock: HTTPXMock
) -> None:
    """The real upstream gzips SSE; passthrough relays it compressed and the count still works."""
    stream = (
        b'event: message_start\ndata: {"type": "message_start", "message": {"usage": '
        b'{"input_tokens": 70}}}\n\n'
        b'event: message_delta\ndata: {"type": "message_delta", "usage": {"output_tokens": 9}}\n\n'
    )
    httpx_mock.add_response(
        url=_ANTHROPIC_URL,
        method="POST",
        content=gzip.compress(stream),
        headers={"content-type": "text/event-stream", "content-encoding": "gzip"},
    )
    response = await client.post(
        "/v1/messages", json={**_PAYLOAD, "stream": True}, headers=_HEADERS
    )
    assert response.status_code == 200
    assert response.headers["content-encoding"] == "gzip"

    state = (await client.get("/dashboard/state")).json()
    assert state["totals"]["usage"] == {
        "input_tokens": 70,
        "output_tokens": 9,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 0,
    }


async def test_dashboard_state_counts_an_upstream_error(
    client: httpx.AsyncClient, httpx_mock: HTTPXMock
) -> None:
    httpx_mock.add_response(
        url=_ANTHROPIC_URL,
        method="POST",
        status_code=529,
        json={"type": "error", "error": {"type": "overloaded_error", "message": "busy"}},
    )
    response = await client.post("/v1/messages", json=_PAYLOAD, headers=_HEADERS)
    assert response.status_code == 529

    state = (await client.get("/dashboard/state")).json()
    assert state["totals"]["errors"] == 1
    assert state["events"][0]["status"] == 529
    assert state["events"][0]["level"] == "error"
