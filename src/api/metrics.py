"""``GET /metrics``: the monitor's counters in the Prometheus text format.

The same numbers ``/dashboard/state`` returns as JSON, rendered as
counters and gauges for a scraper. No client library: the exposition
format is a few lines of text, and the monitor already keeps everything a
counter needs (monotonic totals since the process started). Labels are
``provider`` and ``model``; per-provider totals are a ``sum by (provider)``
away in PromQL. ``monitoring/`` in the repository holds a Prometheus and
Grafana stack that scrapes this endpoint.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import PlainTextResponse

from api.dashboard import proxy_info
from dependencies import SettingsDep
from services.monitor import Monitor

router = APIRouter()

PROMETHEUS_CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"

_TOKEN_KINDS = (
    ("input", "input_tokens"),
    ("output", "output_tokens"),
    ("cache_write", "cache_creation_input_tokens"),
    ("cache_read", "cache_read_input_tokens"),
)


def _escape(value: object) -> str:
    """Escape a label value for the exposition format."""
    return str(value).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _labels(**labels: object) -> str:
    """Render a label set, ``{}``-free when empty."""
    if not labels:
        return ""
    body = ",".join(f'{name}="{_escape(value)}"' for name, value in labels.items())
    return "{" + body + "}"


def _fmt(value: float) -> str:
    """Render a sample value; integers without a trailing ``.0``."""
    return str(int(value)) if float(value).is_integer() else repr(float(value))


def render_metrics(snapshot: dict[str, Any], proxy: dict[str, Any], version: str) -> str:
    """Render a monitor snapshot as Prometheus text.

    Args:
        snapshot: ``Monitor.snapshot()``.
        proxy: ``api.dashboard.proxy_info``.
        version: the application version.

    Returns:
        The exposition text, one metric family per ``# HELP``/``# TYPE`` pair.
    """
    return "".join(_lines(snapshot, proxy, version))


def _lines(snapshot: dict[str, Any], proxy: dict[str, Any], version: str) -> Iterator[str]:
    """Yield the exposition text line by line."""
    models: dict[str, dict[str, Any]] = snapshot.get("models", {})
    inflight: dict[str, int] = {}
    for item in snapshot.get("active", []):
        inflight[item["provider"]] = inflight.get(item["provider"], 0) + 1

    yield "# HELP ohr_info Router build information.\n"
    yield "# TYPE ohr_info gauge\n"
    yield f"ohr_info{_labels(version=version)} 1\n"

    yield "# HELP ohr_uptime_seconds Seconds since the router process started.\n"
    yield "# TYPE ohr_uptime_seconds gauge\n"
    yield f"ohr_uptime_seconds {_fmt(snapshot.get('uptime_s', 0))}\n"

    yield "# HELP ohr_forward_proxy_enabled Whether the forward-proxy listener is configured.\n"
    yield "# TYPE ohr_forward_proxy_enabled gauge\n"
    yield f"ohr_forward_proxy_enabled {1 if proxy.get('enabled') else 0}\n"

    yield "# HELP ohr_inflight_requests Routed requests currently in flight.\n"
    yield "# TYPE ohr_inflight_requests gauge\n"
    for provider, count in sorted(inflight.items()):
        yield f"ohr_inflight_requests{_labels(provider=provider)} {count}\n"
    if not inflight:
        yield "ohr_inflight_requests 0\n"

    yield "# HELP ohr_requests_total Routed requests finished, by model.\n"
    yield "# TYPE ohr_requests_total counter\n"
    for model, stats in sorted(models.items()):
        labels = _labels(provider=stats.get("provider"), model=model)
        yield f"ohr_requests_total{labels} {stats['requests']}\n"

    yield "# HELP ohr_errors_total Routed requests that failed or answered 4xx/5xx, by model.\n"
    yield "# TYPE ohr_errors_total counter\n"
    for model, stats in sorted(models.items()):
        labels = _labels(provider=stats.get("provider"), model=model)
        yield f"ohr_errors_total{labels} {stats['errors']}\n"

    yield "# HELP ohr_request_duration_seconds Time from routing decision to last byte, by model.\n"
    yield "# TYPE ohr_request_duration_seconds summary\n"
    for model, stats in sorted(models.items()):
        labels = _labels(provider=stats.get("provider"), model=model)
        seconds = stats.get("duration_ms_total", 0.0) / 1000.0
        yield f"ohr_request_duration_seconds_sum{labels} {_fmt(seconds)}\n"
        yield f"ohr_request_duration_seconds_count{labels} {stats['requests']}\n"

    yield "# HELP ohr_tokens_total Tokens read off responses, by model and kind.\n"
    yield "# TYPE ohr_tokens_total counter\n"
    for model, stats in sorted(models.items()):
        usage = stats.get("usage", {})
        for kind, field_name in _TOKEN_KINDS:
            labels = _labels(provider=stats.get("provider"), model=model, kind=kind)
            yield f"ohr_tokens_total{labels} {usage.get(field_name, 0)}\n"

    yield "# HELP ohr_cost_usd_total Cost at the configured prices, by model (0 without pricing).\n"
    yield "# TYPE ohr_cost_usd_total counter\n"
    for model, stats in sorted(models.items()):
        labels = _labels(provider=stats.get("provider"), model=model)
        yield f"ohr_cost_usd_total{labels} {_fmt(stats.get('cost_usd', 0.0))}\n"

    yield "# HELP ohr_priced_requests_total Requests that had a price to be costed at, by model.\n"
    yield "# TYPE ohr_priced_requests_total counter\n"
    for model, stats in sorted(models.items()):
        labels = _labels(provider=stats.get("provider"), model=model)
        yield f"ohr_priced_requests_total{labels} {stats.get('priced_requests', 0)}\n"


@router.get("/metrics", include_in_schema=False)
async def metrics(request: Request, settings: SettingsDep) -> PlainTextResponse:
    """Serve the monitor's counters for a Prometheus scraper."""
    text = render_metrics(
        Monitor.current().snapshot(), proxy_info(settings.proxy), request.app.version
    )
    return PlainTextResponse(text, media_type=PROMETHEUS_CONTENT_TYPE)
