"""Claude's tool-visibility rule for deferred tools, shared by both provider types.

Claude Code's MCP tool search marks most MCP tools ``defer_loading``: the
Claude API hides such a tool from the model until the conversation carries a
``tool_addition`` block referencing it by name (beta
``mid-conversation-tool-changes-2026-07-01``), and the
``DeferredToolPlaceholder`` entry -- deferred and never referenced -- only
keeps that machinery active. An upstream without that machinery (an
OpenAI-format one, or an Anthropic-compatible vendor that rejects the
block) needs the router to resolve visibility itself. The rule lives here,
once: ``resolve_visible`` over whatever representation the caller holds
(pydantic models in the translated provider, raw dicts in passthrough), and
``fold_tool_additions`` for the passthrough request body.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from const import DEFERRED_TOOL_PLACEHOLDER

_TOOL_ADDITION = "tool_addition"
_TOOL_DEFINITION = "tool_definition"
_DEFER_LOADING = "defer_loading"


def addition_tool(tool: Mapping[str, Any]) -> Mapping[str, Any]:
    """Unwrap the ``tool`` of a ``tool_addition`` block to the tool itself.

    Claude Code sends either a reference, ``{"type": "tool_reference",
    "name": ...}``, or a full definition wrapped as ``{"type":
    "tool_definition", "definition": {"name": ..., "input_schema": ...}}``.

    Args:
        tool: the block's ``tool`` field as sent.

    Returns:
        The inner definition for a ``tool_definition`` wrapper, else ``tool``.
    """
    inner = tool.get("definition")
    if tool.get("type") == _TOOL_DEFINITION and isinstance(inner, Mapping):
        return inner
    return tool


@dataclass(frozen=True)
class Visibility[T]:
    """Outcome of ``resolve_visible``.

    ``visible`` keeps the request's order, with definitions carried by
    blocks for tools the request does not define appended; ``invalid`` names
    the blocks whose definition did not validate, ``unresolved`` the
    referenced names the request does not define and no block defines.
    """

    visible: list[T]
    invalid: list[str]
    unresolved: list[str]


def resolve_visible[T](
    tools: Sequence[T],
    addition_tools: Iterable[Mapping[str, Any]],
    *,
    name_of: Callable[[T], str],
    deferred_of: Callable[[T], bool],
    from_definition: Callable[[Mapping[str, Any]], T | None],
) -> Visibility[T]:
    """Apply the visibility rule: what the model could see on the Claude API.

    A non-deferred tool is always visible, a deferred one once a block
    references it by name, the placeholder never (by name, not only by its
    flag). A block that carries a full definition rather than a reference
    counts as a reference to its name, and its definition is used only for a
    tool the request does not define.

    Args:
        tools: the request's tools, in the caller's representation.
        addition_tools: the ``tool`` field of every ``tool_addition`` block
            in the conversation.
        name_of: the tool's name.
        deferred_of: whether the tool is marked ``defer_loading``.
        from_definition: builds a tool from a block's definition, or returns
            ``None`` when it is invalid.

    Returns:
        The visible tools and the problems found, for the caller to log.
    """
    referenced: set[str] = set()
    defined: dict[str, T] = {}
    invalid: list[str] = []
    for raw in addition_tools:
        tool = addition_tool(raw)
        name = tool.get("name")
        if not isinstance(name, str):
            continue
        referenced.add(name)
        if "input_schema" not in tool:
            continue
        built = from_definition(tool)
        if built is None:
            invalid.append(name)
        else:
            defined[name] = built

    known = {name_of(tool) for tool in tools}
    visible = [
        tool
        for tool in tools
        if name_of(tool) != DEFERRED_TOOL_PLACEHOLDER
        and (not deferred_of(tool) or name_of(tool) in referenced)
    ]
    visible.extend(
        tool
        for name, tool in defined.items()
        if name not in known and name != DEFERRED_TOOL_PLACEHOLDER
    )
    unresolved = sorted(referenced - known - set(defined))
    return Visibility(visible=visible, invalid=invalid, unresolved=unresolved)


@dataclass(frozen=True)
class FoldResult:
    """A request body rewritten by ``fold_tool_additions``."""

    body: bytes
    blocks_removed: int
    tools_added: int
    tools_hidden: int
    invalid: list[str]
    unresolved: list[str]


def _raw_tool(definition: Mapping[str, Any]) -> dict[str, Any] | None:
    """Copy a block's definition as a raw tool, or ``None`` when it is not one."""
    if not isinstance(definition.get("name"), str) or not isinstance(
        definition.get("input_schema"), dict
    ):
        return None
    return dict(definition)


def fold_tool_additions(raw_body: bytes) -> FoldResult | None:
    """Resolve tool visibility in a Messages request body and drop the blocks.

    For an upstream that rejects ``tool_addition`` blocks (Kimi answers 400
    "Invalid request Error"): every block is removed from the messages (a
    message left without content is dropped), and ``tools`` becomes what
    ``resolve_visible`` keeps -- raw dicts with every field but
    ``defer_loading`` intact, since the router has resolved visibility.

    Args:
        raw_body: the request body as received.

    Returns:
        The rewritten body, or ``None`` when the body is not a JSON object or
        has neither a ``tool_addition`` block nor a ``defer_loading`` tool --
        the caller then forwards ``raw_body`` untouched.
    """
    try:
        payload = json.loads(raw_body)
    except ValueError:
        return None
    if not isinstance(payload, dict):
        return None
    messages = payload.get("messages")
    messages = messages if isinstance(messages, list) else []
    raw_tools = payload.get("tools")
    tools = [t for t in raw_tools if isinstance(t, dict)] if isinstance(raw_tools, list) else None

    additions: list[Mapping[str, Any]] = []
    kept_messages: list[Any] = []
    blocks_removed = 0
    for message in messages:
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, list):
            kept_messages.append(message)
            continue
        kept = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == _TOOL_ADDITION:
                blocks_removed += 1
                tool = block.get("tool")
                if isinstance(tool, dict):
                    additions.append(tool)
            else:
                kept.append(block)
        if kept or len(kept) == len(content):
            kept_messages.append({**message, "content": kept})
    has_deferred = tools is not None and any(t.get(_DEFER_LOADING) for t in tools)
    if not blocks_removed and not has_deferred:
        return None

    tools_added = tools_hidden = 0
    invalid: list[str] = []
    unresolved: list[str] = []
    if blocks_removed:
        payload["messages"] = kept_messages
    if tools is not None:
        outcome = resolve_visible(
            tools,
            additions,
            name_of=lambda t: str(t.get("name")),
            deferred_of=lambda t: bool(t.get(_DEFER_LOADING)),
            from_definition=_raw_tool,
        )
        invalid, unresolved = outcome.invalid, outcome.unresolved
        tools_added = sum(1 for t in outcome.visible if not any(t is o for o in tools))
        tools_hidden = len(tools) - (len(outcome.visible) - tools_added)
        payload["tools"] = [
            {k: v for k, v in t.items() if k != _DEFER_LOADING} for t in outcome.visible
        ]
    return FoldResult(
        body=json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode(),
        blocks_removed=blocks_removed,
        tools_added=tools_added,
        tools_hidden=tools_hidden,
        invalid=invalid,
        unresolved=unresolved,
    )
