"""Unit tests for mapping ``tool_choice`` when converting Anthropic -> Chat Completions.

The two protocols' ``tool_choice`` value sets do not match: Anthropic
distinguishes ``auto`` (the model decides whether to call) from ``any`` (a
call is required), which OpenAI expresses as ``auto`` and ``required``.
"""

from __future__ import annotations

from typing import Any

from conversion.request_converter import convert_claude_to_openai
from models.claude import ClaudeMessagesRequest

_MODEL = "glm-5.2"


def _build_request(**overrides: Any) -> ClaudeMessagesRequest:
    """A minimal valid Anthropic request with overridable fields."""
    payload: dict[str, Any] = {
        "model": "claude-sonnet-4",
        "max_tokens": 1024,
        "messages": [{"role": "user", "content": "Hello"}],
    }
    payload.update(overrides)
    return ClaudeMessagesRequest.model_validate(payload)


def _convert(request: ClaudeMessagesRequest) -> dict[str, Any]:
    """Conversion with the provider's default limits."""
    return convert_claude_to_openai(request, _MODEL, max_tokens_limit=8192)


def test_tool_choice_auto_converted_to_auto() -> None:
    """tool_choice type=auto leaves the call up to the model -> auto."""
    result = _convert(_build_request(tool_choice={"type": "auto"}))
    assert result["tool_choice"] == "auto"


def test_tool_choice_any_converted_to_required() -> None:
    """tool_choice type=any requires a tool call -> required."""
    result = _convert(_build_request(tool_choice={"type": "any"}))
    assert result["tool_choice"] == "required"


def test_tool_choice_named_tool_converted_to_nested_function_choice() -> None:
    """A specific tool -> the nested {type: function, function: {...}} shape."""
    request = _build_request(tool_choice={"type": "tool", "name": "get_weather"})
    result = _convert(request)
    assert result["tool_choice"] == {
        "type": "function",
        "function": {"name": "get_weather"},
    }


def test_tool_choice_unknown_type_falls_back_to_auto() -> None:
    """An unknown tool_choice type -> auto."""
    result = _convert(_build_request(tool_choice={"type": "bogus"}))
    assert result["tool_choice"] == "auto"


def test_missing_tool_choice_does_not_create_key() -> None:
    """Without tool_choice, no key appears in the request."""
    result = _convert(_build_request())
    assert "tool_choice" not in result
