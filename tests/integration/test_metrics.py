"""Integration tests: ``GET /metrics`` in the Prometheus text format."""

from __future__ import annotations

import httpx
from pytest_httpx import HTTPXMock

from api.metrics import PROMETHEUS_CONTENT_TYPE, render_metrics

_ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
_PAYLOAD = {
    "model": "claude-opus-4-8",
    "max_tokens": 16,
    "messages": [{"role": "user", "content": "hi"}],
}
_HEADERS = {"x-api-key": "test-client-key", "anthropic-version": "2023-06-01"}


async def test_metrics_are_served_in_the_exposition_format(client: httpx.AsyncClient) -> None:
    response = await client.get("/metrics")
    assert response.status_code == 200
    assert response.headers["content-type"] == PROMETHEUS_CONTENT_TYPE
    text = response.text
    assert "# TYPE ohr_requests_total counter\n" in text
    assert 'ohr_info{version="' in text
    assert "ohr_forward_proxy_enabled 0\n" in text
    assert "ohr_inflight_requests 0\n" in text
    assert text.endswith("\n")


async def test_metrics_reflect_a_routed_request(
    client: httpx.AsyncClient, httpx_mock: HTTPXMock
) -> None:
    httpx_mock.add_response(
        url=_ANTHROPIC_URL,
        method="POST",
        json={"usage": {"input_tokens": 1_000_000, "output_tokens": 100_000}},
    )
    assert (await client.post("/v1/messages", json=_PAYLOAD, headers=_HEADERS)).status_code == 200

    text = (await client.get("/metrics")).text
    labels = 'provider="anthropic",model="claude-opus-4-8"'
    assert f"ohr_requests_total{{{labels}}} 1\n" in text
    assert f"ohr_errors_total{{{labels}}} 0\n" in text
    assert f'ohr_tokens_total{{{labels},kind="input"}} 1000000\n' in text
    assert f'ohr_tokens_total{{{labels},kind="output"}} 100000\n' in text
    assert f'ohr_tokens_total{{{labels},kind="cache_read"}} 0\n' in text
    # 1M input at $3 + 100K output at $15 (routing_test.yaml prices).
    assert f"ohr_cost_usd_total{{{labels}}} 4.5\n" in text
    assert f"ohr_priced_requests_total{{{labels}}} 1\n" in text
    assert f"ohr_request_duration_seconds_count{{{labels}}} 1\n" in text


def test_render_escapes_label_values() -> None:
    snapshot = {
        "uptime_s": 12.5,
        "active": [{"provider": "p"}, {"provider": "p"}, {"provider": "q"}],
        "models": {
            'we"ird\\model\n': {
                "provider": "p",
                "requests": 2,
                "errors": 1,
                "duration_ms_total": 1500.0,
                "usage": {"input_tokens": 1, "output_tokens": 2},
                "cost_usd": 0.0,
                "priced_requests": 0,
            }
        },
    }
    text = render_metrics(snapshot, {"enabled": True}, "9.9.9")
    assert 'ohr_info{version="9.9.9"} 1\n' in text
    assert "ohr_uptime_seconds 12.5\n" in text
    assert "ohr_forward_proxy_enabled 1\n" in text
    assert 'ohr_inflight_requests{provider="p"} 2\n' in text
    assert 'ohr_inflight_requests{provider="q"} 1\n' in text
    escaped = 'provider="p",model="we\\"ird\\\\model\\n"'
    assert f"ohr_requests_total{{{escaped}}} 2\n" in text
    assert f"ohr_request_duration_seconds_sum{{{escaped}}} 1.5\n" in text
    assert f'ohr_tokens_total{{{escaped},kind="cache_write"}} 0\n' in text
