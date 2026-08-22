"""Unit tests for truncating the ``tools`` array for openai-translate providers.

Verify that ``cap_tools`` preserves Claude Code's built-in tools, truncates
the MCP tail to the limit, and logs the dropped names.
"""

from __future__ import annotations

import logging

import pytest

from const import CLAUDE_BUILTIN_TOOL_NAMES
from models.claude import ClaudeTool
from providers.openai_translate import cap_tools


def _tool(name: str) -> ClaudeTool:
    """Create a minimal ClaudeTool with the given name."""
    return ClaudeTool(name=name, input_schema={"type": "object", "properties": {}})


def _tools(names: list[str]) -> list[ClaudeTool]:
    """Create a list of ClaudeTool from a list of names."""
    return [_tool(n) for n in names]


def test_cap_tools_no_limit_returns_tools_unchanged() -> None:
    """tools_max=0 -- no truncation is applied (144 tools remain)."""
    tools = _tools(["Bash"] + [f"mcp_tool_{i}" for i in range(143)])
    result = cap_tools(tools, tools_max=0, provider_name="openai")
    assert result is tools
    assert len(result) == 144


def test_cap_tools_none_tools_returns_none() -> None:
    """tools=None -- returns None without errors."""
    result = cap_tools(None, tools_max=128, provider_name="openai")
    assert result is None


def test_cap_tools_under_limit_returns_unchanged() -> None:
    """50 tools with tools_max=128 -- no truncation."""
    tools = _tools(["Bash", "Read", "Write"] + [f"mcp_{i}" for i in range(47)])
    result = cap_tools(tools, tools_max=128, provider_name="openai")
    assert result is tools
    assert len(result) == 50


def test_cap_tools_caps_mcp_tail_preserving_all_builtins() -> None:
    """144 tools (builtins + MCP) with tools_max=128 -> 128, builtins preserved."""
    builtin_names = ["Bash", "Read", "Write", "Edit", "Grep", "Glob"]
    mcp_names = [f"mcp_tool_{i:03d}" for i in range(138)]
    tools = _tools(builtin_names + mcp_names)
    assert len(tools) == 144

    result = cap_tools(tools, tools_max=128, provider_name="openai")

    assert len(result) == 128
    # All builtins are preserved.
    result_names = [t.name for t in result]
    for name in builtin_names:
        assert name in result_names
    # MCP is filled up to 128, the tail is dropped.
    kept_mcp = [n for n in result_names if n not in CLAUDE_BUILTIN_TOOL_NAMES]
    assert len(kept_mcp) == 128 - len(builtin_names)
    # MCP order is preserved (first N in order).
    assert kept_mcp == mcp_names[: len(kept_mcp)]


def test_cap_tools_only_mcp_capped_to_limit() -> None:
    """200 MCP tools without builtins with tools_max=128 -> first 128 in order."""
    mcp_names = [f"mcp_only_{i:03d}" for i in range(200)]
    tools = _tools(mcp_names)

    result = cap_tools(tools, tools_max=128, provider_name="openai")

    assert len(result) == 128
    result_names = [t.name for t in result]
    assert result_names == mcp_names[:128]


def test_cap_tools_logs_warning_with_dropped_count(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """When truncating, a warning with the dropped tool count is logged."""
    tools = _tools(["Bash"] + [f"mcp_{i}" for i in range(200)])
    caplog.set_level(logging.WARNING, logger="providers.openai_translate")
    cap_tools(tools, tools_max=128, provider_name="openai")

    # structlog writes through stdlib-logging, caplog intercepts the message.
    assert any("tools array capped" in r.message for r in caplog.records)


def test_cap_tools_builtins_exceed_limit_kept_all() -> None:
    """If builtins > tools_max (theoretically impossible) -- all builtins remain anyway."""
    builtin_sample = list(CLAUDE_BUILTIN_TOOL_NAMES)[:5]
    tools = _tools(builtin_sample + ["mcp_extra"])
    # tools_max is smaller than the builtin count -- all builtins are preserved.
    result = cap_tools(tools, tools_max=3, provider_name="openai")
    assert len(result) == 5  # all builtins, MCP dropped
    result_names = {t.name for t in result}
    assert result_names == set(builtin_sample)
