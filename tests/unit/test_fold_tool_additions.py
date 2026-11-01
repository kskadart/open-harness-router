"""``fold_tool_additions``: resolving deferred-tool visibility for an own-key passthrough.

Kimi answers 400 "Invalid request Error" to the ``tool_addition`` blocks
Claude Code puts into the conversation from the second turn of an agent loop
(beta ``mid-conversation-tool-changes-2026-07-01``; reproduced 2026-11-01).
With ``fold_tool_additions`` the passthrough provider removes them and sends
the tools the model could see, through the rule it shares with the translated
providers (``services.tool_visibility``).
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest
from pydantic import SecretStr, ValidationError
from pytest_httpx import HTTPXMock

from models.claude import ClaudeMessage, ClaudeTool
from providers.openai_translate import visible_tools
from providers.passthrough import PassthroughProvider
from routing.schema import ProviderCfg, RouteLimits
from services.tool_visibility import fold_tool_additions
from settings import UpstreamSettings

_SCHEMA: dict[str, Any] = {"type": "object", "properties": {}}
_UPSTREAM = "https://kimi.example/coding"


def _tool(name: str, *, deferred: bool = False, **extra: Any) -> dict[str, Any]:
    tool: dict[str, Any] = {"name": name, "description": f"{name} tool", "input_schema": _SCHEMA}
    if deferred:
        tool["defer_loading"] = True
    return {**tool, **extra}


def _reference(name: str) -> dict[str, Any]:
    return {"type": "tool_addition", "tool": {"type": "tool_reference", "name": name}}


def _definition(name: str, **extra: Any) -> dict[str, Any]:
    return {
        "type": "tool_addition",
        "tool": {"type": "tool_definition", "definition": _tool(name, **extra)},
    }


def _body(messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None) -> bytes:
    payload: dict[str, Any] = {"model": "k3.1-pro", "max_tokens": 64, "messages": messages}
    if tools is not None:
        payload["tools"] = tools
    return json.dumps(payload).encode()


def _fold(messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None) -> dict[str, Any]:
    folded = fold_tool_additions(_body(messages, tools))
    assert folded is not None
    result: dict[str, Any] = json.loads(folded.body)
    return result


_USER = {"role": "user", "content": [{"type": "text", "text": "hi"}]}


def test_blocks_are_removed_and_text_blocks_stay() -> None:
    mixed = {"role": "system", "content": [{"type": "text", "text": "note"}, _reference("a")]}
    out = _fold([_USER, mixed], [_tool("a", deferred=True)])

    assert out["messages"][1]["content"] == [{"type": "text", "text": "note"}]


def test_message_left_empty_is_dropped() -> None:
    out = _fold([_USER, {"role": "system", "content": [_reference("a")]}], [_tool("a")])

    assert out["messages"] == [_USER]


def test_deferred_tool_is_hidden_until_referenced() -> None:
    tools = [_tool("Bash"), _tool("a", deferred=True), _tool("b", deferred=True)]
    out = _fold([_USER, {"role": "system", "content": [_reference("b")]}], tools)

    assert [t["name"] for t in out["tools"]] == ["Bash", "b"]


def test_placeholder_is_removed_even_when_referenced() -> None:
    tools = [_tool("DeferredToolPlaceholder", deferred=True), _tool("Bash")]
    out = _fold(
        [_USER, {"role": "system", "content": [_reference("DeferredToolPlaceholder")]}], tools
    )

    assert [t["name"] for t in out["tools"]] == ["Bash"]


def test_placeholder_carried_only_by_a_definition_block_never_goes_upstream() -> None:
    out = _fold(
        [_USER, {"role": "system", "content": [_definition("DeferredToolPlaceholder")]}],
        [_tool("Bash")],
    )

    assert [t["name"] for t in out["tools"]] == ["Bash"]


def test_definition_block_supplies_a_tool_the_request_lacks() -> None:
    out = _fold(
        [_USER, {"role": "system", "content": [_definition("new", defer_loading=True)]}],
        [_tool("Bash")],
    )

    assert [t["name"] for t in out["tools"]] == ["Bash", "new"]
    assert "defer_loading" not in out["tools"][1]


def test_definition_block_does_not_override_a_defined_tool() -> None:
    known = _tool("a", deferred=True, description="the request's own")
    out = _fold([_USER, {"role": "system", "content": [_definition("a")]}], [known])

    assert out["tools"][0]["description"] == "the request's own"
    assert len(out["tools"]) == 1


def test_defer_loading_is_stripped_and_other_fields_kept() -> None:
    tool = _tool("a", deferred=True, cache_control={"type": "ephemeral"}, custom=[1, 2])
    out = _fold([_USER, {"role": "system", "content": [_reference("a")]}], [tool])

    assert out["tools"] == [
        {
            "name": "a",
            "description": "a tool",
            "input_schema": _SCHEMA,
            "cache_control": {"type": "ephemeral"},
            "custom": [1, 2],
        }
    ]


def test_invalid_definition_and_unresolved_reference_are_reported_not_fatal() -> None:
    broken = {
        "type": "tool_addition",
        "tool": {
            "type": "tool_definition",
            "definition": {"name": "broken", "input_schema": "not-a-schema"},
        },
    }
    folded = fold_tool_additions(
        _body([_USER, {"role": "system", "content": [broken, _reference("ghost")]}], [_tool("a")])
    )

    assert folded is not None
    assert folded.invalid == ["broken"]
    assert folded.unresolved == ["broken", "ghost"]
    assert folded.blocks_removed == 2


def test_stats() -> None:
    tools = [_tool("a"), _tool("b", deferred=True), _tool("c", deferred=True)]
    blocks = [_reference("b"), _definition("n")]
    folded = fold_tool_additions(_body([_USER, {"role": "system", "content": blocks}], tools))

    assert folded is not None
    assert (folded.blocks_removed, folded.tools_added, folded.tools_hidden) == (2, 1, 1)


def test_deferred_tool_without_any_block_is_hidden() -> None:
    out = _fold([_USER], [_tool("a"), _tool("b", deferred=True)])

    assert [t["name"] for t in out["tools"]] == ["a"]


def test_blocks_without_tools_key_leave_tools_absent() -> None:
    out = _fold([_USER, {"role": "system", "content": [_reference("a")]}], None)

    assert "tools" not in out


@pytest.mark.parametrize(
    "raw",
    [
        _body([_USER], [_tool("a")]),
        _body([{"role": "user", "content": "plain string"}], None),
        b"not json at all",
        b'["a", "list"]',
        b"",
    ],
)
def test_nothing_to_fold_returns_none(raw: bytes) -> None:
    assert fold_tool_additions(raw) is None


def test_shape_matches_the_translated_providers_rule() -> None:
    """One rule: the pydantic side picks the same tools from the same input."""
    tools = [
        _tool("Bash"),
        _tool("a", deferred=True),
        _tool("b", deferred=True),
        _tool("DeferredToolPlaceholder", deferred=True),
    ]
    messages = [
        _USER,
        {"role": "system", "content": [_reference("b"), _definition("n", defer_loading=True)]},
    ]
    out = _fold(messages, tools)
    models = visible_tools(
        [ClaudeTool.model_validate(t) for t in tools],
        [ClaudeMessage.model_validate(m) for m in messages],
        "gateway",
    )

    assert models is not None
    assert [t["name"] for t in out["tools"]] == [t.name for t in models]


# A real captured request holds a real conversation, so it never enters the
# repository: point TOOL_ADDITION_CAPTURE at one ({"body": {...}}) to run this.
_CAPTURE = Path(os.environ.get("TOOL_ADDITION_CAPTURE", "/nonexistent"))


@pytest.mark.skipif(not _CAPTURE.is_file(), reason="captured Claude Code request not available")
def test_captured_claude_code_request_loses_every_tool_addition() -> None:
    """The request Kimi answered with 400, as Claude Code sent it."""
    body = json.loads(_CAPTURE.read_text())["body"]
    raw = body.encode() if isinstance(body, str) else json.dumps(body).encode()
    folded = fold_tool_additions(raw)

    assert folded is not None
    assert folded.blocks_removed > 0
    out = json.loads(folded.body)
    assert "tool_addition" not in folded.body.decode()
    assert all("defer_loading" not in t for t in out["tools"])
    assert "DeferredToolPlaceholder" not in {t["name"] for t in out["tools"]}


def test_translated_provider_reads_the_tool_definition_shape() -> None:
    """Claude Code nests a definition under ``definition``; the rule unwraps it."""
    messages = [
        ClaudeMessage.model_validate(
            {"role": "system", "content": [_definition("n", defer_loading=True)]}
        )
    ]
    tools = [ClaudeTool.model_validate(_tool("Bash"))]
    visible = visible_tools(tools, messages, "gateway")

    assert visible is not None
    assert [t.name for t in visible] == ["Bash", "n"]


# --- schema -----------------------------------------------------------------


def test_schema_accepts_fold_on_own_key_passthrough() -> None:
    cfg = ProviderCfg(
        type="passthrough",
        base_url=_UPSTREAM,
        forward_client_auth=False,
        api_key_env="KIMI_KEY",
        fold_tool_additions=True,
    )

    assert cfg.fold_tool_additions is True
    assert ProviderCfg(type="passthrough", base_url=_UPSTREAM).fold_tool_additions is False


def test_schema_rejects_fold_on_native_passthrough() -> None:
    with pytest.raises(ValidationError, match="forward_client_auth=false"):
        ProviderCfg(type="passthrough", base_url=_UPSTREAM, fold_tool_additions=True)


def test_schema_rejects_fold_on_openai_translate() -> None:
    with pytest.raises(ValidationError, match="only applies to passthrough"):
        ProviderCfg(
            type="openai-translate",
            base_url=_UPSTREAM,
            api_key_env="KEY",
            max_tokens_limit=1024,
            fold_tool_additions=True,
        )


# --- provider ---------------------------------------------------------------


class _Channel:
    async def is_disconnected(self) -> bool:
        return False


def _provider(*, fold: bool) -> PassthroughProvider:
    cfg = ProviderCfg(
        type="passthrough",
        base_url=_UPSTREAM,
        forward_client_auth=False,
        api_key_env="KIMI_KEY",
        fold_tool_additions=fold,
    )
    return PassthroughProvider("moonshot", cfg, UpstreamSettings(), SecretStr("own"), None)


_FOLDABLE = _body(
    [_USER, {"role": "system", "content": [_reference("a")]}],
    [_tool("a", deferred=True), _tool("b", deferred=True)],
)
_HEADERS = {"anthropic-version": "2023-06-01", "content-type": "application/json"}


@pytest.mark.parametrize(
    ("endpoint", "path"),
    [("handle_messages", "/v1/messages"), ("count_tokens", "/v1/messages/count_tokens")],
)
async def test_upstream_receives_the_folded_body(
    httpx_mock: HTTPXMock, endpoint: str, path: str
) -> None:
    httpx_mock.add_response(url=_UPSTREAM + path, method="POST", json={"id": "msg"})
    provider = _provider(fold=True)
    limits = RouteLimits.resolve(provider.cfg, None)
    try:
        if endpoint == "count_tokens":
            await provider.count_tokens(_FOLDABLE, _HEADERS, None, limits)
        else:
            await provider.handle_messages(_FOLDABLE, _HEADERS, _Channel(), None, limits)
    finally:
        await provider.aclose()

    request = httpx_mock.get_request()
    assert request is not None
    sent = json.loads(request.content)
    assert [t["name"] for t in sent["tools"]] == ["a"]
    assert "defer_loading" not in sent["tools"][0]
    assert sent["messages"] == [_USER]


async def test_upstream_receives_original_bytes_without_the_flag(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(url=_UPSTREAM + "/v1/messages", method="POST", json={"id": "msg"})
    provider = _provider(fold=False)
    try:
        await provider.handle_messages(
            _FOLDABLE, _HEADERS, _Channel(), None, RouteLimits.resolve(provider.cfg, None)
        )
    finally:
        await provider.aclose()

    request = httpx_mock.get_request()
    assert request is not None
    assert request.content == _FOLDABLE


async def test_upstream_receives_original_bytes_when_nothing_to_fold(
    httpx_mock: HTTPXMock,
) -> None:
    plain = b'{ "model":"k3.1-pro",  "messages":[{"role":"user","content":"hi"}] }'
    httpx_mock.add_response(url=_UPSTREAM + "/v1/messages", method="POST", json={"id": "msg"})
    provider = _provider(fold=True)
    try:
        await provider.handle_messages(
            plain, _HEADERS, _Channel(), None, RouteLimits.resolve(provider.cfg, None)
        )
    finally:
        await provider.aclose()

    request = httpx_mock.get_request()
    assert request is not None
    assert request.content == plain
