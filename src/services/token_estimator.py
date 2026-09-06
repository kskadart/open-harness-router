"""Heuristic input-token estimate for OpenAI-compatible upstreams.

Most have no native count_tokens, so the router estimates over the CONVERTED
wire payload -- the dict sent to /v1/chat/completions or /v1/responses -- for
both ``count_tokens`` and the context-window guard in
``providers.openai_translate``; passthrough proxies the native endpoint.

Divisors calibrated 2026-09-04 against live ``usage.input_tokens``;
over-counted on all samples (ratio 1.10-1.33). Under-counting is the harmful
direction: it lets an oversized request reach the upstream.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass

# The non-ASCII divisor was measured on Cyrillic and applies to every script
# except the CJK ranges below, which are denser by a factor of two.
_ASCII_CHARS_PER_TOKEN = 3.5
_NON_ASCII_CHARS_PER_TOKEN = 2.0
# CJK characters cost about one token each on the fleet's tokenizers
# (o200k-family, GLM, MiniMax): one Han character is a frequent enough unit
# to own a token, and rarer ones fall back to their UTF-8 bytes and cost
# more. Counting them at the Cyrillic rate halved the estimate of a Chinese
# or Japanese prompt.
_CJK_CHARS_PER_TOKEN = 1.0
# Ranges counted at the CJK rate: CJK symbols and punctuation, Hiragana,
# Katakana, Han (Extension A, Unified, Compatibility), Hangul (Jamo and
# Syllables), the fullwidth/halfwidth forms CJK input methods produce, and
# Han Extension B+ on the supplementary plane.
_CJK_PATTERN = re.compile(
    "[\u3000-\u303f\u3040-\u309f\u30a0-\u30ff\u1100-\u11ff"
    "\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\uac00-\ud7af\uff00-\uffef"
    "\U00020000-\U0002fa1f]"
)
# Characters of ``encrypted_content`` per token of the reasoning it stands for:
# base64 over an AEAD ciphertext; 2.0 sits below every plausible ratio, so the
# estimate errs high.
_REASONING_CHARS_PER_TOKEN = 2.0
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
    cjk_chars: int = 0
    reasoning_chars: int = 0
    messages: int = 0
    tools: int = 0
    images: int = 0

    def add_text(self, text: object) -> None:
        """Count a string's ASCII, CJK and other non-ASCII characters.

        Non-strings are ignored, so a caller may hand over a field that the
        wire shape leaves absent or null.
        """
        if not isinstance(text, str):
            return
        ascii_chars = len(text.encode("ascii", "ignore"))
        self.ascii_chars += ascii_chars
        non_ascii_chars = len(text) - ascii_chars
        if not non_ascii_chars:
            return
        # Only the non-ASCII remainder is scanned, and through the regex
        # engine: a per-character Python loop over a 100K-character prompt
        # would cost more than the whole estimate is worth.
        cjk_chars = _CJK_PATTERN.subn("", text)[1]
        self.cjk_chars += cjk_chars
        self.non_ascii_chars += non_ascii_chars - cjk_chars

    def add_json(self, value: object) -> None:
        """Count a value in its compact JSON form (tool schemas, arguments)."""
        self.add_text(json.dumps(value, separators=(",", ":"), ensure_ascii=False))

    def add_reasoning(self, encrypted_content: object) -> None:
        """Count an encrypted reasoning blob through its length; ignore non-strings."""
        if isinstance(encrypted_content, str):
            self.reasoning_chars += len(encrypted_content)

    def total(self) -> int:
        """Token estimate: character terms rounded up plus the fixed overheads."""
        return (
            math.ceil(self.ascii_chars / _ASCII_CHARS_PER_TOKEN)
            + math.ceil(self.non_ascii_chars / _NON_ASCII_CHARS_PER_TOKEN)
            + math.ceil(self.cjk_chars / _CJK_CHARS_PER_TOKEN)
            + math.ceil(self.reasoning_chars / _REASONING_CHARS_PER_TOKEN)
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
    """Count Responses API ``input`` items, reasoning blobs included."""
    if not isinstance(input_items, list):
        return
    for input_item in input_items:
        if not isinstance(input_item, dict):
            continue
        tally.messages += 1
        input_type = input_item.get("type")
        if input_type == "reasoning":
            # A reasoning item restored from the cache is opaque, but the
            # upstream decrypts it into the same window as the prompt, and
            # a long agentic turn puts many of them in ``input``. Only
            # encrypted_content carries weight: the router asks for no
            # reasoning summary, so ``summary`` arrives empty.
            tally.add_reasoning(input_item.get("encrypted_content"))
        elif input_type == "function_call":
            _add_function_call(tally, input_item)
        elif input_type == "function_call_output":
            tally.add_text(input_item.get("output"))
        else:
            _add_content(tally, input_item.get("content"))


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
    Every text string is counted, CJK characters at their own denser rate;
    tool definitions and arguments in compact JSON; images at a fixed cost
    with their data URL excluded; a restored reasoning item through the
    length of its ``encrypted_content``. Terms are rounded up.

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
