"""Pydantic models for the Anthropic protocol (Claude Messages API).

Ported from claude-code-proxy (src/models/claude.py). Contains request
models for ``/v1/messages`` and ``/v1/messages/count_tokens``, plus all
content blocks (text, image, tool use, tool result) and tool descriptions.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel


class ClaudeContentBlockText(BaseModel):
    """Text content block."""

    type: Literal["text"]
    text: str


class ClaudeContentBlockImage(BaseModel):
    """Image content block (base64/URL in the ``source`` field)."""

    type: Literal["image"]
    source: dict[str, Any]


class ClaudeContentBlockToolUse(BaseModel):
    """Tool call block issued by the assistant."""

    type: Literal["tool_use"]
    id: str
    name: str
    input: dict[str, Any]


class ClaudeContentBlockToolResult(BaseModel):
    """Tool execution result block, sent by the user."""

    type: Literal["tool_result"]
    tool_use_id: str
    content: str | list[dict[str, Any]] | dict[str, Any]


class ClaudeSystemContent(BaseModel):
    """Element of an Anthropic array-form system prompt."""

    type: Literal["text"]
    text: str


class ClaudeMessage(BaseModel):
    """Message in a Claude conversation.

    The ``system`` role is allowed: Anthropic clients (including Claude
    Code) sometimes send system instructions inside the ``messages`` array.
    """

    role: Literal["user", "assistant", "system"]
    content: (
        str
        | list[
            ClaudeContentBlockText
            | ClaudeContentBlockImage
            | ClaudeContentBlockToolUse
            | ClaudeContentBlockToolResult
        ]
    )


class ClaudeTool(BaseModel):
    """Description of a tool available to the model."""

    name: str
    description: str | None = None
    input_schema: dict[str, Any]


class ClaudeThinkingConfig(BaseModel):
    """Extended thinking configuration (``thinking``).

    The shape was observed empirically from a live Claude Code client:
    ``{"type": "adaptive", "display": "omitted"}``. Values are not narrowed
    to ``Literal``: the public Anthropic API allows other variants
    (``enabled``/``disabled``), and rejecting an unfamiliar value would be a
    regression for third-party clients.
    """

    type: str | None = None
    display: str | None = None


class ClaudeOutputConfig(BaseModel):
    """Anthropic output parameters: reasoning level."""

    effort: Literal["low", "medium", "high", "xhigh", "max"] | None = None


class ClaudeMessagesRequest(BaseModel):
    """Anthropic Messages API ``POST /v1/messages`` request."""

    model: str
    max_tokens: int
    messages: list[ClaudeMessage]
    system: str | list[ClaudeSystemContent] | None = None
    stop_sequences: list[str] | None = None
    stream: bool | None = False
    temperature: float | None = 1.0
    top_p: float | None = None
    top_k: int | None = None
    metadata: dict[str, Any] | None = None
    tools: list[ClaudeTool] | None = None
    tool_choice: dict[str, Any] | None = None
    thinking: ClaudeThinkingConfig | None = None
    output_config: ClaudeOutputConfig | None = None


class ClaudeTokenCountRequest(BaseModel):
    """Anthropic Messages API ``POST /v1/messages/count_tokens`` request."""

    model: str
    messages: list[ClaudeMessage]
    system: str | list[ClaudeSystemContent] | None = None
    tools: list[ClaudeTool] | None = None
    thinking: ClaudeThinkingConfig | None = None
    tool_choice: dict[str, Any] | None = None
