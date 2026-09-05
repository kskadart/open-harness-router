"""Matching a model name against routing rules."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    # Import guard, not a style choice: ``routing.schema`` calls
    # ``match_model`` from its own validators, and a runtime import here
    # would close the cycle. ``MatchRule`` is used only as an annotation,
    # deferred by ``from __future__ import annotations``.
    from routing.schema import MatchRule


def match_model(rule: MatchRule, model: str) -> bool:
    """Check whether the model name matches the rule.

    Args:
        rule: the matching rule (type and value).
        model: model name from the request body.

    Returns:
        True if the model satisfies the rule.
    """
    match rule.type:
        case "exact":
            return model == rule.value
        case "prefix":
            return model.startswith(rule.value)
        case "contains":
            return rule.value in model
        case "regex":
            return re.search(rule.value, model) is not None
