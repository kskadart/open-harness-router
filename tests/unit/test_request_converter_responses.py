"""Unit tests for converting an Anthropic request -> OpenAI Responses API.

The expected request shape was captured empirically from /v1/responses
(gpt-5.6-sol): flat ``tools``, ``instructions`` for the system prompt,
``input_text``/``input_image`` in user content, ``function_call``/
``function_call_output`` instead of the ``tool`` role.
"""

from __future__ import annotations

from typing import Any

import pytest

from conversion.request_converter import convert_claude_to_responses
from models.claude import ClaudeMessagesRequest
from services.reasoning_cache import ReasoningCache

_MODEL = "gpt-5.6-sol"


def _build_request(**overrides: Any) -> ClaudeMessagesRequest:
    """A minimal valid Anthropic request with overridable fields."""
    payload: dict[str, Any] = {
        "model": "claude-sonnet-4",
        "max_tokens": 1024,
        "messages": [{"role": "user", "content": "Hello"}],
    }
    payload.update(overrides)
    return ClaudeMessagesRequest.model_validate(payload)


def _convert(request: ClaudeMessagesRequest, **overrides: Any) -> dict[str, Any]:
    """Conversion with the provider's default limits."""
    kwargs: dict[str, Any] = {
        "max_tokens_limit": 8192,
        "reasoning_effort_fallback": "medium",
        "reasoning_cache": ReasoningCache(),
    }
    kwargs.update(overrides)
    return convert_claude_to_responses(request, _MODEL, **kwargs)


@pytest.mark.parametrize("effort", ["low", "medium", "high", "xhigh", "max"])
def test_output_config_effort_passed_to_reasoning_without_translation(effort: str) -> None:
    """The level from output_config passes through to reasoning.effort verbatim."""
    request = _build_request(output_config={"effort": effort})
    result = _convert(request, reasoning_effort_fallback="none")
    assert result["reasoning"] == {"effort": effort}


def test_missing_output_config_uses_provider_fallback_effort() -> None:
    """Without output_config, the reasoning level comes from the provider config."""
    result = _convert(_build_request(), reasoning_effort_fallback="high")
    assert result["reasoning"] == {"effort": "high"}


def test_output_config_without_effort_uses_provider_fallback() -> None:
    """output_config with an empty effort does not override the provider fallback."""
    request = _build_request(output_config={})
    result = _convert(request, reasoning_effort_fallback="xhigh")
    assert result["reasoning"] == {"effort": "xhigh"}


def test_string_message_converted_to_input_item_with_role() -> None:
    """A string message is carried into input as-is."""
    result = _convert(_build_request())
    assert result["input"] == [{"role": "user", "content": "Hello"}]
    assert "messages" not in result


def test_text_blocks_converted_to_input_text_parts() -> None:
    """Text blocks become input_text parts (upstream rejects type text)."""
    request = _build_request(
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "first"},
                    {"type": "text", "text": "second"},
                ],
            }
        ]
    )
    result = _convert(request)
    assert result["input"] == [
        {
            "role": "user",
            "content": [
                {"type": "input_text", "text": "first"},
                {"type": "input_text", "text": "second"},
            ],
        }
    ]


def test_image_block_converted_to_input_image_with_data_url_string() -> None:
    """An image is carried as input_image with a string data URL."""
    request = _build_request(
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "what is this"},
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/png",
                            "data": "QUJD",
                        },
                    },
                ],
            }
        ]
    )
    result = _convert(request)
    parts = result["input"][0]["content"]
    assert parts[1] == {
        "type": "input_image",
        "image_url": "data:image/png;base64,QUJD",
    }


def test_assistant_message_converted_to_assistant_input_item() -> None:
    """Assistant text is carried as a separate input item."""
    request = _build_request(
        messages=[
            {"role": "user", "content": "question"},
            {"role": "assistant", "content": [{"type": "text", "text": "answer"}]},
            {"role": "user", "content": "more"},
        ]
    )
    result = _convert(request)
    assert result["input"] == [
        {"role": "user", "content": "question"},
        {"role": "assistant", "content": "answer"},
        {"role": "user", "content": "more"},
    ]


def test_tool_use_and_tool_result_converted_to_flat_function_call_items() -> None:
    """A tool_use/tool_result pair unfolds into function_call and its output."""
    request = _build_request(
        messages=[
            {"role": "user", "content": "weather in Paris"},
            {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "checking"},
                    {
                        "type": "tool_use",
                        "id": "toolu_01",
                        "name": "get_weather",
                        "input": {"city": "Paris"},
                    },
                ],
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "toolu_01",
                        "content": "21C, sunny",
                    }
                ],
            },
        ]
    )
    result = _convert(request)
    assert result["input"] == [
        {"role": "user", "content": "weather in Paris"},
        {"role": "assistant", "content": "checking"},
        {
            "type": "function_call",
            "call_id": "toolu_01",
            "name": "get_weather",
            "arguments": '{"city": "Paris"}',
        },
        {
            "type": "function_call_output",
            "call_id": "toolu_01",
            "output": "21C, sunny",
        },
    ]


def test_tool_result_precedes_user_text_in_same_message() -> None:
    """function_call_output comes before the text part of the same message."""
    request = _build_request(
        messages=[
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "toolu_02",
                        "name": "ping",
                        "input": {},
                    }
                ],
            },
            {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "toolu_02", "content": "pong"},
                    {"type": "text", "text": "continue"},
                ],
            },
        ]
    )
    result = _convert(request)
    assert [item.get("type") or item["role"] for item in result["input"]] == [
        "function_call",
        "function_call_output",
        "user",
    ]


def test_top_level_system_string_goes_to_instructions() -> None:
    """A top-level system string is carried into instructions, not input."""
    request = _build_request(system="You are an assistant.")
    result = _convert(request)
    assert result["instructions"] == "You are an assistant."
    assert result["input"] == [{"role": "user", "content": "Hello"}]


def test_top_level_system_blocks_joined_into_instructions() -> None:
    """An array system is joined into a single instructions string."""
    request = _build_request(
        system=[
            {"type": "text", "text": "First block"},
            {"type": "text", "text": "Second block"},
        ]
    )
    result = _convert(request)
    assert result["instructions"] == "First block\n\nSecond block"


def test_blank_system_does_not_add_instructions_key() -> None:
    """A blank system does not create the instructions key."""
    result = _convert(_build_request(system="   "))
    assert "instructions" not in result


def test_system_role_message_stays_inline_in_input() -> None:
    """A system-role message inside messages keeps its position in input."""
    request = _build_request(
        messages=[
            {"role": "user", "content": "one"},
            {"role": "system", "content": "be brief"},
            {"role": "user", "content": "two"},
        ]
    )
    result = _convert(request)
    assert result["input"] == [
        {"role": "user", "content": "one"},
        {"role": "system", "content": "be brief"},
        {"role": "user", "content": "two"},
    ]


def test_tools_converted_to_flat_form_without_nested_function_key() -> None:
    """tools are assembled in flat form: name/description/parameters at the top level."""
    schema = {"type": "object", "properties": {"city": {"type": "string"}}}
    request = _build_request(
        tools=[{"name": "get_weather", "description": "Weather", "input_schema": schema}]
    )
    result = _convert(request)
    assert result["tools"] == [
        {
            "type": "function",
            "name": "get_weather",
            "description": "Weather",
            "parameters": schema,
        }
    ]
    assert "function" not in result["tools"][0]


def test_tool_without_description_gets_empty_description() -> None:
    """A tool without a description gets an empty string, not None."""
    request = _build_request(tools=[{"name": "ping", "input_schema": {}}])
    result = _convert(request)
    assert result["tools"][0]["description"] == ""


def test_missing_tools_does_not_create_empty_array() -> None:
    """Without tools, no tools key appears in the request."""
    result = _convert(_build_request())
    assert "tools" not in result


def test_blank_tool_names_are_skipped_and_tools_key_omitted() -> None:
    """Tools with an empty name are dropped, an empty array is not created."""
    request = _build_request(tools=[{"name": "   ", "input_schema": {}}])
    result = _convert(request)
    assert "tools" not in result


def test_tool_choice_auto_converted_to_auto() -> None:
    """tool_choice type=auto -> the string auto."""
    result = _convert(_build_request(tool_choice={"type": "auto"}))
    assert result["tool_choice"] == "auto"


def test_tool_choice_any_converted_to_required() -> None:
    """tool_choice type=any requires a tool call -> required."""
    result = _convert(_build_request(tool_choice={"type": "any"}))
    assert result["tool_choice"] == "required"


def test_tool_choice_named_tool_converted_to_flat_function_choice() -> None:
    """A specific tool -> the flat {type: function, name: ...} shape."""
    request = _build_request(tool_choice={"type": "tool", "name": "get_weather"})
    result = _convert(request)
    assert result["tool_choice"] == {"type": "function", "name": "get_weather"}


def test_tool_choice_unknown_type_falls_back_to_auto() -> None:
    """An unknown tool_choice type -> auto."""
    result = _convert(_build_request(tool_choice={"type": "bogus"}))
    assert result["tool_choice"] == "auto"


def test_missing_tool_choice_does_not_create_key() -> None:
    """Without tool_choice, no key appears in the request."""
    result = _convert(_build_request())
    assert "tool_choice" not in result


def test_max_tokens_renamed_to_max_output_tokens() -> None:
    """max_tokens is carried into the Responses API's max_output_tokens."""
    result = _convert(_build_request(max_tokens=2048))
    assert result["max_output_tokens"] == 2048
    assert "max_tokens" not in result
    assert "max_completion_tokens" not in result


def test_max_output_tokens_clamped_to_provider_upper_limit() -> None:
    """A value above the provider's ceiling is clamped to max_tokens_limit."""
    result = _convert(_build_request(max_tokens=100000), max_tokens_limit=8192)
    assert result["max_output_tokens"] == 8192


def test_max_output_tokens_raised_to_lower_limit() -> None:
    """A value below the lower bound is raised to min_tokens_limit."""
    result = _convert(_build_request(max_tokens=1))
    assert result["max_output_tokens"] == 100


def test_sampling_params_are_not_forwarded() -> None:
    """temperature/top_p/top_k are not forwarded: the reasoning model rejects them."""
    request = _build_request(temperature=0.7, top_p=0.9, top_k=40)
    result = _convert(request)
    assert "temperature" not in result
    assert "top_p" not in result
    assert "top_k" not in result


def test_stop_sequences_are_not_forwarded() -> None:
    """stop_sequences is not forwarded: the Responses API has no such parameter."""
    request = _build_request(stop_sequences=["\n\n"])
    result = _convert(request)
    assert "stop_sequences" not in result
    assert "stop" not in result


def test_stream_flag_is_normalized_to_bool() -> None:
    """The stream flag is normalized to bool in both directions."""
    assert _convert(_build_request(stream=True))["stream"] is True
    assert _convert(_build_request())["stream"] is False


def test_upstream_model_replaces_claude_model() -> None:
    """The upstream model name is sent in the request, not the original Claude model."""
    result = _convert(_build_request())
    assert result["model"] == _MODEL


# ---------------------------------------------------------------------------
# Replaying reasoning items before a tool call
# ---------------------------------------------------------------------------

_CALL_ID = "call_oiAhWSjbBKCPqmjzySa6gZmu"
_REASONING_ITEM = {
    "id": "rs_0226e3ff2e40c123006a63d584cd1c81929878ad4d01848b01",
    "type": "reasoning",
    "summary": [],
    "content": [],
    "encrypted_content": "gAAAAABo" + "x" * 256,
}


def _tool_cycle_messages() -> list[dict[str, Any]]:
    """A dialogue consisting of one tool-call cycle: request, call, result."""
    return [
        {"role": "user", "content": "Compute it"},
        {
            "role": "assistant",
            "content": [
                {"type": "tool_use", "id": _CALL_ID, "name": "probe", "input": {"index": 0}}
            ],
        },
        {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": _CALL_ID, "content": "4712"}
            ],
        },
    ]


def _cache_with_reasoning() -> ReasoningCache:
    """A cache where a reasoning item is already stored for ``_CALL_ID``."""
    cache = ReasoningCache()
    cache.store(_CALL_ID, [_REASONING_ITEM])
    return cache


def test_cached_reasoning_is_injected_directly_before_its_function_call() -> None:
    """Reasoning is placed directly before its ``function_call``."""
    request = _build_request(messages=_tool_cycle_messages())

    result = _convert(request, reasoning_cache=_cache_with_reasoning())
    types = [item.get("type") or item.get("role") for item in result["input"]]

    assert types == ["user", "reasoning", "function_call", "function_call_output"]


def test_injected_reasoning_carries_encrypted_content() -> None:
    """The injected item carries ``encrypted_content`` -- without it there is no point."""
    request = _build_request(messages=_tool_cycle_messages())

    result = _convert(request, reasoning_cache=_cache_with_reasoning())
    reasoning = next(item for item in result["input"] if item.get("type") == "reasoning")

    assert reasoning["encrypted_content"] == _REASONING_ITEM["encrypted_content"]
    assert reasoning["id"] == _REASONING_ITEM["id"]


def test_cache_miss_leaves_input_identical_to_previous_behaviour() -> None:
    """A cache miss injects nothing: the ``input`` shape stays as before."""
    request = _build_request(messages=_tool_cycle_messages())

    result = _convert(request, reasoning_cache=ReasoningCache())
    types = [item.get("type") or item.get("role") for item in result["input"]]

    assert types == ["user", "function_call", "function_call_output"]


def test_reasoning_is_injected_for_matching_call_id_only() -> None:
    """Items are injected only for the call whose ``call_id`` matched."""
    other_call_id = "call_other"
    messages = _tool_cycle_messages()
    messages[1]["content"].append(  # type: ignore[union-attr]
        {"type": "tool_use", "id": other_call_id, "name": "probe", "input": {"index": 1}}
    )
    messages[2]["content"].append(  # type: ignore[union-attr]
        {"type": "tool_result", "tool_use_id": other_call_id, "content": "8395"}
    )
    request = _build_request(messages=messages)

    result = _convert(request, reasoning_cache=_cache_with_reasoning())
    types = [item.get("type") or item.get("role") for item in result["input"]]

    assert types == [
        "user",
        "reasoning",
        "function_call",
        "function_call",
        "function_call_output",
        "function_call_output",
    ]


def test_assistant_text_and_reasoning_both_precede_the_function_call() -> None:
    """Assistant text does not break up the reasoning + ``function_call`` pair."""
    messages = _tool_cycle_messages()
    messages[1]["content"].insert(0, {"type": "text", "text": "Let me check"})  # type: ignore[union-attr]
    request = _build_request(messages=messages)

    result = _convert(request, reasoning_cache=_cache_with_reasoning())
    types = [item.get("type") or item.get("role") for item in result["input"]]

    assert types == [
        "user",
        "assistant",
        "reasoning",
        "function_call",
        "function_call_output",
    ]


def test_trailing_system_role_message_after_tool_result_stays_system_in_input() -> None:
    """The Responses converter keeps a closing system message as-is (the chat flavor does not)."""
    request = _build_request(
        messages=[*_tool_cycle_messages(), {"role": "system", "content": "be brief"}]
    )
    result = _convert(request)
    assert [item.get("type") or item["role"] for item in result["input"]] == [
        "user",
        "function_call",
        "function_call_output",
        "system",
    ]
    assert result["input"][-1] == {"role": "system", "content": "be brief"}
