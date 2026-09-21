"""Deferred tools and ``tool_addition`` blocks on the translated provider.

Claude Code's MCP tool search sends every MCP tool in ``tools`` with
``defer_loading: true`` plus a ``DeferredToolPlaceholder`` entry, and
announces a tool the model may now see with a ``tool_addition`` block --
``{"type": "tool_addition", "tool": {"type": "tool_reference", "name":
...}}`` -- inside a system-role message (beta
``mid-conversation-tool-changes-2026-07-01``, captured from Claude Code
2.1.278 on 2026-09-21). The schema used to reject the block with a 400 on
the first request of every session, and the translation sent every
deferred schema upstream. Now the block parses, and only the tools the
model could see on the Claude API go upstream.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import pytest
from pydantic import SecretStr
from pytest_httpx import HTTPXMock

from models.claude import ClaudeMessage, ClaudeMessagesRequest, ClaudeTool
from providers.openai_translate import OpenAITranslateProvider, visible_tools
from routing.schema import ProviderCfg, RouteLimits
from settings import UpstreamSettings

_BASE_URL = "https://gateway.example/v1"
_CHAT_URL = _BASE_URL + "/chat/completions"
_MODEL = "zai-org/GLM-5.3-Flash"
_SCHEMA: dict[str, Any] = {"type": "object", "properties": {}}
_PLACEHOLDER = {
    "name": "DeferredToolPlaceholder",
    "description": (
        "Reserved placeholder that keeps deferred tool loading active; never call this tool."
    ),
    "input_schema": _SCHEMA,
    "defer_loading": True,
}
_COMPLETION: dict[str, Any] = {
    "id": "chatcmpl-test",
    "object": "chat.completion",
    "created": 0,
    "model": _MODEL,
    "choices": [
        {"index": 0, "message": {"role": "assistant", "content": "Hi"}, "finish_reason": "stop"}
    ],
    "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
}


def _tool(name: str, *, deferred: bool | None = None) -> ClaudeTool:
    return ClaudeTool(name=name, input_schema=_SCHEMA, defer_loading=deferred)


def _addition(name: str) -> dict[str, Any]:
    """The block as Claude Code sends it: a reference by name."""
    return {"type": "tool_addition", "tool": {"type": "tool_reference", "name": name}}


def _system_message(*blocks: dict[str, Any]) -> ClaudeMessage:
    return ClaudeMessage.model_validate({"role": "system", "content": list(blocks)})


_TOOLS = [
    _tool("Bash"),
    _tool("mcp__docs__batch", deferred=True),
    _tool("mcp__docs__guide", deferred=True),
    ClaudeTool.model_validate(_PLACEHOLDER),
    _tool("Read", deferred=False),
]


def test_schema_accepts_tool_addition_in_system_and_user_messages() -> None:
    """The block parses wherever it appears, cache_control included."""
    request = ClaudeMessagesRequest.model_validate({
        "model": "ag-GLM-5.3-Flash",
        "max_tokens": 128,
        "messages": [
            {"role": "user", "content": [{"type": "text", "text": "hi"}, _addition("a")]},
            {
                "role": "system",
                "content": [
                    {"type": "text", "text": "reminder"},
                    {**_addition("b"), "cache_control": {"type": "ephemeral"}},
                ],
            },
        ],
        "tools": [{**_PLACEHOLDER}],
    })

    reminder = request.messages[1].content
    assert isinstance(reminder, list)
    assert reminder[1].type == "tool_addition"
    assert request.tools is not None
    assert request.tools[0].defer_loading is True


def test_deferred_tools_and_the_placeholder_stay_hidden_without_a_reference() -> None:
    visible = visible_tools(_TOOLS, [_system_message({"type": "text", "text": "x"})], "gateway")

    assert visible is not None
    assert [tool.name for tool in visible] == ["Bash", "Read"]


def test_a_referenced_deferred_tool_becomes_visible_in_request_order() -> None:
    messages = [_system_message(_addition("mcp__docs__guide"))]

    visible = visible_tools(_TOOLS, messages, "gateway")

    assert visible is not None
    assert [tool.name for tool in visible] == ["Bash", "mcp__docs__guide", "Read"]


def test_a_full_definition_in_the_block_is_appended() -> None:
    block = {
        "type": "tool_addition",
        "tool": {"name": "mcp__new__tool", "description": "new", "input_schema": _SCHEMA},
    }

    visible = visible_tools(_TOOLS, [_system_message(block)], "gateway")

    assert visible is not None
    assert [tool.name for tool in visible] == ["Bash", "Read", "mcp__new__tool"]
    assert visible[-1].description == "new"


def test_a_definition_for_a_tool_the_request_defines_counts_as_a_reference() -> None:
    """The request's own definition wins; the tool must not vanish."""
    block = {
        "type": "tool_addition",
        "tool": {"name": "mcp__docs__guide", "description": "from block", "input_schema": _SCHEMA},
    }

    visible = visible_tools(_TOOLS, [_system_message(block)], "gateway")

    assert visible is not None
    assert [tool.name for tool in visible] == ["Bash", "mcp__docs__guide", "Read"]
    assert visible[1].description is None


def test_an_invalid_definition_is_logged_and_skipped(caplog: pytest.LogCaptureFixture) -> None:
    """A malformed block must not fail the request with an internal error."""
    block = {"type": "tool_addition", "tool": {"name": "mcp__bad", "input_schema": "nope"}}

    with caplog.at_level(logging.WARNING):
        visible = visible_tools(_TOOLS, [_system_message(block)], "gateway")

    assert visible is not None
    assert [tool.name for tool in visible] == ["Bash", "Read"]
    assert "tool_addition_invalid" in caplog.text


def test_the_placeholder_is_hidden_even_without_the_flag() -> None:
    tools = [_tool("Bash"), _tool("DeferredToolPlaceholder"), _tool("Read")]

    visible = visible_tools(tools, [], "gateway")

    assert visible is not None
    assert [tool.name for tool in visible] == ["Bash", "Read"]


def test_an_unresolved_reference_is_logged_and_skipped(caplog: pytest.LogCaptureFixture) -> None:
    messages = [_system_message(_addition("mcp__gone__tool"))]

    with caplog.at_level(logging.WARNING):
        visible = visible_tools(_TOOLS, messages, "gateway")
    assert visible is not None
    assert [tool.name for tool in visible] == ["Bash", "Read"]
    assert "tool_addition_unresolved" in caplog.text
    assert "mcp__gone__tool" in caplog.text

    caplog.clear()
    with caplog.at_level(logging.WARNING):
        visible_tools(_TOOLS, messages, "gateway", log_issues=False)
    assert "tool_addition_unresolved" not in caplog.text


def test_no_tools_stays_none() -> None:
    assert visible_tools(None, [_system_message(_addition("x"))], "gateway") is None


def _provider(tools_max: int = 0) -> OpenAITranslateProvider:
    cfg = ProviderCfg(
        type="openai-translate",
        base_url=_BASE_URL,
        api_key_env="GATEWAY_API_KEY",
        max_tokens_limit=8192,
        tools_max=tools_max,
    )
    return OpenAITranslateProvider(
        name="gateway",
        cfg=cfg,
        api_key=SecretStr("test-gateway-key"),
        ca_bundle_path=None,
        upstream=UpstreamSettings(),
    )


class _ConnectedChannel:
    """Client channel that never disconnects."""

    async def is_disconnected(self) -> bool:
        """Report the client as still connected."""
        return False


def _first_request_of_a_session() -> bytes:
    """The shape Claude Code 2.1.278 sends first through the forward-proxy."""
    return json.dumps({
        "model": "ag-GLM-5.3-Flash",
        "max_tokens": 128,
        "messages": [
            {"role": "user", "content": [{"type": "text", "text": "hi"}]},
            {
                "role": "system",
                "content": [
                    {"type": "text", "text": "<system-reminder>tools surfaced</system-reminder>"},
                    _addition("mcp__docs__batch"),
                    {**_addition("mcp__docs__guide"), "cache_control": {"type": "ephemeral"}},
                ],
            },
        ],
        "tools": [
            {"name": "Bash", "input_schema": _SCHEMA},
            {"name": "mcp__docs__batch", "input_schema": _SCHEMA, "defer_loading": True},
            {"name": "mcp__docs__guide", "input_schema": _SCHEMA, "defer_loading": True},
            _PLACEHOLDER,
            {"name": "mcp__docs__update", "input_schema": _SCHEMA, "defer_loading": True},
        ],
    }).encode()


async def test_the_first_request_of_a_session_is_served_with_the_visible_tools(
    httpx_mock: HTTPXMock,
) -> None:
    httpx_mock.add_response(url=_CHAT_URL, method="POST", json=_COMPLETION)
    provider = _provider()
    try:
        result = await provider.handle_messages(
            _first_request_of_a_session(),
            {},
            _ConnectedChannel(),
            _MODEL,
            RouteLimits.resolve(provider.cfg, None),
        )
        counted = await provider.count_tokens(
            _first_request_of_a_session(), {}, _MODEL, RouteLimits.resolve(provider.cfg, None)
        )
    finally:
        await provider.aclose()

    assert result.status_code == 200
    assert counted.status_code == 200
    upstream_request = httpx_mock.get_request()
    assert upstream_request is not None
    sent = json.loads(upstream_request.content)
    assert [tool["function"]["name"] for tool in sent["tools"]] == [
        "Bash", "mcp__docs__batch", "mcp__docs__guide",
    ]
    # The system-role message still reaches the upstream as user text.
    assert sent["messages"][-1]["role"] == "user"
    assert "tools surfaced" in sent["messages"][-1]["content"]


async def test_visibility_is_applied_before_the_tools_cap(httpx_mock: HTTPXMock) -> None:
    """A referenced deferred tool survives a cap that a hidden one would have used up."""
    httpx_mock.add_response(url=_CHAT_URL, method="POST", json=_COMPLETION)
    body = json.dumps({
        "model": "ag-GLM-5.3-Flash",
        "max_tokens": 128,
        "messages": [
            {"role": "user", "content": "hi"},
            {"role": "system", "content": [_addition("mcp__b")]},
        ],
        "tools": [
            {"name": "Bash", "input_schema": _SCHEMA},
            {"name": "mcp__a", "input_schema": _SCHEMA, "defer_loading": True},
            {"name": "mcp__b", "input_schema": _SCHEMA, "defer_loading": True},
        ],
    }).encode()
    provider = _provider(tools_max=2)
    try:
        result = await provider.handle_messages(
            body, {}, _ConnectedChannel(), _MODEL, RouteLimits.resolve(provider.cfg, None)
        )
    finally:
        await provider.aclose()

    assert result.status_code == 200
    upstream_request = httpx_mock.get_request()
    assert upstream_request is not None
    sent = json.loads(upstream_request.content)
    assert [tool["function"]["name"] for tool in sent["tools"]] == ["Bash", "mcp__b"]
