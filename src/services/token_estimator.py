"""Heuristic input-token estimate for OpenAI-compatible upstreams.

Most OpenAI-compatible upstreams have no native count_tokens (the corporate
gateway answers 404 on ``/tokenize``), so the router estimates over the
CONVERTED wire payload -- the same dict that goes to /v1/chat/completions or
/v1/responses -- and uses one heuristic for both ``count_tokens`` and the
pre-flight context-window guard in ``providers.openai_translate``. For the
passthrough provider (Anthropic) no estimate is needed: the request is
proxied to the native ``/v1/messages/count_tokens``.

A character heuristic instead of a tokenizer: the fleet mixes tokenizers
(MiniMax, DeepSeek, GLM, GPT), none of which ships with the router, and the
guard only needs a safe upper bound. The divisors were calibrated on
2026-09-04 through the live router against ``usage.input_tokens`` of
MiniMax-M3 and DeepSeek-V4-Flash on three ~6000-character bodies (English
prose; Python code plus six tool definitions; Russian prose): the estimate
was above the measured count on all six samples, estimate/measured ratios
1.10-1.33.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass

# Calibration of 2026-09-04 (module docstring): 3.5/2.0 kept every sample
# over-estimated, so they were not lowered further.
_ASCII_CHARS_PER_TOKEN = 3.5
_NON_ASCII_CHARS_PER_TOKEN = 2.0
# Framing per message or input item (role, separators) and per tool
# definition (the upstream renders the schema into its tool prompt).
_MESSAGE_OVERHEAD_TOKENS = 4
_TOOL_OVERHEAD_TOKENS = 8
# Flat cost per image. The base64 data URL is excluded from the character
# count: its length says nothing about how the upstream tokenizes a picture
# and would dwarf the rest of the prompt.
_IMAGE_TOKENS = 1600
_IMAGE_PART_TYPES: frozenset[str] = frozenset({"image_url", "input_image"})
_TOOL_DEFINITION_KEYS: tuple[str, ...] = ("name", "description", "parameters")


@dataclass
class _Tally:
    """Running character and overhead counts for one request."""

    ascii_chars: int = 0
    non_ascii_chars: int = 0
    messages: int = 0
    tools: int = 0
    images: int = 0

    def add_text(self, text: object) -> None:
        """Count a string's ASCII and non-ASCII characters; ignore non-strings."""
        if not isinstance(text, str):
            return
        ascii_chars = len(text.encode("ascii", "ignore"))
        self.ascii_chars += ascii_chars
        self.non_ascii_chars += len(text) - ascii_chars

    def add_json(self, value: object) -> None:
        """Count a value in its compact JSON form (tool schemas, arguments)."""
        self.add_text(json.dumps(value, separators=(",", ":"), ensure_ascii=False))

    def total(self) -> int:
        """Token estimate: character terms rounded up plus the fixed overheads."""
        return (
            math.ceil(self.ascii_chars / _ASCII_CHARS_PER_TOKEN)
            + math.ceil(self.non_ascii_chars / _NON_ASCII_CHARS_PER_TOKEN)
            + self.messages * _MESSAGE_OVERHEAD_TOKENS
            + self.tools * _TOOL_OVERHEAD_TOKENS
            + self.images * _IMAGE_TOKENS
        )


def _add_content(tally: _Tally, content: object) -> None:
    """Count a message's ``content``: a string or a list of typed parts."""
    if isinstance(content, str):
        tally.add_text(content)
        return
    if not isinstance(content, list):
        return
    for part in content:
        if not isinstance(part, dict):
            continue
        if part.get("type") in _IMAGE_PART_TYPES:
            tally.images += 1
        else:
            tally.add_text(part.get("text"))


def _add_function_call(tally: _Tally, call: object) -> None:
    """Count a tool call's name and serialized arguments."""
    if not isinstance(call, dict):
        return
    tally.add_text(call.get("name"))
    tally.add_text(call.get("arguments"))


def _add_chat_messages(tally: _Tally, messages: object) -> None:
    """Count Chat Completions ``messages`` (system/user/assistant/tool)."""
    if not isinstance(messages, list):
        return
    for message in messages:
        if not isinstance(message, dict):
            continue
        tally.messages += 1
        _add_content(tally, message.get("content"))
        tool_calls = message.get("tool_calls")
        if isinstance(tool_calls, list):
            for tool_call in tool_calls:
                if isinstance(tool_call, dict):
                    _add_function_call(tally, tool_call.get("function"))


def _add_responses_input(tally: _Tally, input_items: object) -> None:
    """Count Responses API ``input`` items, skipping cached reasoning blobs."""
    if not isinstance(input_items, list):
        return
    for item in input_items:
        # Reasoning items are opaque encrypted_content restored from the
        # cache; the upstream charges them as its own state, not as prompt.
        if not isinstance(item, dict) or item.get("type") == "reasoning":
            continue
        tally.messages += 1
        item_type = item.get("type")
        if item_type == "function_call":
            _add_function_call(tally, item)
        elif item_type == "function_call_output":
            tally.add_text(item.get("output"))
        else:
            _add_content(tally, item.get("content"))


def _add_tools(tally: _Tally, tools: object) -> None:
    """Count tool definitions in either wire shape (nested ``function`` or flat)."""
    if not isinstance(tools, list):
        return
    for tool in tools:
        if not isinstance(tool, dict):
            continue
        tally.tools += 1
        function = tool.get("function")
        definition = function if isinstance(function, dict) else tool
        tally.add_json({key: definition.get(key) for key in _TOOL_DEFINITION_KEYS})


def estimate_openai_request_tokens(openai_request: dict[str, object]) -> int:
    """Estimate the input tokens of a converted OpenAI request.

    Handles both wire shapes produced by ``conversion.request_converter``:
    Chat Completions (``messages`` with string or part-list content,
    ``tool_calls[].function.arguments``, role ``tool`` results, nested
    ``tools[].function``) and Responses (``instructions``, ``input`` items
    including ``function_call``/``function_call_output``, flat ``tools``).
    Every text string is counted; tool definitions and arguments in compact
    JSON; images at a fixed cost with their data URL excluded; cached
    reasoning items skipped. Terms are rounded up.

    Args:
        openai_request: the request dict as it will be sent upstream.

    Returns:
        The estimated input token count (an upper bound by calibration).
    """
    tally = _Tally()
    instructions = openai_request.get("instructions")
    if isinstance(instructions, str):
        tally.messages += 1
        tally.add_text(instructions)
    _add_chat_messages(tally, openai_request.get("messages"))
    _add_responses_input(tally, openai_request.get("input"))
    _add_tools(tally, openai_request.get("tools"))
    return tally.total()
