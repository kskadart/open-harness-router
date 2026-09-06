"""``GET /dashboard`` (the page) and ``GET /dashboard/state`` (its data).

The page is a single self-contained HTML file next to this module; it polls
``/dashboard/state`` and renders what ``services.monitor`` collected plus
the routing table and the forward-proxy facts from settings. Both live on
the ASGI listener (8787 by default) and, like ``/health``, are meant for the
loopback interface: the state names models, providers, request paths and
tunnelled hosts -- never bodies or credentials -- but it is the router's
activity log in JSON.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from cryptography import x509
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from dependencies import RegistryDep, SettingsDep
from services.monitor import Monitor
from settings import ProxySettings

router = APIRouter()

_PAGE = Path(__file__).with_name("dashboard.html").read_text(encoding="utf-8")


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


@router.get("/dashboard/state")
async def dashboard_state(
    request: Request, settings: SettingsDep, registry: RegistryDep
) -> dict[str, Any]:
    """Return the monitor's snapshot plus the routing table and proxy facts.

    Args:
        request: the incoming request (for the application version).
        settings: the application settings.
        registry: the provider registry.

    Returns:
        ``Monitor.snapshot()`` extended with ``version``, ``routes``
        (``ProviderRegistry.describe_routes``) and ``proxy`` (``proxy_info``).
    """
    state = Monitor.current().snapshot()
    state["version"] = request.app.version
    state["routes"] = registry.describe_routes()
    state["proxy"] = proxy_info(settings.proxy)
    return state
