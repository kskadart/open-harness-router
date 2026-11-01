"""Provider registry and route resolution by model name."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from log import get_logger
from providers.base import Provider
from providers.factory import build_provider
from routing.matcher import match_model
from routing.schema import (
    PricingCfg,
    RouteLimits,
    RoutingConfig,
    RoutingRule,
    effective_pricing,
)
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
        pricing: the prices the dashboard costs this route at, resolved the
            same way; ``None`` when neither the rule nor the provider sets
            them.
        pricing_source: where those prices came from -- ``"rule"`` when the
            matched rule sets its own, ``"provider"`` when the route
            inherits its provider's, ``None`` when neither sets prices.
    """

    provider: Provider
    upstream_model: str | None
    limits: RouteLimits
    pricing: PricingCfg | None = None
    pricing_source: str | None = None


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
                    effective_pricing(provider.cfg, rule),
                    self._pricing_source(rule),
                )
        default = self.providers[self.default_provider]
        return RouteDecision(
            default,
            None,
            RouteLimits.resolve(default.cfg, None),
            effective_pricing(default.cfg, None),
            "provider" if default.cfg.pricing is not None else None,
        )

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

    def resolve_pricing(self, model: str) -> dict[str, Any]:
        """Return the effective prices of one model and where they come from.

        The same resolution the request path uses (:meth:`resolve`): the
        first rule that matches the model decides, and the rule's own
        ``pricing:`` overrides its provider's. A model no rule matches
        lands on the default route, exactly as it would when it is
        requested.

        Args:
            model: model name from the request body.

        Returns:
            ``{"pricing": None, "pricing_source": None}`` or
            ``{"pricing": {...}, "pricing_source": "rule" | "provider"}``
            -- the effective rates per million tokens and the source of
            them. A model the registry cannot resolve at all (its
            provider is gone from the configuration) gets no prices and
            no source.
        """
        try:
            decision = self.resolve(model)
        except KeyError:
            return {"pricing": None, "pricing_source": None}
        pricing = decision.pricing
        return {
            "pricing": None if pricing is None else pricing.rates(),
            "pricing_source": decision.pricing_source,
        }

    def describe_pricing(self) -> dict[str, Any]:
        """Build the configured prices for the dashboard's cost model.

        Read by the dashboard only: the router never bills anything and
        these numbers never go upstream. Entry order mirrors
        ``resolve``/``describe_routes`` -- the rules in priority order,
        then the trailing default entry -- so a dashboard row lines up
        with the route it prices. ``pricing`` is the EFFECTIVE rate
        (``effective_pricing(...).rates()``), i.e. what the cost column
        actually charges that route at, and ``pricing_source`` says where
        those numbers came from: ``"rule"`` when the rule's own
        ``pricing:`` is in force, ``"provider"`` when the route inherits
        its provider's, and ``None`` (together with ``"pricing": None``)
        when neither sets prices. Both keys are always present, even when
        null, because the dashboard renders fixed columns and a missing
        key would read as a bug rather than as "not configured".

        Returns:
            ``unit`` (the rate unit), ``providers`` mapping every
            configured provider name to its effective rates (or ``None``
            when it sets no pricing), and ``routes`` listing each route
            with its ``match_type``, ``match_value``, ``provider``,
            ``pricing`` and ``pricing_source``; the default entry carries
            no ``match_value``.
        """
        return {
            "unit": "USD per 1M tokens",
            "providers": {
                name: None if provider.cfg.pricing is None else provider.cfg.pricing.rates()
                for name, provider in self.providers.items()
            },
            "routes": self._describe_pricing_routes(),
        }

    def _describe_pricing_routes(self) -> list[dict[str, Any]]:
        """Build one pricing entry per route, in ``resolve`` order.

        Returns:
            The rules in priority order followed by the trailing default
            entry, each with its resolved rates and their source.
        """
        routes: list[dict[str, Any]] = []
        for rule in self.rules:
            pricing = effective_pricing(self.providers[rule.provider].cfg, rule)
            routes.append(
                {
                    "match_type": rule.match.type,
                    "match_value": rule.match.value,
                    "provider": rule.provider,
                    "pricing": None if pricing is None else pricing.rates(),
                    "pricing_source": self._pricing_source(rule),
                }
            )
        default_cfg = self.providers[self.default_provider].cfg
        routes.append(
            {
                "match_type": "default",
                "provider": self.default_provider,
                "pricing": None if default_cfg.pricing is None else default_cfg.pricing.rates(),
                "pricing_source": "provider" if default_cfg.pricing is not None else None,
            }
        )
        return routes

    def _pricing_source(self, rule: RoutingRule) -> str | None:
        """Return where a rule's effective prices come from.

        Args:
            rule: the routing rule.

        Returns:
            ``"rule"`` when the rule sets its own ``pricing:``,
            ``"provider"`` when it inherits the provider's, else ``None``.
        """
        if rule.pricing is not None:
            return "rule"
        return "provider" if self.providers[rule.provider].cfg.pricing is not None else None

    async def close_all(self) -> None:
        """Close all providers."""
        for provider in self.providers.values():
            await provider.aclose()
