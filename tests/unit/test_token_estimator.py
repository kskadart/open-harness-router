"""Unit tests for the local token estimator used by fleet providers."""

from __future__ import annotations

from models.claude import (
    ClaudeContentBlockText,
    ClaudeMessage,
    ClaudeSystemContent,
    ClaudeTokenCountRequest,
)
from services.token_estimator import estimate_from_claude_request


def _empty_request() -> ClaudeTokenCountRequest:
    return ClaudeTokenCountRequest(model="glm", messages=[])


def test_estimate_returns_min_one_for_empty_request() -> None:
    assert estimate_from_claude_request(_empty_request()) == 1


def test_estimate_uses_str_system_prompt() -> None:
    # 40 characters -> 40 // 4 = 10 tokens
    req = ClaudeTokenCountRequest(
        model="glm",
        messages=[],
        system="a" * 40,
    )
    assert estimate_from_claude_request(req) == 10


def test_estimate_uses_list_system_prompt() -> None:
    # two blocks of 8 characters = 16 -> 16 // 4 = 4
    req = ClaudeTokenCountRequest(
        model="glm",
        messages=[],
        system=[
            ClaudeSystemContent(type="text", text="a" * 8),
            ClaudeSystemContent(type="text", text="b" * 8),
        ],
    )
    assert estimate_from_claude_request(req) == 4


def test_estimate_counts_string_message_content() -> None:
    req = ClaudeTokenCountRequest(
        model="glm",
        messages=[ClaudeMessage(role="user", content="x" * 20)],
    )
    assert estimate_from_claude_request(req) == 5


def test_estimate_counts_structured_message_content() -> None:
    req = ClaudeTokenCountRequest(
        model="glm",
        messages=[
            ClaudeMessage(
                role="user",
                content=[
                    ClaudeContentBlockText(type="text", text="a" * 8),
                    ClaudeContentBlockText(type="text", text="b" * 4),
                ],
            ),
        ],
    )
    assert estimate_from_claude_request(req) == 3


def test_estimate_sums_system_and_messages() -> None:
    # system 40 + user 20 = 60 -> 15
    req = ClaudeTokenCountRequest(
        model="glm",
        messages=[ClaudeMessage(role="user", content="x" * 20)],
        system="a" * 40,
    )
    assert estimate_from_claude_request(req) == 15
