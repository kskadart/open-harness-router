"""Unit tests for mapping ``tool_choice`` when converting Anthropic -> Chat Completions.

The two protocols' ``tool_choice`` value sets do not match: Anthropic
distinguishes ``auto`` (the model decides whether to call) from ``any`` (a
call is required), which OpenAI expresses as ``auto`` and ``required``.

The message-shaping tests pin the wire shape Claude Code produces: a
``system`` role embedded in ``messages`` (often as the LAST element, after a
tool_result) is sent as user text, because open-model chat templates such as
DeepSeek's answer a trailing system message with an immediate EOS; and text
blocks next to a ``tool_result`` (the retry nudge, system-reminders) are
forwarded instead of dropped.
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


_SYSTEM_NOTE = "# MCP Server Instructions\n\nUse context7 for library docs."
_RETRY_NUDGE = "[Your previous response had no visible output. Please try again.]"


def _tool_cycle_messages(*tail: dict[str, Any]) -> list[dict[str, Any]]:
    """A user request, an assistant tool_use, its tool_result, and any trailing messages."""
    return [
        {"role": "user", "content": "list files"},
        {
            "role": "assistant",
            "content": [
                {"type": "tool_use", "id": "toolu_01", "name": "ls", "input": {"path": "src"}}
            ],
        },
        {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": "toolu_01", "content": "main.py"}
            ],
        },
        *tail,
    ]


def _roles(result: dict[str, Any]) -> list[str]:
    """Roles of the converted messages, in order."""
    return [message["role"] for message in result["messages"]]


def test_trailing_system_message_after_tool_result_becomes_user_message() -> None:
    """A system message closing the array is sent as a user turn after the tool message."""
    request = _build_request(
        messages=_tool_cycle_messages({"role": "system", "content": _SYSTEM_NOTE})
    )
    result = _convert(request)
    assert _roles(result) == ["user", "assistant", "tool", "user"]
    assert result["messages"][-1] == {"role": "user", "content": _SYSTEM_NOTE}


def test_system_message_after_string_user_message_is_merged_into_it() -> None:
    """A system message right after a plain user message is joined into that message."""
    request = _build_request(
        messages=[
            {"role": "user", "content": "Hello"},
            {"role": "system", "content": _SYSTEM_NOTE},
        ]
    )
    result = _convert(request)
    assert result["messages"] == [{"role": "user", "content": f"Hello\n\n{_SYSTEM_NOTE}"}]


def test_system_message_after_multipart_user_message_is_appended_as_text_part() -> None:
    """With list content, the system text becomes one more text part, after the image."""
    request = _build_request(
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "what is this"},
                    {
                        "type": "image",
                        "source": {"type": "base64", "media_type": "image/png", "data": "QUJD"},
                    },
                ],
            },
            {"role": "system", "content": _SYSTEM_NOTE},
        ]
    )
    result = _convert(request)
    assert _roles(result) == ["user"]
    parts = result["messages"][0]["content"]
    assert [part["type"] for part in parts] == ["text", "image_url", "text"]
    assert parts[-1] == {"type": "text", "text": _SYSTEM_NOTE}


def test_system_message_after_assistant_message_starts_new_user_message() -> None:
    """Without a preceding user message, the system text opens a new user turn."""
    request = _build_request(
        messages=[
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "Hi"},
            {"role": "system", "content": _SYSTEM_NOTE},
        ]
    )
    result = _convert(request)
    assert result["messages"] == [
        {"role": "user", "content": "Hello"},
        {"role": "assistant", "content": "Hi"},
        {"role": "user", "content": _SYSTEM_NOTE},
    ]


def test_top_level_system_field_still_maps_to_system_message() -> None:
    """The top-level system field keeps the system role at index 0."""
    result = _convert(_build_request(system="You are terse."))
    assert result["messages"] == [
        {"role": "system", "content": "You are terse."},
        {"role": "user", "content": "Hello"},
    ]


def test_leading_system_message_in_messages_is_user_even_after_top_level_system() -> None:
    """Only the top-level field is a system message; the in-array one is user text.

    The note cannot open a user turn of its own here: the user message that
    follows would then be a second, adjacent user turn.
    """
    request = _build_request(
        system="You are terse.",
        messages=[
            {"role": "system", "content": _SYSTEM_NOTE},
            {"role": "user", "content": "Hello"},
        ],
    )
    result = _convert(request)
    assert result["messages"] == [
        {"role": "system", "content": "You are terse."},
        {"role": "user", "content": f"{_SYSTEM_NOTE}\n\nHello"},
    ]


def test_blank_system_message_in_messages_is_dropped() -> None:
    """A whitespace-only system message adds nothing."""
    request = _build_request(
        messages=[
            {"role": "user", "content": "Hello"},
            {"role": "system", "content": "   "},
        ]
    )
    assert _convert(request)["messages"] == [{"role": "user", "content": "Hello"}]


def test_tool_result_only_message_yields_tool_message_without_user_turn() -> None:
    """A tool_result-only user message still maps to tool messages alone."""
    result = _convert(_build_request(messages=_tool_cycle_messages()))
    assert _roles(result) == ["user", "assistant", "tool"]
    assert result["messages"][-1] == {
        "role": "tool",
        "tool_call_id": "toolu_01",
        "content": "main.py",
    }


def test_tool_result_message_with_text_forwards_text_as_user_message() -> None:
    """Text next to a tool_result is not dropped: tool messages first, then a user turn."""
    messages = _tool_cycle_messages()
    messages[-1]["content"].append({"type": "text", "text": _RETRY_NUDGE})
    result = _convert(_build_request(messages=messages))
    assert _roles(result) == ["user", "assistant", "tool", "user"]
    assert result["messages"][2]["tool_call_id"] == "toolu_01"
    assert result["messages"][3] == {"role": "user", "content": _RETRY_NUDGE}


def test_tool_result_message_with_several_text_blocks_keeps_their_order() -> None:
    """Several text blocks next to a tool_result become one user message with ordered parts."""
    messages = _tool_cycle_messages()
    messages[-1]["content"].extend(
        [{"type": "text", "text": "first"}, {"type": "text", "text": "second"}]
    )
    result = _convert(_build_request(messages=messages))
    assert _roles(result) == ["user", "assistant", "tool", "user"]
    assert result["messages"][-1] == {
        "role": "user",
        "content": [{"type": "text", "text": "first"}, {"type": "text", "text": "second"}],
    }


def test_trailing_system_after_mixed_tool_result_message_merges_into_forwarded_text() -> None:
    """The forwarded text and a trailing system message end up in one user turn."""
    messages = _tool_cycle_messages({"role": "system", "content": _SYSTEM_NOTE})
    messages[2]["content"].append({"type": "text", "text": _RETRY_NUDGE})
    result = _convert(_build_request(messages=messages))
    assert _roles(result) == ["user", "assistant", "tool", "user"]
    assert result["messages"][-1] == {
        "role": "user",
        "content": f"{_RETRY_NUDGE}\n\n{_SYSTEM_NOTE}",
    }


def test_system_message_before_a_user_message_merges_forward_into_it() -> None:
    """With no top-level system, a leading note joins the user turn that follows.

    Emitting it as its own turn would leave two adjacent user messages,
    which chat templates with strict role alternation answer with 400.
    """
    request = _build_request(
        messages=[
            {"role": "system", "content": _SYSTEM_NOTE},
            {"role": "user", "content": "Hello"},
        ]
    )
    result = _convert(request)
    assert result["messages"] == [
        {"role": "user", "content": f"{_SYSTEM_NOTE}\n\nHello"}
    ]


def test_two_adjacent_system_messages_merge_forward_into_one_user_turn() -> None:
    """A run of system messages collapses into the single user turn that follows."""
    request = _build_request(
        messages=[
            {"role": "system", "content": _SYSTEM_NOTE},
            {"role": "system", "content": _RETRY_NUDGE},
            {"role": "user", "content": "Hello"},
        ]
    )
    result = _convert(request)
    assert result["messages"] == [
        {"role": "user", "content": f"{_SYSTEM_NOTE}\n\n{_RETRY_NUDGE}\n\nHello"}
    ]


def test_system_message_between_assistant_and_user_merges_into_the_next_user_turn() -> None:
    """The assistant turn stays intact and the note rides on the following user turn."""
    request = _build_request(
        messages=[
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "Hi"},
            {"role": "system", "content": _SYSTEM_NOTE},
            {"role": "user", "content": "carry on"},
        ]
    )
    result = _convert(request)
    assert result["messages"] == [
        {"role": "user", "content": "Hello"},
        {"role": "assistant", "content": "Hi"},
        {"role": "user", "content": f"{_SYSTEM_NOTE}\n\ncarry on"},
    ]


def test_system_message_after_a_tool_message_merges_into_the_next_user_turn() -> None:
    """A tool message is not a user turn, so the note merges forward, not backward."""
    messages = _tool_cycle_messages(
        {"role": "system", "content": _SYSTEM_NOTE},
        {"role": "user", "content": "carry on"},
    )
    result = _convert(_build_request(messages=messages))
    assert _roles(result) == ["user", "assistant", "tool", "user"]
    assert result["messages"][-1] == {
        "role": "user",
        "content": f"{_SYSTEM_NOTE}\n\ncarry on",
    }


def test_system_message_before_a_multipart_user_message_becomes_its_first_text_part() -> None:
    """With list content the note is inserted ahead of the existing parts."""
    request = _build_request(
        messages=[
            {"role": "system", "content": _SYSTEM_NOTE},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "what is this"},
                    {
                        "type": "image",
                        "source": {"type": "base64", "media_type": "image/png", "data": "QUJD"},
                    },
                ],
            },
        ]
    )
    result = _convert(request)
    assert _roles(result) == ["user"]
    parts = result["messages"][0]["content"]
    assert [part["type"] for part in parts] == ["text", "text", "image_url"]
    assert parts[0] == {"type": "text", "text": _SYSTEM_NOTE}


def test_blank_system_message_before_a_user_message_is_dropped() -> None:
    """A whitespace-only system message contributes nothing to the next user turn."""
    request = _build_request(
        messages=[
            {"role": "system", "content": "   "},
            {"role": "user", "content": "Hello"},
        ]
    )
    assert _convert(request)["messages"] == [{"role": "user", "content": "Hello"}]


def test_tool_result_message_with_blank_text_yields_no_user_turn() -> None:
    """A whitespace-only remainder next to a tool_result is not worth a user turn."""
    messages = _tool_cycle_messages()
    messages[-1]["content"].append({"type": "text", "text": "   \n  "})
    result = _convert(_build_request(messages=messages))
    assert _roles(result) == ["user", "assistant", "tool"]


def test_tool_result_message_with_blank_text_and_image_still_forwards_the_image() -> None:
    """An image is content in its own right, whatever the text blocks look like."""
    messages = _tool_cycle_messages()
    messages[-1]["content"].extend(
        [
            {"type": "text", "text": "  "},
            {
                "type": "image",
                "source": {"type": "base64", "media_type": "image/png", "data": "QUJD"},
            },
        ]
    )
    result = _convert(_build_request(messages=messages))
    assert _roles(result) == ["user", "assistant", "tool", "user"]
    assert [part["type"] for part in result["messages"][-1]["content"]] == [
        "text",
        "image_url",
    ]
