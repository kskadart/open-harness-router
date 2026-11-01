"""``GET /dashboard`` (the page) and ``GET /dashboard/state`` (its data).

The page is a thin HTML shell next to this module; it polls
``/dashboard/state`` and renders what ``services.monitor`` collected plus
the routing table and the forward-proxy facts from settings. Its script and
its stylesheet are served from ``static/`` by a fixed whitelist, so the
page loads only same-origin files. Both live on the ASGI listener (8787 by
default) and, like ``/health``, are meant for the loopback interface: the
state names models, providers, request paths and tunnelled hosts -- never
bodies or credentials -- but it is the router's activity log in JSON.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from cryptography import x509
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse

from dependencies import RegistryDep, SettingsDep
from services.monitor import Monitor
from services.request_store import WINDOW_LABELS, resolve_window
from settings import ProxySettings, Settings

if TYPE_CHECKING:
    from routing.registry import ProviderRegistry

router = APIRouter()

_PAGE = Path(__file__).with_name("dashboard.html").read_text(encoding="utf-8")

# Where the page's own files live. The frontend is vendored, byte for byte,
# under ``static/vendor`` -- no build step, no CDN at runtime.
_STATIC_DIR = Path(__file__).with_name("static")

# The only files the static route serves, and the media type of each. A
# whitelist rather than a directory listing: the route must not become a
# reader of the checkout. A name that is not on the list -- including a
# traversal like ``../`` -- is a 404.
_STATIC_FILES: dict[str, str] = {
    "app.js": "text/javascript; charset=utf-8",
    "app.css": "text/css; charset=utf-8",
    "vendor/preact.mjs": "text/javascript; charset=utf-8",
    "vendor/preact-hooks.mjs": "text/javascript; charset=utf-8",
    "vendor/htm.mjs": "text/javascript; charset=utf-8",
    "vendor/chart.umd.min.js": "text/javascript; charset=utf-8",
}


def _root_certificate_expiry(path: Path) -> str | None:
    """Return the root CA's ``notAfter`` in ISO form, if the PEM is readable."""
    try:
        certificate = x509.load_pem_x509_certificate(path.read_bytes())
    except (OSError, ValueError):
        return None
    return certificate.not_valid_after_utc.isoformat()


def proxy_info(proxy: ProxySettings) -> dict[str, Any]:
    """Describe the forward-proxy as configured.

    ``enabled`` is the setting, not a probe: under the combined entrypoint
    it is also whether the listener is up, while a bare uvicorn run ignores
    the flag (``main.create_app`` logs ``proxy_enabled_without_entrypoint``).

    Args:
        proxy: the forward-proxy settings.

    Returns:
        The flag, the listen address, the MITM allowlist, the root CA path
        and its expiry (``None`` before the first ``make run-proxy``).
    """
    root_certificate = proxy.ca_dir / "rootCA.pem"
    return {
        "enabled": proxy.enabled,
        "host": proxy.host,
        "port": proxy.port,
        "mitm_hosts": sorted(proxy.mitm_host_set()),
        "root_certificate": str(root_certificate),
        "root_certificate_not_after": _root_certificate_expiry(root_certificate),
    }


@router.get("/dashboard", include_in_schema=False)
async def dashboard_page() -> HTMLResponse:
    """Serve the dashboard page."""
    return HTMLResponse(_PAGE)


@router.get("/dashboard/static/{name:path}", include_in_schema=False)
async def dashboard_static(name: str) -> FileResponse:
    """Serve one of the page's own static files.

    Only the names on the whitelist are served, so the route cannot be
    turned into a reader of the checkout; anything else -- an unknown
    name, a traversal -- is a 404.

    Args:
        name: the path under ``static/``, e.g. ``app.js``.

    Returns:
        The file, with its media type and ``Cache-Control: no-cache`` (the
        page polls for fresh state, and a stale script would be worse
        than a round trip).

    Raises:
        HTTPException: ``404`` when the name is not on the whitelist or
            the file is not there.
    """
    media_type = _STATIC_FILES.get(name)
    if media_type is None:
        raise HTTPException(status_code=404, detail="not found")
    path = _STATIC_DIR / name
    if not path.is_file():
        raise HTTPException(status_code=404, detail="not found")
    return FileResponse(path, media_type=media_type, headers={"Cache-Control": "no-cache"})


@router.get("/dashboard/state")
async def dashboard_state(
    request: Request,
    settings: SettingsDep,
    registry: RegistryDep,
    window: str = Query(default="30d", description="24h | 7d | 30d | all | custom"),
    start: str | None = Query(default=None, description="custom range start, YYYY-MM-DD"),
    end: str | None = Query(default=None, description="custom range end, YYYY-MM-DD"),
) -> dict[str, Any]:
    """Return the monitor's snapshot plus the routing table and proxy facts.

    The persisted statistics are read over the requested window -- one of
    ``24h``, ``7d``, ``30d``, ``all``, or a ``custom`` range given as
    ``start``/``end`` -- so the page can show today, a week, a month or an
    arbitrary period. The in-memory counters stay exactly as they are; the
    windowed figures ride alongside them under ``history``.

    Args:
        request: the incoming request (for the application version).
        settings: the application settings.
        registry: the provider registry.
        window: the named window, or ``custom`` with ``start``/``end``.
        start: custom range lower bound.
        end: custom range upper bound.

    Returns:
        ``Monitor.snapshot()`` extended with ``version``, ``routes``
        (``ProviderRegistry.describe_routes``), ``pricing``
        (``ProviderRegistry.describe_pricing``), ``proxy`` (``proxy_info``)
        and ``history`` -- ``None`` when storage is off, else the resolved
        ``window`` (label, bounds, bucket), its ``totals``, the ``series``
        with one entry per bucket, the ``tps`` throughput figures, the
        ``breakdown`` of the window per (provider, model) and the
        ``data_since`` timestamp of the oldest stored row.

    Raises:
        HTTPException: ``400`` when the window name or the custom range is
            malformed -- the caller asked for something the store cannot
            answer, and a silent fallback would mislabel the numbers.
    """
    state = Monitor.current().snapshot()
    state["version"] = request.app.version
    state["routes"] = registry.describe_routes()
    state["pricing"] = registry.describe_pricing()
    state["proxy"] = proxy_info(settings.proxy)
    state["windows"] = list(WINDOW_LABELS)
    state["history"] = await _history(
        settings, registry, window=window, start=start, end=end
    )
    return state


async def _history(
    settings: Settings,
    registry: ProviderRegistry,
    *,
    window: str,
    start: str | None,
    end: str | None,
) -> dict[str, Any] | None:
    """Read the persisted statistics for one window, if storage is on.

    Args:
        settings: the application settings (for storage and retention).
        registry: the provider registry (for the effective prices of the
            models in the breakdown).
        window: the named window or ``custom``.
        start: custom range lower bound.
        end: custom range upper bound.

    Returns:
        ``None`` when storage is disabled or no store is attached, else the
        resolved window with its totals, its bucket series, its throughput
        figures (the generation speed of every model), the breakdown of
        the window per (provider, model) -- each row carrying the prices
        the registry resolves that model at -- and the timestamp of the
        oldest stored row.

    Raises:
        HTTPException: ``400`` on a malformed window or range.
    """
    store = Monitor.current().store
    if not settings.storage.enabled or store is None:
        return None
    try:
        resolved = resolve_window(
            window=window,
            start=start,
            end=end,
            retention_days=settings.storage.retention_days,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    totals = await store.range_totals(start=resolved.start, end=resolved.end)
    series = await store.range_series(
        start=resolved.start, end=resolved.end, bucket=resolved.bucket
    )
    grouped = await store.grouped_totals(start=resolved.start, end=resolved.end)
    tps = await store.tps(start=resolved.start, end=resolved.end, bucket=resolved.bucket)
    breakdown = await store.breakdown(start=resolved.start, end=resolved.end)
    # The prices the registry resolves a model at -- the rule's own if it
    # sets them, the provider's otherwise. A model the registry no longer
    # resolves has no prices. One resolution per model: a model appears
    # under several providers, and the rules are matched again every time.
    prices: dict[str | None, dict[str, Any]] = {None: {"pricing": None, "pricing_source": None}}
    for row in breakdown:
        model = row["model"]
        if model not in prices:
            prices[model] = registry.resolve_pricing(model)
        row["pricing"] = prices[model]["pricing"]
        row["pricing_source"] = prices[model]["pricing_source"]
    return {
        "window": {
            "label": resolved.label,
            "start": resolved.start,
            "end": resolved.end,
            "bucket": resolved.bucket,
            "clamped": resolved.clamped,
        },
        "totals": totals,
        "series": series,
        "providers": grouped["providers"],
        "models": grouped["models"],
        "tps": tps,
        "breakdown": breakdown,
        "data_since": await store.first_ts(),
    }
