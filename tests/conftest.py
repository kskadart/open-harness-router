"""Shared pytest fixtures for open-harness-router.

Sets up environment variables (required Settings), builds the FastAPI app
via the ``create_app`` factory, and manually populates ``app.state`` (the
lifespan does not run under ``httpx.ASGITransport``). The test HTTP client
uses ``ASGITransport``, so its requests reach the app, while outbound
provider requests (default httpx transport) are intercepted by pytest-httpx.
"""

from __future__ import annotations

import inspect
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
import structlog
from fastapi import FastAPI
from pydantic_settings import BaseSettings

import main as main_module
import settings as settings_module
from main import create_app
from routing.config_loader import load_routing_config
from routing.registry import ProviderRegistry
from services.monitor import Monitor
from settings import Settings

TESTS_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = TESTS_DIR.parent
ROUTING_TEST_YAML = TESTS_DIR / "fixtures" / "routing_test.yaml"


def _settings_classes() -> list[type[BaseSettings]]:
    """Every ``BaseSettings`` subclass defined in ``settings``."""
    return [
        cls
        for _name, cls in inspect.getmembers(settings_module, inspect.isclass)
        if issubclass(cls, BaseSettings) and cls is not BaseSettings
    ]


@pytest.fixture(autouse=True)
def _ignore_dotenv(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the developer's working ``.env`` out of the test run.

    The file reaches the code on two paths, and both are relative to the
    current directory, which is the repository root under ``make test``:
    every settings class reads ``env_file=".env"``, and ``build_runtime``
    calls ``load_dotenv()``, which writes the file into ``os.environ`` for
    the rest of the process -- past ``monkeypatch``, past the test that
    triggered it. A working ``.env`` there (``ROUTER_PROXY_ENABLED=true`` on
    a machine that runs the forward-proxy) leaked into tests that assert
    the defaults and made them fail depending on the checkout, not the
    code. Tests set what they need through ``monkeypatch.setenv``, so both
    paths are disabled for all of them.
    """
    for cls in _settings_classes():
        monkeypatch.setitem(cls.model_config, "env_file", None)
    monkeypatch.setattr(main_module, "load_dotenv", lambda *_args, **_kwargs: False)


@pytest.fixture(autouse=True)
def _fresh_monitor() -> None:
    """Give every test an empty process-wide monitor.

    ``services.monitor.Monitor`` is one object per process by design (the
    dashboard's uptime and counters are process facts), so without a reset
    a test would see the requests and log events of the tests before it.
    """
    Monitor.reset()


@pytest.fixture(autouse=True)
def _isolate_structlog_config() -> AsyncIterator[None]:
    """Restore the global structlog configuration after each test.

    ``setup_logging`` inside ``build_runtime`` configures structlog for the
    whole process, so a test that boots the app changed the log output for
    every test after it. Tests using ``structlog.testing.capture_logs`` after
    such a test saw no events (they went through the stdlib handlers instead)
    and failed depending on run order, while passing in isolation.
    """
    saved = structlog.get_config()
    try:
        yield
    finally:
        structlog.configure(**saved)


@pytest.fixture
def non_mocked_hosts() -> list[str]:
    """Hosts that pytest-httpx must not intercept.

    The test ASGI client talks to ``http://test``, a placeholder host.
    ASGITransport does not inherit from ``AsyncHTTPTransport`` anyway, but the
    fixture is documented for explicitness.
    """
    return ["test"]


@pytest.fixture
def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Prepare environment variables before ``Settings`` is created."""
    monkeypatch.setenv("ROUTER_CONFIG_PATH", str(ROUTING_TEST_YAML))
    monkeypatch.setenv("ROUTER_CERTS_DIR", str(PROJECT_ROOT / "certs"))
    monkeypatch.setenv("ROUTER_LOG_CONF_PATH", str(PROJECT_ROOT / "logging_conf.json"))
    monkeypatch.setenv("OPENAI_COMPATIBLE_KEY", "test-openai-compatible-key")
    monkeypatch.setenv("OPENAI_API_KEY", "test-openai-key")


@pytest_asyncio.fixture
async def app(_env: None) -> AsyncIterator[FastAPI]:
    """Build the FastAPI app and manually initialize its state.

    ``httpx.ASGITransport`` does not run the lifespan handler, so
    ``app.state.settings`` and ``app.state.registry`` are populated here.
    """
    fastapi_app = create_app()
    settings = Settings()
    registry = ProviderRegistry.build(
        load_routing_config(settings.routing.config_path), settings
    )
    fastapi_app.state.settings = settings
    fastapi_app.state.registry = registry
    try:
        yield fastapi_app
    finally:
        await registry.close_all()


@pytest_asyncio.fixture
async def client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    """HTTP client that reaches the app through ASGITransport."""
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac
