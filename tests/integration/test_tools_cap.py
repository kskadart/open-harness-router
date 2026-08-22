"""Integration test: truncating the ``tools`` array for the openai-translate provider.

Verifies end-to-end: Claude Code sends 144 tools, the openai provider on the
chat path (``openai_chat``, tools_max=128) truncates to 128 while preserving
the built-in tools. Regression: the ``anthropic`` passthrough provider
forwards tools byte-for-byte without truncation.
"""

from __future__ import annotations

import json

import httpx
from pytest_httpx import HTTPXMock

_OPENAI_CHAT_URL = "https://api.openai.com/v1/chat/completions"
_ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"

# The chat- prefix routes to the provider with api_flavor=chat.
_CHAT_MODEL = "chat-gpt-5.6-sol"

_OPENAI_RESPONSE = {
    "id": "chatcmpl-tools-cap-01",
    "object": "chat.completion",
    "created": 1_720_000_000,
    "model": "gpt-5.6-sol",
    "choices": [
        {
            "index": 0,
            "message": {"role": "assistant", "content": "ok"},
            "finish_reason": "stop",
        }
    ],
    "usage": {"prompt_tokens": 5, "completion_tokens": 1, "total_tokens": 6},
}

_ANTHROPIC_RESPONSE = {
    "id": "msg_tools_cap_01",
    "type": "message",
    "role": "assistant",
    "model": "claude-opus-4-8",
    "content": [{"type": "text", "text": "ok"}],
    "stop_reason": "end_turn",
    "stop_sequence": None,
    "usage": {"input_tokens": 3, "output_tokens": 1},
}

# A handful of built-in names + 138 MCP = 144 tools (as in the real bug).
_BUILTIN_TOOL_NAMES = ["Bash", "Read", "Write", "Edit", "Grep", "Glob"]
_MCP_TOOL_NAMES = [f"mcp_tool_{i:03d}" for i in range(138)]
_ALL_TOOL_NAMES = _BUILTIN_TOOL_NAMES + _MCP_TOOL_NAMES


def _tools_payload(names: list[str]) -> list[dict[str, object]]:
    """Generate a tools array in Anthropic format for a list of names."""
    return [
        {
            "name": n,
            "description": f"Tool {n}",
            "input_schema": {"type": "object", "properties": {}},
        }
        for n in names
    ]


async def test_openai_provider_caps_tools_to_128_preserving_builtins(
    client: httpx.AsyncClient, httpx_mock: HTTPXMock
) -> None:
    """144 tools -> openai_chat truncates to 128, builtins are preserved."""
    httpx_mock.add_response(
        url=_OPENAI_CHAT_URL,
        method="POST",
        json=_OPENAI_RESPONSE,
        status_code=200,
        headers={"content-type": "application/json"},
    )

    payload = {
        "model": _CHAT_MODEL,
        "max_tokens": 128,
        "messages": [{"role": "user", "content": "hi"}],
        "tools": _tools_payload(_ALL_TOOL_NAMES),
    }
    response = await client.post(
        "/v1/messages",
        json=payload,
        headers={"x-api-key": "irrelevant-openai-uses-server-key"},
    )

    assert response.status_code == 200

    outbound = httpx_mock.get_requests(url=_OPENAI_CHAT_URL, method="POST")
    assert len(outbound) == 1
    upstream_body = json.loads(outbound[0].content)
    assert "tools" in upstream_body
    assert len(upstream_body["tools"]) == 128

    upstream_tool_names = {t["function"]["name"] for t in upstream_body["tools"]}
    for name in _BUILTIN_TOOL_NAMES:
        assert name in upstream_tool_names


async def test_passthrough_provider_does_not_cap_tools(
    client: httpx.AsyncClient, httpx_mock: HTTPXMock
) -> None:
    """Passthrough (anthropic) forwards all 144 tools byte-for-byte."""
    httpx_mock.add_response(
        url=_ANTHROPIC_URL,
        method="POST",
        json=_ANTHROPIC_RESPONSE,
        status_code=200,
        headers={"content-type": "application/json"},
    )

    tools = _tools_payload(_ALL_TOOL_NAMES)
    payload = {
        "model": "claude-opus-4-8",
        "max_tokens": 16,
        "messages": [{"role": "user", "content": "hi"}],
        "tools": tools,
    }
    response = await client.post(
        "/v1/messages",
        json=payload,
        headers={"x-api-key": "test-client-key", "anthropic-version": "2023-06-01"},
    )

    assert response.status_code == 200

    upstream = httpx_mock.get_requests(url=_ANTHROPIC_URL, method="POST")
    assert len(upstream) == 1
    upstream_body = json.loads(upstream[0].content)
    # Passthrough: all 144 tools are forwarded without truncation.
    assert len(upstream_body["tools"]) == 144
