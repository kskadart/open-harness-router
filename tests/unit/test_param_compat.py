"""Unit tests for OpenAI request parameter compatibility for reasoning models."""

from __future__ import annotations

from typing import Any

import pytest

from providers.openai_translate import apply_param_compat
from routing.schema import ProviderCfg


def _base_openai_request() -> dict[str, Any]:
    """A typical OpenAI request, as built by convert_claude_to_openai."""
    return {
        "model": "gpt-5.6-sol",
        "messages": [{"role": "user", "content": "hi"}],
        "max_tokens": 128,
        "temperature": 0.7,
        "top_p": 0.9,
        "stream": False,
    }


def test_apply_param_compat_renames_max_tokens_when_token_param_switched() -> None:
    cfg = ProviderCfg(
        type="openai-translate",
        base_url="https://api.openai.com/v1",
        api_key_env="OPENAI_API_KEY",
        token_param="max_completion_tokens",
        max_tokens_limit=128000,
    )
    request = _base_openai_request()
    result = apply_param_compat(request, cfg)
    assert "max_tokens" not in result
    assert result["max_completion_tokens"] == 128
    # temperature/top_p must not be touched if drop_params is empty
    assert result["temperature"] == 0.7
    assert result["top_p"] == 0.9


def test_apply_param_compat_drops_only_listed_params_and_preserves_rest() -> None:
    cfg = ProviderCfg(
        type="openai-translate",
        base_url="https://api.openai.com/v1",
        api_key_env="OPENAI_API_KEY",
        drop_params=["temperature", "top_p"],
        max_tokens_limit=128000,
    )
    request = _base_openai_request()
    result = apply_param_compat(request, cfg)
    assert "temperature" not in result
    assert "top_p" not in result
    # The remaining keys must not disappear, including max_tokens (token_param unchanged)
    assert result["model"] == "gpt-5.6-sol"
    assert result["messages"] == [{"role": "user", "content": "hi"}]
    assert result["max_tokens"] == 128
    assert result["stream"] is False


def test_apply_param_compat_default_cfg_leaves_request_intact() -> None:
    cfg = ProviderCfg(
        type="openai-translate",
        base_url="https://api.openai.com/v1",
        api_key_env="OPENAI_API_KEY",
        max_tokens_limit=128000,
    )
    request = _base_openai_request()
    before = dict(request)
    result = apply_param_compat(request, cfg)
    assert result == before


def test_apply_param_compat_missing_max_tokens_does_not_add_completion_key() -> None:
    cfg = ProviderCfg(
        type="openai-translate",
        base_url="https://api.openai.com/v1",
        api_key_env="OPENAI_API_KEY",
        token_param="max_completion_tokens",
        max_tokens_limit=128000,
    )
    request = _base_openai_request()
    request.pop("max_tokens")
    result = apply_param_compat(request, cfg)
    assert "max_completion_tokens" not in result
    assert "max_tokens" not in result


def test_provider_cfg_rejects_forbidden_drop_params() -> None:
    with pytest.raises(ValueError, match="drop_params contains forbidden entries"):
        ProviderCfg(
            type="openai-translate",
            base_url="https://api.openai.com/v1",
            api_key_env="OPENAI_API_KEY",
            drop_params=["messages"],
            max_tokens_limit=128000,
        )


def test_provider_cfg_accepts_valid_token_param() -> None:
    cfg = ProviderCfg(
        type="openai-translate",
        base_url="https://api.openai.com/v1",
        api_key_env="OPENAI_API_KEY",
        token_param="max_completion_tokens",
        max_tokens_limit=128000,
    )
    assert cfg.token_param == "max_completion_tokens"


def test_provider_cfg_rejects_invalid_token_param() -> None:
    with pytest.raises(ValueError, match="token_param"):
        ProviderCfg(
            type="openai-translate",
            base_url="https://api.openai.com/v1",
            api_key_env="OPENAI_API_KEY",
            token_param="max_thinking_tokens",  # type: ignore[arg-type]
            max_tokens_limit=128000,
        )
