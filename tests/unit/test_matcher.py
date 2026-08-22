"""Unit tests for matching a model name against routing rules."""

from __future__ import annotations

import pytest

from routing.matcher import match_model
from routing.schema import MatchRule


def test_matcher_exact_matches_only_full_equality() -> None:
    rule = MatchRule(type="exact", value="claude-opus-4-8")
    assert match_model(rule, "claude-opus-4-8") is True
    assert match_model(rule, "claude-opus-4-8-mini") is False
    assert match_model(rule, "claude-opus") is False


def test_matcher_prefix_matches_only_at_start() -> None:
    rule = MatchRule(type="prefix", value="claude-")
    assert match_model(rule, "claude-opus-4-8") is True
    assert match_model(rule, "claude-") is True
    assert match_model(rule, "my-claude-model") is False
    assert match_model(rule, "") is False


def test_matcher_contains_matches_anywhere_in_string() -> None:
    rule = MatchRule(type="contains", value="GLM")
    assert match_model(rule, "zai-org/GLM-5.2-FP8") is True
    assert match_model(rule, "GLM") is True
    assert match_model(rule, "prefixGLMsuffix") is True
    assert match_model(rule, "glm-lowercase") is False


def test_matcher_regex_matches_by_search_semantics() -> None:
    rule = MatchRule(type="regex", value=r"^(opus|sonnet|haiku|fable)")
    assert match_model(rule, "opus-4-8") is True
    assert match_model(rule, "sonnet-3-5") is True
    assert match_model(rule, "fable-1") is True
    assert match_model(rule, "claude-opus") is False


@pytest.mark.parametrize(
    ("match_type", "value", "model", "expected"),
    [
        ("exact", "gpt-4o", "gpt-4o", True),
        ("exact", "gpt-4o", "gpt-4o-mini", False),
        ("prefix", "gpt-", "gpt-4o", True),
        ("prefix", "gpt-", "-gpt-", False),
        ("contains", "-mini", "gpt-4o-mini", True),
        ("contains", "-mini", "gpt-4o", False),
        ("regex", r"gpt-\d+", "gpt-4o", True),
        ("regex", r"gpt-\d+", "claude-opus", False),
    ],
)
def test_matcher_parametrized_matrix(
    match_type: str, value: str, model: str, expected: bool
) -> None:
    rule = MatchRule(type=match_type, value=value)  # type: ignore[arg-type]
    assert match_model(rule, model) is expected
