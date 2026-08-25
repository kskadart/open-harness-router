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
    with pytest.raises(
        ValueError, match="default.provider must be passthrough with forward_client_auth=true"
    ):
        RoutingConfig.model_validate(raw)


def test_default_route_to_own_key_passthrough_provider_is_rejected() -> None:
    """A passthrough provider alone is not enough for the default route.

    Regression for the tightened invariant: passthrough used to be
    synonymous with "native Anthropic on the user's subscription", which
    stopped being true once passthrough also covers third-party upstreams
    billed on their own key (forward_client_auth=false). An unmatched model
    must never silently land on a paid third-party vendor.
    """
    raw = _clone_valid()
    raw["providers"]["moonshot"] = {  # type: ignore[index]
        "type": "passthrough",
        "base_url": "https://api.moonshot.ai/anthropic",
        "forward_client_auth": False,
        "api_key_env": "MOONSHOT_API_KEY",
    }
    raw["default"] = {"provider": "moonshot"}
    with pytest.raises(
        ValueError, match="default.provider must be passthrough with forward_client_auth=true"
    ):
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


def test_forward_client_auth_defaults_to_true() -> None:
    """Preserves pre-fix behavior for configs that don't set the flag.

    Before this field had any effect, every passthrough provider forwarded
    the client's credentials unconditionally, so the default must keep
    doing that.
    """
    cfg = RoutingConfig.model_validate(_clone_valid())
    assert cfg.providers["anthropic"].forward_client_auth is True


def test_passthrough_own_key_without_api_key_env_is_rejected() -> None:
    """passthrough with forward_client_auth=false requires api_key_env.

    Without the key the router would have nothing to inject and the client's
    own Claude Code OAuth token would keep being the only credential
    available -- exactly the leak this whole feature exists to prevent.
    """
    raw = _clone_valid()
    raw["providers"]["anthropic"]["forward_client_auth"] = False  # type: ignore[index]
    with pytest.raises(
        ValueError, match="passthrough with forward_client_auth=false requires 'api_key_env'"
    ):
        RoutingConfig.model_validate(raw)


def test_passthrough_forward_client_auth_true_with_api_key_env_is_rejected() -> None:
    """passthrough with forward_client_auth=true must not also set api_key_env.

    Before this validator, factory.py returned before resolving the key, so
    a stray api_key_env on a native-Anthropic provider was silently ignored
    -- a trap where the configured key quietly did nothing. Now it fails
    loudly at startup.
    """
    raw = _clone_valid()
    raw["providers"]["anthropic"]["forward_client_auth"] = True  # type: ignore[index]
    raw["providers"]["anthropic"]["api_key_env"] = "ANTHROPIC_API_KEY"  # type: ignore[index]
    with pytest.raises(
        ValueError, match="passthrough with forward_client_auth=true must not set 'api_key_env'"
    ):
        RoutingConfig.model_validate(raw)


def test_passthrough_own_key_with_api_key_env_and_auth_header_is_accepted() -> None:
    """A fully specified own-key passthrough provider validates and parses auth_header."""
    raw = _clone_valid()
    raw["providers"]["moonshot"] = {  # type: ignore[index]
        "type": "passthrough",
        "base_url": "https://api.moonshot.ai/anthropic",
        "forward_client_auth": False,
        "api_key_env": "MOONSHOT_API_KEY",
        "auth_header": "x-api-key",
    }
    cfg = RoutingConfig.model_validate(raw)
    assert cfg.providers["moonshot"].forward_client_auth is False
    assert cfg.providers["moonshot"].api_key_env == "MOONSHOT_API_KEY"
    assert cfg.providers["moonshot"].auth_header == "x-api-key"


def test_auth_header_defaults_to_bearer() -> None:
    """auth_header defaults to bearer when omitted (most third-party upstreams)."""
    cfg = RoutingConfig.model_validate(_clone_valid())
    assert cfg.providers["anthropic"].auth_header == "bearer"


def test_auth_header_invalid_value_is_rejected() -> None:
    """An invalid auth_header -> pydantic validation error."""
    raw = _clone_valid()
    raw["providers"]["anthropic"]["auth_header"] = "basic"  # type: ignore[index]
    with pytest.raises(ValueError, match="auth_header"):
        RoutingConfig.model_validate(raw)


def test_auth_header_set_with_forward_client_auth_true_is_rejected() -> None:
    """auth_header is meaningless (and rejected) on forward_client_auth=true.

    Same trap as api_key_env in that mode: a configured value that quietly
    does nothing, now a startup error instead.
    """
    raw = _clone_valid()
    raw["providers"]["anthropic"]["auth_header"] = "x-api-key"  # type: ignore[index]
    with pytest.raises(ValueError, match="auth_header has no effect"):
        RoutingConfig.model_validate(raw)


def test_auth_header_set_on_openai_translate_is_rejected() -> None:
    """auth_header is meaningless (and rejected) on openai-translate providers.

    A separate message from the forward_client_auth=true case: this branch
    names the wrong provider type, not the wrong passthrough mode, so
    "set forward_client_auth=false" (impossible on openai-translate) must
    not appear here.
    """
    raw = _clone_valid()
    raw["providers"]["openai_compatible"]["auth_header"] = "bearer"  # type: ignore[index]
    with pytest.raises(ValueError, match="auth_header only applies to passthrough"):
        RoutingConfig.model_validate(raw)


def test_auth_header_unset_with_forward_client_auth_true_is_accepted() -> None:
    """Not setting auth_header at all is fine on forward_client_auth=true (the default)."""
    cfg = RoutingConfig.model_validate(_clone_valid())
    assert cfg.providers["anthropic"].auth_header == "bearer"


def test_rule_upstream_model_on_passthrough_provider_is_rejected() -> None:
    """upstream_model on a rule pointing at a passthrough provider -> startup error.

    Byte-for-byte proxying forwards the request body (including the model
    field) unchanged and cannot rewrite it: a user copying the Kimi
    passthrough template who naturally writes upstream_model on the rule
    would otherwise get it silently dropped and a confusing model-not-found
    from the vendor.
    """
    raw = _clone_valid()
    raw["rules"][0]["upstream_model"] = "claude-opus-4-8-20260101"  # type: ignore[index]
    with pytest.raises(
        ValueError, match="upstream_model is not supported on passthrough"
    ):
        RoutingConfig.model_validate(raw)


def test_rule_upstream_model_on_openai_translate_provider_is_accepted() -> None:
    """Regression: upstream_model remains valid on openai-translate rules."""
    raw = _clone_valid()
    raw["rules"][1]["upstream_model"] = "zai-org/GLM-5.2-FP8"  # type: ignore[index]
    cfg = RoutingConfig.model_validate(raw)
    assert cfg.rules[1].upstream_model == "zai-org/GLM-5.2-FP8"


def test_forward_client_auth_true_with_api_key_env_message_offers_own_key_alternative() -> None:
    """The error for forward_client_auth=true + api_key_env also names the intended fix.

    The likeliest way to hit this branch (now that the default is true) is
    someone writing a NEW own-key third-party provider who set api_key_env
    and auth_header but forgot forward_client_auth: false -- the message
    must point at that fix, not just say "remove the key".
    """
    raw = _clone_valid()
    raw["providers"]["anthropic"]["forward_client_auth"] = True  # type: ignore[index]
    raw["providers"]["anthropic"]["api_key_env"] = "ANTHROPIC_API_KEY"  # type: ignore[index]
    with pytest.raises(ValueError, match="set forward_client_auth=false"):
        RoutingConfig.model_validate(raw)


def test_forward_client_auth_set_on_openai_translate_is_rejected() -> None:
    """forward_client_auth is meaningless (and rejected) on openai-translate providers.

    Same trap class as auth_header in the same position: _validate_passthrough_auth
    used to return early for non-passthrough, letting forward_client_auth=false
    on openai-translate validate and silently do nothing.
    """
    raw = _clone_valid()
    raw["providers"]["openai_compatible"]["forward_client_auth"] = False  # type: ignore[index]
    with pytest.raises(
        ValueError, match="forward_client_auth only applies to passthrough"
    ):
        RoutingConfig.model_validate(raw)


def test_forward_client_auth_unset_on_openai_translate_is_accepted() -> None:
    """Regression: not setting forward_client_auth at all is fine on openai-translate."""
    cfg = RoutingConfig.model_validate(_clone_valid())
    assert cfg.providers["openai_compatible"].forward_client_auth is True


def test_passthrough_extra_headers_naming_authorization_is_rejected() -> None:
    """extra_headers must not name authorization on a passthrough provider.

    Merged in AFTER this provider's auth handling (see
    providers/passthrough.py _build_headers), so it would silently override
    the auth header this provider forwards or injects.
    """
    raw = _clone_valid()
    raw["providers"]["anthropic"]["extra_headers"] = {"Authorization": "Bearer sneaky"}  # type: ignore[index]
    with pytest.raises(ValueError, match=r"extra_headers must not set \['authorization'\]"):
        RoutingConfig.model_validate(raw)


def test_passthrough_extra_headers_naming_x_api_key_is_rejected() -> None:
    """extra_headers must not name x-api-key on a passthrough provider (case-insensitive)."""
    raw = _clone_valid()
    raw["providers"]["anthropic"]["extra_headers"] = {"X-Api-Key": "sneaky-key"}  # type: ignore[index]
    with pytest.raises(ValueError, match=r"extra_headers must not set \['x-api-key'\]"):
        RoutingConfig.model_validate(raw)


def test_passthrough_extra_headers_other_names_are_accepted() -> None:
    """extra_headers with non-colliding names validates and parses on passthrough."""
    raw = _clone_valid()
    raw["providers"]["anthropic"]["extra_headers"] = {  # type: ignore[index]
        "HTTP-Referer": "https://example.com",
        "X-Title": "my-router",
    }
    cfg = RoutingConfig.model_validate(raw)
    assert cfg.providers["anthropic"].extra_headers == {
        "HTTP-Referer": "https://example.com",
        "X-Title": "my-router",
    }


def test_openai_translate_extra_headers_naming_authorization_is_rejected() -> None:
    """extra_headers must not name authorization on openai-translate either.

    Verified against the installed OpenAI SDK (openai/_base_client.py):
    default_headers (which folds in our extra_headers last) is merged in
    AFTER _auth_headers (the api_key_env-derived Authorization header), so
    extra_headers["Authorization"] silently wins over the configured key.
    The collision check is provider-agnostic for exactly this reason.
    """
    raw = _clone_valid()
    raw["providers"]["openai_compatible"]["extra_headers"] = {  # type: ignore[index]
        "Authorization": "Bearer sneaky"
    }
    with pytest.raises(ValueError, match=r"extra_headers must not set \['authorization'\]"):
        RoutingConfig.model_validate(raw)


def test_openai_translate_extra_headers_other_names_are_accepted() -> None:
    """Regression: non-colliding extra_headers remain valid on openai-translate."""
    raw = _clone_valid()
    raw["providers"]["openai_compatible"]["extra_headers"] = {  # type: ignore[index]
        "User-Agent": "open-harness-router/0.1"
    }
    cfg = RoutingConfig.model_validate(raw)
    assert cfg.providers["openai_compatible"].extra_headers == {
        "User-Agent": "open-harness-router/0.1"
    }


def test_tls_verify_hostname_defaults_to_true() -> None:
    """tls_verify_hostname defaults to true -- full verification including the hostname."""
    cfg = RoutingConfig.model_validate(_clone_valid())
    assert cfg.providers["anthropic"].tls_verify_hostname is True
    assert cfg.providers["openai_compatible"].tls_verify_hostname is True


def test_tls_verify_hostname_false_parsed_when_set() -> None:
    """tls_verify_hostname=false parses (internal dev gateway with a misissued leaf cert)."""
    raw = _clone_valid()
    raw["providers"]["openai_compatible"]["tls_verify_hostname"] = False  # type: ignore[index]
    cfg = RoutingConfig.model_validate(raw)
    assert cfg.providers["openai_compatible"].tls_verify_hostname is False
