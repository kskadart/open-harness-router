"""Unit tests for ``RoutingConfig`` validation and YAML loading."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from errors import ConfigError
from routing.config_loader import load_routing_config
from routing.schema import RoutingConfig

_MINIMAL_VALID: dict[str, object] = {
    "version": 1,
    "providers": {
        "anthropic": {
            "type": "passthrough",
            "base_url": "https://api.anthropic.com",
        },
        "openai_compatible": {
            "type": "openai-translate",
            "base_url": "https://gateway.example.com/v1",
            "api_key_env": "OPENAI_COMPATIBLE_KEY",
            "max_tokens_limit": 65536,
        },
    },
    "rules": [
        {"match": {"type": "prefix", "value": "claude-"}, "provider": "anthropic"},
        {"match": {"type": "contains", "value": "GLM"}, "provider": "openai_compatible"},
    ],
    "default": {"provider": "anthropic"},
}


def _clone_valid() -> dict[str, object]:
    """Deep copy of the minimal-valid config for mutation in tests."""
    return yaml.safe_load(yaml.safe_dump(_MINIMAL_VALID))


def test_valid_config_loads_and_validates() -> None:
    cfg = RoutingConfig.model_validate(_clone_valid())
    assert cfg.default.provider == "anthropic"
    assert set(cfg.providers.keys()) == {"anthropic", "openai_compatible"}
    assert len(cfg.rules) == 2


def test_rule_references_unknown_provider_is_rejected() -> None:
    raw = _clone_valid()
    raw["rules"][0]["provider"] = "does-not-exist"  # type: ignore[index]
    with pytest.raises(ValueError, match="unknown provider"):
        RoutingConfig.model_validate(raw)


def test_openai_translate_without_api_key_env_is_rejected() -> None:
    raw = _clone_valid()
    raw["providers"]["openai_compatible"].pop("api_key_env")  # type: ignore[index]
    with pytest.raises(ValueError, match="openai-translate requires 'api_key_env'"):
        RoutingConfig.model_validate(raw)


def test_default_route_must_point_to_passthrough_provider() -> None:
    raw = _clone_valid()
    raw["default"] = {"provider": "openai_compatible"}
    with pytest.raises(ValueError, match="default.provider must be of type 'passthrough'"):
        RoutingConfig.model_validate(raw)


def test_default_route_to_unknown_provider_is_rejected() -> None:
    raw = _clone_valid()
    raw["default"] = {"provider": "ghost"}
    with pytest.raises(ValueError, match="unknown provider"):
        RoutingConfig.model_validate(raw)


def test_invalid_regex_value_is_rejected() -> None:
    raw = _clone_valid()
    raw["rules"][0]["match"] = {"type": "regex", "value": "([unclosed"}  # type: ignore[index]
    with pytest.raises(ValueError, match="invalid regex"):
        RoutingConfig.model_validate(raw)


def test_load_routing_config_missing_file_raises_config_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="routing config not found"):
        load_routing_config(tmp_path / "does_not_exist.yaml")


def test_load_routing_config_bad_yaml_raises_config_error(tmp_path: Path) -> None:
    bad = tmp_path / "bad.yaml"
    bad.write_text("::: not a valid: yaml : :\n  - [", encoding="utf-8")
    with pytest.raises(ConfigError, match="invalid YAML"):
        load_routing_config(bad)


def test_load_routing_config_bad_schema_raises_config_error(tmp_path: Path) -> None:
    bad = tmp_path / "bad.yaml"
    raw = _clone_valid()
    raw["default"] = {"provider": "openai_compatible"}  # breaks the passthrough invariant
    bad.write_text(yaml.safe_dump(raw), encoding="utf-8")
    with pytest.raises(ConfigError, match="invalid routing config"):
        load_routing_config(bad)


def test_load_routing_config_non_mapping_raises_config_error(tmp_path: Path) -> None:
    bad = tmp_path / "bad.yaml"
    bad.write_text("- just\n- a\n- list\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="must be a mapping"):
        load_routing_config(bad)


def test_load_routing_config_valid_yaml_returns_model(tmp_path: Path) -> None:
    good = tmp_path / "routing.yaml"
    good.write_text(yaml.safe_dump(_clone_valid()), encoding="utf-8")
    cfg = load_routing_config(good)
    assert cfg.default.provider == "anthropic"
    assert cfg.providers["openai_compatible"].api_key_env == "OPENAI_COMPATIBLE_KEY"


def test_provider_cfg_tools_max_defaults_to_zero() -> None:
    """tools_max defaults to 0 (no limit) -- does not break unbounded providers."""
    cfg = RoutingConfig.model_validate(_clone_valid())
    assert cfg.providers["anthropic"].tools_max == 0
    assert cfg.providers["openai_compatible"].tools_max == 0


def test_provider_cfg_accepts_tools_max() -> None:
    """ProviderCfg with tools_max=128 parses correctly."""
    raw = _clone_valid()
    raw["providers"]["openai_compatible"]["tools_max"] = 128  # type: ignore[index]
    cfg = RoutingConfig.model_validate(raw)
    assert cfg.providers["openai_compatible"].tools_max == 128


def test_project_routing_example_yaml_is_valid_minimal_example() -> None:
    """The repo's routing.example.yaml -- a minimal working example, not a live config.

    This is the only routing file guaranteed to be present in a clean clone:
    the personal routing.yaml is in .gitignore, is created by copying the
    example, and is absent from the repository. Out of the box only
    anthropic (passthrough, no key needed) is active; GLM/OpenAI/Kimi and
    the other fleet providers are commented out as templates. We check
    invariants that hold specifically for the example, not the presence of
    specific fleet providers -- that's what tests/fixtures/routing_test.yaml
    is for.
    """
    project_yaml = Path(__file__).resolve().parents[2] / "routing.example.yaml"
    cfg = load_routing_config(project_yaml)
    assert set(cfg.providers.keys()) == {"anthropic"}
    assert cfg.providers["anthropic"].type == "passthrough"
    assert cfg.providers["anthropic"].tools_max == 0
    assert cfg.default.provider == "anthropic"


def test_fixture_routing_yaml_openai_has_tools_max_and_anthropic_does_not() -> None:
    """tests/fixtures/routing_test.yaml: openai sets tools_max=128, anthropic does not (0).

    Uses the test fixture (fleet providers for regression tests), not the
    repo's routing.example.yaml -- see
    test_project_routing_example_yaml_is_valid_minimal_example.
    """
    fixture_yaml = Path(__file__).resolve().parents[1] / "fixtures" / "routing_test.yaml"
    cfg = load_routing_config(fixture_yaml)
    assert cfg.providers["openai"].tools_max == 128
    assert cfg.providers["anthropic"].tools_max == 0


def test_api_flavor_and_reasoning_effort_default_when_omitted() -> None:
    """The api_flavor/reasoning_effort fields default to chat/medium."""
    cfg = RoutingConfig.model_validate(_clone_valid())
    assert cfg.providers["anthropic"].api_flavor == "chat"
    assert cfg.providers["openai_compatible"].api_flavor == "chat"
    assert cfg.providers["anthropic"].reasoning_effort == "medium"
    assert cfg.providers["openai_compatible"].reasoning_effort == "medium"


def test_api_flavor_and_reasoning_effort_parsed_when_set() -> None:
    """Both fields parse when explicitly set on openai-translate."""
    raw = _clone_valid()
    raw["providers"]["openai_compatible"]["api_flavor"] = "responses"  # type: ignore[index]
    raw["providers"]["openai_compatible"]["reasoning_effort"] = "high"  # type: ignore[index]
    cfg = RoutingConfig.model_validate(raw)
    assert cfg.providers["openai_compatible"].api_flavor == "responses"
    assert cfg.providers["openai_compatible"].reasoning_effort == "high"


def test_api_flavor_responses_on_passthrough_is_rejected() -> None:
    """api_flavor=responses on passthrough -> ConfigError."""
    raw = _clone_valid()
    raw["providers"]["anthropic"]["api_flavor"] = "responses"  # type: ignore[index]
    with pytest.raises(ValueError, match="api_flavor='responses' requires type='openai-translate'"):
        RoutingConfig.model_validate(raw)


def test_api_flavor_invalid_value_is_rejected() -> None:
    """An invalid api_flavor -> pydantic validation error."""
    raw = _clone_valid()
    raw["providers"]["openai_compatible"]["api_flavor"] = "bogus"  # type: ignore[index]
    with pytest.raises(ValueError, match="api_flavor"):
        RoutingConfig.model_validate(raw)


@pytest.mark.parametrize("effort", ["none", "low", "medium", "high", "xhigh", "max"])
def test_reasoning_effort_accepts_every_upstream_level(effort: str) -> None:
    """All reasoning levels accepted by the Responses API pass validation."""
    raw = _clone_valid()
    raw["providers"]["openai_compatible"]["reasoning_effort"] = effort  # type: ignore[index]
    cfg = RoutingConfig.model_validate(raw)
    assert cfg.providers["openai_compatible"].reasoning_effort == effort


def test_reasoning_effort_invalid_value_is_rejected() -> None:
    """An invalid reasoning_effort -> pydantic validation error."""
    raw = _clone_valid()
    raw["providers"]["openai_compatible"]["reasoning_effort"] = "ultra"  # type: ignore[index]
    with pytest.raises(ValueError, match="reasoning_effort"):
        RoutingConfig.model_validate(raw)


def test_token_param_max_output_tokens_accepted() -> None:
    """token_param=max_output_tokens (Responses API) parses on openai-translate."""
    raw = _clone_valid()
    raw["providers"]["openai_compatible"]["token_param"] = "max_output_tokens"  # type: ignore[index]
    cfg = RoutingConfig.model_validate(raw)
    assert cfg.providers["openai_compatible"].token_param == "max_output_tokens"


def test_token_param_invalid_value_is_rejected() -> None:
    """An invalid token_param -> pydantic validation error."""
    raw = _clone_valid()
    raw["providers"]["openai_compatible"]["token_param"] = "max_thinking_tokens"  # type: ignore[index]
    with pytest.raises(ValueError, match="token_param"):
        RoutingConfig.model_validate(raw)


def test_existing_provider_defaults_unchanged() -> None:
    """Existing providers' defaults did not shift after the schema extension."""
    cfg = RoutingConfig.model_validate(_clone_valid())
    # anthropic and openai_compatible do not set token_param/drop_params explicitly -> defaults.
    assert cfg.providers["anthropic"].token_param == "max_tokens"
    assert cfg.providers["openai_compatible"].token_param == "max_tokens"
    assert cfg.providers["anthropic"].drop_params == []
    assert cfg.providers["openai_compatible"].drop_params == []


def test_openai_translate_without_max_tokens_limit_is_rejected() -> None:
    """openai-translate without max_tokens_limit -> ValueError (silent default is forbidden)."""
    raw = _clone_valid()
    raw["providers"]["openai_compatible"].pop("max_tokens_limit")  # type: ignore[index]
    with pytest.raises(ValueError, match="max_tokens_limit"):
        RoutingConfig.model_validate(raw)


def test_passthrough_without_max_tokens_limit_is_accepted() -> None:
    """passthrough without max_tokens_limit -> None, validation passes (not applicable)."""
    raw = _clone_valid()
    cfg = RoutingConfig.model_validate(raw)
    assert cfg.providers["anthropic"].max_tokens_limit is None


def test_max_tokens_limit_parsed_when_set() -> None:
    """An explicitly set max_tokens_limit parses on openai-translate."""
    raw = _clone_valid()
    raw["providers"]["openai_compatible"]["max_tokens_limit"] = 131072  # type: ignore[index]
    cfg = RoutingConfig.model_validate(raw)
    assert cfg.providers["openai_compatible"].max_tokens_limit == 131072
