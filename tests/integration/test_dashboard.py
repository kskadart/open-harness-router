"""Integration tests: ``GET /dashboard`` and ``GET /dashboard/state``.

The passthrough upstream is mocked with pytest-httpx; the state must
reflect the routed request, its usage and the cost at the prices of
``routing_test.yaml``.
"""

from __future__ import annotations

import gzip
import json

import httpx
from pytest_httpx import HTTPXMock

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
    assert state["proxy"]["enabled"] is False
    assert state["proxy"]["mitm_hosts"] == ["api.anthropic.com"]
    assert state["proxy"]["root_certificate"].endswith("rootCA.pem")


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
