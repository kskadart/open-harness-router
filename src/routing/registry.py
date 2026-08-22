"""Provider registry and route resolution by model name."""

from __future__ import annotations

from dataclasses import dataclass

from log import get_logger
from providers.base import Provider
from providers.factory import build_provider
from routing.matcher import match_model
from routing.schema import RoutingConfig, RoutingRule
from settings import Settings

logger = get_logger(__name__)


@dataclass(frozen=True)
class RouteDecision:
    """The result of route resolution.

    Attributes:
        provider: the selected provider.
        upstream_model: model name for the upstream (None -> keep the original).
    """

    provider: Provider
    upstream_model: str | None


class ProviderRegistry:
    """Provider registry that resolves routes by model name."""

    def __init__(
        self,
        providers: dict[str, Provider],
        rules: list[RoutingRule],
        default_provider: str,
    ) -> None:
        """Initialize the registry.

        Args:
            providers: providers keyed by name.
            rules: routing rules (in priority order).
            default_provider: name of the default provider.
        """
        self.providers = providers
        self.rules = rules
        self.default_provider = default_provider

    @classmethod
    def build(cls, config: RoutingConfig, settings: Settings) -> ProviderRegistry:
        """Build the registry from the routing configuration.

        Args:
            config: validated ``routing.yaml`` configuration.
            settings: application settings.

        Returns:
            The built provider registry.
        """
        providers = {
            name: build_provider(name, cfg, settings)
            for name, cfg in config.providers.items()
        }
        return cls(providers, config.rules, config.default.provider)

    def resolve(self, model: str) -> RouteDecision:
        """Resolve the route for a model name.

        Args:
            model: model name from the request body.

        Returns:
            The routing decision (provider + optional model substitution).
        """
        for rule in self.rules:
            if match_model(rule.match, model):
                return RouteDecision(self.providers[rule.provider], rule.upstream_model)
        return RouteDecision(self.providers[self.default_provider], None)

    def describe_routes(self) -> list[dict[str, str]]:
        """Build a compact description of the routing table for the startup log.

        Entry order mirrors the check order in ``resolve``: first the rules
        in priority order, then the default route last (an entry with
        ``match_type="default"``), which everything not matched by a rule
        resolves to. ``upstream_model`` is included in the entry only when
        the rule sets it -- otherwise the log line gets bloated with useless
        ``None`` fields.

        Returns:
            A list of route entries in resolution order: ``match_type``,
            ``match_value``, ``provider``, and an optional
            ``upstream_model`` for each rule, plus a trailing entry for the
            default route.
        """
        routes: list[dict[str, str]] = []
        for rule in self.rules:
            entry = {
                "match_type": rule.match.type,
                "match_value": rule.match.value,
                "provider": rule.provider,
            }
            if rule.upstream_model is not None:
                entry["upstream_model"] = rule.upstream_model
            routes.append(entry)
        routes.append({"match_type": "default", "provider": self.default_provider})
        return routes

    async def close_all(self) -> None:
        """Close all providers."""
        for provider in self.providers.values():
            await provider.aclose()
