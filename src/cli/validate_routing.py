"""Offline validation of ``routing.yaml`` along the router's real boot path.

Run from the repository root -- ``.env``, ``ROUTER_CONFIG_PATH`` and
``certs_dir`` are resolved relative to the working directory, exactly as
under the launchd service (``settings.RoutingSettings``,
``settings.SecretsResolver``)::

    PYTHONPATH=src .venv/bin/python -m cli.validate_routing \\
        [--expect-provider NAME] [ALIAS[=UPSTREAM_MODEL] ...]

``main.build_runtime`` is the same code the service runs at startup: it
loads ``.env``, validates the schema, resolves every provider's API key and
CA bundle and builds the registry -- without opening a listening socket. A
config that fails here would crash-loop the launchd service
(``providers/factory.py``, ``main.py``).

Exit codes:

* 0 -- the config builds and every expectation holds;
* 1 -- the config does not build: ``build_runtime`` exits with the same
  ``open-harness-router: ...`` message the service would write to its
  err.log;
* 3 -- ``--expect-provider`` is not among the built providers, or an alias
  resolved to a different provider / upstream model than expected.

JSON log lines from ``setup_logging`` may interleave with the output; the
summary starts at the ``=== ROUTES ===`` marker.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Sequence
from dataclasses import dataclass

from main import build_runtime
from routing.registry import ProviderRegistry

EXIT_OK = 0
EXIT_ROUTE_MISMATCH = 3

ROUTES_MARKER = "=== ROUTES ==="
ALIASES_MARKER = "=== ALIASES ==="
MISMATCHES_MARKER = "=== MISMATCHES ==="

_ALIAS_SEPARATOR = "="


@dataclass(frozen=True, slots=True)
class AliasExpectation:
    """An alias to resolve and, optionally, the upstream model it must map to.

    Attributes:
        alias: model name exactly as the client would send it.
        upstream_model: expected ``upstream_model`` of the matching rule;
            ``None`` -- not checked.
    """

    alias: str
    upstream_model: str | None


def parse_alias_expectation(raw: str) -> AliasExpectation:
    """Parse one positional ``ALIAS[=UPSTREAM_MODEL]`` argument.

    Args:
        raw: the raw command-line token.

    Returns:
        The parsed expectation; ``upstream_model`` is ``None`` without ``=``.

    Raises:
        argparse.ArgumentTypeError: the alias part is empty.
    """
    alias, separator, upstream_model = raw.partition(_ALIAS_SEPARATOR)
    if not alias:
        raise argparse.ArgumentTypeError(f"empty alias in {raw!r}")
    return AliasExpectation(alias, upstream_model if separator else None)


def describe_registry(registry: ProviderRegistry) -> list[str]:
    """Render the provider list and the routing table in resolution order.

    ``describe_routes`` lists the rules plus a trailing default entry, so it
    has one row more than the ``rules_count`` reported by ``/health``
    (``api/health.py``); both numbers are printed to avoid confusion. The
    limits printed per route are the effective ones, so a rule that
    overrides its provider's numbers shows its own.

    Args:
        registry: the built provider registry.

    Returns:
        Output lines, starting with the ``=== ROUTES ===`` marker.
    """
    routes = registry.describe_routes()
    lines = [
        ROUTES_MARKER,
        f"providers ({len(registry.providers)}): {', '.join(registry.providers)}",
        f"rules: {len(registry.rules)} (= /health rules_count); "
        f"routes below: {len(routes)} (rules + default)",
    ]
    for position, route in enumerate(routes, start=1):
        details = [
            f"{field}={route[field]}"
            for field in ("upstream_model", "max_tokens_limit", "context_window")
            if field in route
        ]
        target = f"{route['provider']} ({', '.join(details)})" if details else route["provider"]
        lines.append(
            f"  {position:>2}. {route['match_type']:<8} "
            f"{route.get('match_value', ''):<40} -> {target}"
        )
    return lines


def check_aliases(
    registry: ProviderRegistry,
    expectations: Sequence[AliasExpectation],
    expected_provider: str | None,
) -> tuple[list[str], list[str]]:
    """Resolve every alias and compare the decisions with the expectations.

    Args:
        registry: the built provider registry.
        expectations: aliases to resolve, with optional upstream models.
        expected_provider: provider every alias must resolve to, and which
            must exist in the registry; ``None`` -- only the upstream
            models (where given) are checked.

    Returns:
        A pair of (report lines, mismatch descriptions); an empty second
        element means every expectation held.
    """
    report = [ALIASES_MARKER]
    mismatches: list[str] = []
    if expected_provider is not None and expected_provider not in registry.providers:
        mismatches.append(
            f"provider '{expected_provider}' is not in the registry "
            f"(built: {', '.join(registry.providers)})"
        )
    for expectation in expectations:
        decision = registry.resolve(expectation.alias)
        provider_name = decision.provider.name
        report.append(
            f"  {expectation.alias} -> {provider_name} "
            f"(upstream_model={decision.upstream_model})"
        )
        if expected_provider is not None and provider_name != expected_provider:
            mismatches.append(
                f"alias '{expectation.alias}' resolved to provider "
                f"'{provider_name}', expected '{expected_provider}'"
            )
        if (
            expectation.upstream_model is not None
            and decision.upstream_model != expectation.upstream_model
        ):
            mismatches.append(
                f"alias '{expectation.alias}' maps to upstream_model "
                f"{decision.upstream_model!r}, expected {expectation.upstream_model!r}"
            )
    return report, mismatches


def _build_parser() -> argparse.ArgumentParser:
    """Build the command-line parser.

    Returns:
        The configured parser.
    """
    parser = argparse.ArgumentParser(
        prog="cli.validate_routing",
        description=(
            "Build the provider registry from routing.yaml exactly like the "
            "service does at startup (keys, CA bundles, schema) and resolve aliases."
        ),
    )
    parser.add_argument(
        "--expect-provider",
        metavar="NAME",
        help="provider that must exist and that every given alias must resolve to",
    )
    parser.add_argument(
        "aliases",
        nargs="*",
        type=parse_alias_expectation,
        metavar="ALIAS[=UPSTREAM_MODEL]",
        help="model name to resolve, optionally with the expected upstream_model",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Validate the routing configuration and print the routing table.

    Args:
        argv: command-line arguments without the program name; ``None`` --
            ``sys.argv[1:]``.

    Returns:
        The process exit code (see the module docstring).

    Raises:
        SystemExit: from ``build_runtime`` when settings or the routing
            configuration do not build (exit status 1 with the message).
    """
    args = _build_parser().parse_args(argv)
    _settings, registry = build_runtime()
    try:
        report = describe_registry(registry)
        alias_report, mismatches = check_aliases(registry, args.aliases, args.expect_provider)
    finally:
        asyncio.run(registry.close_all())
    print("\n".join(report + alias_report))
    if mismatches:
        print("\n".join([MISMATCHES_MARKER, *mismatches]), file=sys.stderr)
        return EXIT_ROUTE_MISMATCH
    print("OK: configuration builds and all expectations hold")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
