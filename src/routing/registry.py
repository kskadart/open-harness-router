"""Provider registry and route resolution by model name."""

from __future__ import annotations

from dataclasses import dataclass

from log import get_logger
from providers.base import Provider
from providers.factory import build_provider
from routing.matcher import match_model
from routing.schema import RouteLimits, RoutingConfig, RoutingRule
from settings import Settings

logger = get_logger(__name__)


@dataclass(frozen=True)
class RouteDecision:
    """The result of route resolution.

    Attributes:
        provider: the selected provider.
        upstream_model: model name for the upstream (None -> keep the original).
        limits: token limits for this route -- the matched rule's overrides
            folded onto the provider's own values.
    """

    provider: Provider
    upstream_model: str | None
    limits: RouteLimits


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
                provider = self.providers[rule.provider]
                return RouteDecision(
                    provider,
                    rule.upstream_model,
                    RouteLimits.resolve(provider.cfg, rule),
                )
        default = self.providers[self.default_provider]
        return RouteDecision(default, None, RouteLimits.resolve(default.cfg, None))

    def describe_routes(self) -> list[dict[str, str | int]]:
        """Build a compact description of the routing table for the startup log.

        Entry order mirrors the check order in ``resolve``: first the rules
        in priority order, then the default route last (an entry with
        ``match_type="default"``), which everything not matched by a rule
        resolves to. ``upstream_model`` and the two limits are included in
        the entry only when the route has them -- otherwise the log line
        gets bloated with useless ``None`` fields. The limits are the
        EFFECTIVE ones, so a rule that overrides its provider's numbers is
        visible in the log and in ``cli.validate_routing``.

        Returns:
            A list of route entries in resolution order: ``match_type``,
            ``match_value``, ``provider``, and the optional
            ``upstream_model``, ``max_tokens_limit`` and ``context_window``
            for each rule, plus a trailing entry for the default route.
        """
        routes: list[dict[str, str | int]] = []
        for rule in self.rules:
            entry: dict[str, str | int] = {
                "match_type": rule.match.type,
                "match_value": rule.match.value,
                "provider": rule.provider,
            }
            if rule.upstream_model is not None:
                entry["upstream_model"] = rule.upstream_model
            limits = RouteLimits.resolve(self.providers[rule.provider].cfg, rule)
            if limits.max_tokens_limit is not None:
                entry["max_tokens_limit"] = limits.max_tokens_limit
            if limits.context_window is not None:
                entry["context_window"] = limits.context_window
            routes.append(entry)
        routes.append({"match_type": "default", "provider": self.default_provider})
        return routes

    async def close_all(self) -> None:
        """Close all providers."""
        for provider in self.providers.values():
            await provider.aclose()
