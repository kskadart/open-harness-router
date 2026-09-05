"""Unit tests for the local token estimator applied to converted OpenAI payloads.

The estimator runs on the wire shape the converters produce (Chat
Completions ``messages``/``tools`` or Responses ``instructions``/``input``/
flat ``tools``), so the fixtures below are those dicts, not Anthropic
models. Absolute expectations encode the calibrated heuristic: 3.5 ASCII
characters per token, 2 non-ASCII characters per token, 4 tokens per
message, 8 per tool definition, 1600 per image, rounded up.
"""

from __future__ import annotations

import json
import math
from typing import Any

from services.token_estimator import estimate_openai_request_tokens

_ASCII_PER_TOKEN = 3.5
_NON_ASCII_PER_TOKEN = 2.0
_MESSAGE_OVERHEAD = 4
_TOOL_OVERHEAD = 8
_IMAGE_TOKENS = 1600
_HUGE_DATA_URL = "data:image/png;base64," + "A" * 100_000
_TOOL_PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {"path": {"type": "string", "description": "Absolute path"}},
    "required": ["path"],
}


def _chat_request(
    messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None
) -> dict[str, Any]:
    """Chat Completions request as ``convert_claude_to_openai`` emits it."""
    request: dict[str, Any] = {
        "model": "glm",
        "messages": messages,
        "max_tokens": 100,
        "temperature": 1.0,
        "stream": False,
    }
    if tools is not None:
        request["tools"] = tools
    return request


def _chat_tool(name: str, description: str) -> dict[str, Any]:
    """Nested Chat Completions tool definition."""
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": _TOOL_PARAMETERS,
        },
    }


def _responses_request(
    input_items: list[dict[str, Any]],
    *,
    instructions: str | None = None,
    tools: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Responses API request as ``convert_claude_to_responses`` emits it."""
    request: dict[str, Any] = {
        "model": "gpt-5.6",
        "input": input_items,
        "max_output_tokens": 100,
        "reasoning": {"effort": "medium"},
        "stream": False,
    }
    if instructions is not None:
        request["instructions"] = instructions
    if tools is not None:
        request["tools"] = tools
    return request


def _responses_tool(name: str, description: str) -> dict[str, Any]:
    """Flat Responses API tool definition."""
    return {
        "type": "function",
        "name": name,
        "description": description,
        "parameters": _TOOL_PARAMETERS,
    }


def _compact_json(value: object) -> str:
    """The serialization the estimator must use for definitions and arguments."""
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False)


def _ascii_tokens(chars: int) -> int:
    return math.ceil(chars / _ASCII_PER_TOKEN)


def test_estimate_request_without_messages_returns_zero() -> None:
    assert estimate_openai_request_tokens({"model": "glm"}) == 0


def test_estimate_chat_ascii_text_uses_calibrated_divisor_and_message_overhead() -> None:
    # 350 ASCII chars / 3.5 = 100 tokens, plus 4 for the single message.
    request = _chat_request([{"role": "user", "content": "a" * 350}])
    assert estimate_openai_request_tokens(request) == 100 + _MESSAGE_OVERHEAD


def test_estimate_chat_non_ascii_text_weighs_more_than_ascii_of_same_length() -> None:
    # 350 Cyrillic chars / 2 = 175 tokens, plus 4 for the single message.
    cyrillic = _chat_request([{"role": "user", "content": "я" * 350}])
    latin = _chat_request([{"role": "user", "content": "a" * 350}])
    assert estimate_openai_request_tokens(cyrillic) == 175 + _MESSAGE_OVERHEAD
    assert estimate_openai_request_tokens(cyrillic) > estimate_openai_request_tokens(latin)


def test_estimate_chat_system_and_user_messages_each_add_overhead() -> None:
    request = _chat_request(
        [
            {"role": "system", "content": "a" * 35},
            {"role": "user", "content": "b" * 35},
        ]
    )
    assert estimate_openai_request_tokens(request) == _ascii_tokens(70) + 2 * _MESSAGE_OVERHEAD


def test_estimate_chat_list_content_text_parts_are_counted() -> None:
    request = _chat_request(
        [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "a" * 35},
                    {"type": "text", "text": "b" * 35},
                ],
            }
        ]
    )
    assert estimate_openai_request_tokens(request) == _ascii_tokens(70) + _MESSAGE_OVERHEAD


def test_estimate_chat_tools_present_adds_compact_definition_text_and_overhead() -> None:
    tools = [
        _chat_tool("read_file", "Read a text file from the workspace."),
        _chat_tool("write_file", "Create or overwrite a text file."),
    ]
    definition_chars = sum(len(_compact_json(tool["function"])) for tool in tools)
    without_tools = _chat_request([{"role": "user", "content": ""}])
    with_tools = _chat_request([{"role": "user", "content": ""}], tools=tools)

    assert estimate_openai_request_tokens(without_tools) == _MESSAGE_OVERHEAD
    assert estimate_openai_request_tokens(with_tools) == (
        _ascii_tokens(definition_chars) + _MESSAGE_OVERHEAD + 2 * _TOOL_OVERHEAD
    )


def test_estimate_chat_tool_result_message_counts_content() -> None:
    request = _chat_request(
        [{"role": "tool", "tool_call_id": "call_1", "content": "x" * 70}]
    )
    assert estimate_openai_request_tokens(request) == _ascii_tokens(70) + _MESSAGE_OVERHEAD


def test_estimate_chat_tool_call_arguments_and_name_are_counted() -> None:
    arguments = json.dumps({"path": "/tmp/" + "a" * 100}, ensure_ascii=False)
    request = _chat_request(
        [
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "read_file", "arguments": arguments},
                    }
                ],
            }
        ]
    )
    expected_chars = len("read_file") + len(arguments)
    assert estimate_openai_request_tokens(request) == (
        _ascii_tokens(expected_chars) + _MESSAGE_OVERHEAD
    )


def test_estimate_chat_image_data_url_excluded_and_fixed_cost_added() -> None:
    request = _chat_request(
        [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "a" * 35},
                    {"type": "image_url", "image_url": {"url": _HUGE_DATA_URL}},
                ],
            }
        ]
    )
    assert estimate_openai_request_tokens(request) == (
        _ascii_tokens(35) + _MESSAGE_OVERHEAD + _IMAGE_TOKENS
    )


def test_estimate_responses_shape_counts_instructions_input_and_tools() -> None:
    tool = _responses_tool("read_file", "Read a text file from the workspace.")
    request = _responses_request(
        [
            {"role": "user", "content": [{"type": "input_text", "text": "b" * 70}]},
            {"type": "function_call", "call_id": "c1", "name": "read_file", "arguments": "{}"},
            {"type": "function_call_output", "call_id": "c1", "output": "c" * 35},
            {"role": "assistant", "content": "d" * 35},
        ],
        instructions="a" * 35,
        tools=[tool],
    )
    definition_chars = len(
        _compact_json(
            {
                "name": tool["name"],
                "description": tool["description"],
                "parameters": tool["parameters"],
            }
        )
    )
    text_chars = 35 + 70 + len("read_file") + len("{}") + 35 + 35
    # instructions + 4 input items = 5 message overheads
    assert estimate_openai_request_tokens(request) == (
        _ascii_tokens(text_chars + definition_chars) + 5 * _MESSAGE_OVERHEAD + _TOOL_OVERHEAD
    )


def test_estimate_responses_input_image_excluded_and_fixed_cost_added() -> None:
    request = _responses_request(
        [
            {
                "role": "user",
                "content": [
                    {"type": "input_text", "text": "a" * 35},
                    {"type": "input_image", "image_url": _HUGE_DATA_URL},
                ],
            }
        ]
    )
    assert estimate_openai_request_tokens(request) == (
        _ascii_tokens(35) + _MESSAGE_OVERHEAD + _IMAGE_TOKENS
    )


def test_estimate_responses_cached_reasoning_item_is_skipped() -> None:
    user_item = {"role": "user", "content": "a" * 35}
    reasoning_item = {
        "type": "reasoning",
        "id": "rs_1",
        "summary": [],
        "encrypted_content": "Z" * 5_000,
    }
    without_reasoning = _responses_request([user_item])
    with_reasoning = _responses_request([reasoning_item, user_item])
    assert estimate_openai_request_tokens(with_reasoning) == estimate_openai_request_tokens(
        without_reasoning
    )


def test_estimate_responses_non_ascii_output_text_weighted() -> None:
    request = _responses_request(
        [{"type": "function_call_output", "call_id": "c1", "output": "я" * 70}]
    )
    assert estimate_openai_request_tokens(request) == (
        math.ceil(70 / _NON_ASCII_PER_TOKEN) + _MESSAGE_OVERHEAD
    )
