"""Unit tests for route resolution via ``ProviderRegistry.resolve``."""

from __future__ import annotations

import pytest

from routing.config_loader import load_routing_config
from routing.registry import ProviderRegistry
from settings import Settings


@pytest.fixture
def registry(_env: None) -> ProviderRegistry:  # noqa: PT019
    """A registry built from the test ``routing_test.yaml``."""
    settings = Settings()
    return ProviderRegistry.build(
        load_routing_config(settings.routing.config_path), settings
    )


def test_registry_resolves_claude_prefix_to_anthropic_passthrough(
    registry: ProviderRegistry,
) -> None:
    decision = registry.resolve("claude-opus-4-8")
    assert decision.provider.name == "anthropic"
    assert decision.provider.cfg.type == "passthrough"
    assert decision.upstream_model is None


def test_registry_resolves_glm_to_openai_compatible_translate(
    registry: ProviderRegistry,
) -> None:
    decision = registry.resolve("zai-org/GLM-5.2-FP8")
    assert decision.provider.name == "openai_compatible"
    assert decision.provider.cfg.type == "openai-translate"
    assert decision.upstream_model == "zai-org/GLM-5.2-FP8"


def test_registry_falls_back_to_default_for_unknown_model(
    registry: ProviderRegistry,
) -> None:
    decision = registry.resolve("totally-unknown-model")
    assert decision.provider.name == "anthropic"
    assert decision.provider.cfg.type == "passthrough"
    assert decision.upstream_model is None


def test_registry_resolves_gpt_prefix_to_openai_translate(
    registry: ProviderRegistry,
) -> None:
    decision = registry.resolve("gpt-5.6-sol")
    assert decision.provider.name == "openai"
    assert decision.provider.cfg.type == "openai-translate"
    assert decision.provider.cfg.token_param == "max_completion_tokens"
    assert decision.upstream_model is None


def test_registry_falls_back_to_default_for_mistral_model(
    registry: ProviderRegistry,
) -> None:
    decision = registry.resolve("mistral-large-latest")
    assert decision.provider.name == "anthropic"
    assert decision.upstream_model is None


def test_registry_first_rule_wins(registry: ProviderRegistry) -> None:
    # "claude-GLM-hybrid" matches both the "claude-" prefix and the "GLM" contains rule.
    # The first rule (anthropic) must win.
    decision = registry.resolve("claude-GLM-hybrid")
    assert decision.provider.name == "anthropic"


def test_describe_routes_lists_rules_in_priority_order_then_default(
    registry: ProviderRegistry,
) -> None:
    routes = registry.describe_routes()
    assert routes == [
        {"match_type": "prefix", "match_value": "claude-", "provider": "anthropic"},
        {
            "match_type": "contains",
            "match_value": "GLM",
            "provider": "openai_compatible",
            "upstream_model": "zai-org/GLM-5.2-FP8",
            "max_tokens_limit": 65536,
        },
        {
            "match_type": "prefix",
            "match_value": "gpt-",
            "provider": "openai",
            "max_tokens_limit": 128000,
        },
        {
            "match_type": "prefix",
            "match_value": "chat-",
            "provider": "openai_chat",
            "max_tokens_limit": 128000,
        },
        {"match_type": "default", "provider": "anthropic"},
    ]


def test_describe_routes_omits_upstream_model_when_not_set(
    registry: ProviderRegistry,
) -> None:
    claude_route = registry.describe_routes()[0]
    assert "upstream_model" not in claude_route


def test_describe_routes_includes_upstream_model_when_set(
    registry: ProviderRegistry,
) -> None:
    glm_route = registry.describe_routes()[1]
    assert glm_route["upstream_model"] == "zai-org/GLM-5.2-FP8"


def test_describe_routes_last_entry_is_default(registry: ProviderRegistry) -> None:
    routes = registry.describe_routes()
    assert routes[-1] == {"match_type": "default", "provider": registry.default_provider}


def test_resolve_falls_back_to_the_provider_limits_without_a_rule_override(
    registry: ProviderRegistry,
) -> None:
    """A rule that overrides nothing hands the provider's own numbers to the provider."""
    decision = registry.resolve("zai-org/GLM-5.2-FP8")
    assert decision.limits.max_tokens_limit == 65536
    assert decision.limits.context_window is None


def test_resolve_and_describe_routes_carry_a_rule_limit_override(
    _env: None,  # noqa: PT019
) -> None:
    """A per-model override reaches both the routing decision and the route table.

    Two models on one gateway differ only by these numbers, so the decision
    must carry the rule's values, not the provider block's.
    """
    settings = Settings()
    config = load_routing_config(settings.routing.config_path)
    config.rules[1].max_tokens_limit = 32000
    config.rules[1].context_window = 223680
    registry = ProviderRegistry.build(config, settings)

    decision = registry.resolve("zai-org/GLM-5.2-FP8")

    assert decision.limits.max_tokens_limit == 32000
    assert decision.limits.context_window == 223680
    assert registry.providers["openai_compatible"].cfg.max_tokens_limit == 65536
    assert registry.describe_routes()[1]["max_tokens_limit"] == 32000
    assert registry.describe_routes()[1]["context_window"] == 223680
