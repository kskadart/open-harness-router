"""Convert Anthropic Messages API requests -> OpenAI (Chat Completions/Responses).

Ported from claude-code-proxy (src/conversion/request_converter.py). The
``convert_claude_to_openai`` function unpacks the top-level ``system`` field,
walks the ``messages`` array (including a system role embedded in the array,
which Claude Code sometimes sends), converts user multimodal blocks,
assistant tool_use blocks and tool results, and assembles ``tools``/
``tool_choice`` and the final OpenAI request.

``convert_claude_to_responses`` builds a request for /v1/responses: a flat
``input`` array instead of ``messages``, system instructions in
``instructions``, a flat ``tools`` shape, and the reasoning level in
``reasoning.effort``.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from const import Constants
from log import get_logger

if TYPE_CHECKING:
    from models.claude import ClaudeMessage, ClaudeMessagesRequest
    from routing.schema import ReasoningEffort
    from services.reasoning_cache import ReasoningCache

logger = get_logger(__name__)


def convert_claude_to_openai(  # noqa: PLR0912, PLR0915
    claude_request: ClaudeMessagesRequest,
    upstream_model: str | None = None,
    *,
    min_tokens_limit: int = 100,
    max_tokens_limit: int = 4096,
) -> dict[str, Any]:
    """Convert an Anthropic Messages request into OpenAI Chat Completions format.

    Args:
        claude_request: parsed Anthropic ``/v1/messages`` request.
        upstream_model: name of the upstream OpenAI model; if ``None``,
            ``claude_request.model`` is used unchanged.
        min_tokens_limit: minimum allowed value for ``max_tokens``.
        max_tokens_limit: maximum allowed value for ``max_tokens``.

    Returns:
        OpenAI Chat Completions request dict.
    """

    openai_model = upstream_model if upstream_model is not None else claude_request.model

    openai_messages: list[dict[str, Any]] = []

    if claude_request.system:
        system_text = ""
        if isinstance(claude_request.system, str):
            system_text = claude_request.system
        elif isinstance(claude_request.system, list):
            text_parts = []
            for block in claude_request.system:
                if hasattr(block, "type") and block.type == Constants.CONTENT_TEXT:
                    text_parts.append(block.text)
                elif (
                    isinstance(block, dict)
                    and block.get("type") == Constants.CONTENT_TEXT
                ):
                    text_parts.append(block.get("text", ""))
            system_text = "\n\n".join(text_parts)

        if system_text.strip():
            openai_messages.append(
                {"role": Constants.ROLE_SYSTEM, "content": system_text.strip()}
            )

    i = 0
    while i < len(claude_request.messages):
        msg = claude_request.messages[i]

        if msg.role == Constants.ROLE_USER:
            openai_message = convert_claude_user_message(msg)
            openai_messages.append(openai_message)
        elif msg.role == Constants.ROLE_SYSTEM:
            # Anthropic clients (including Claude Code) sometimes send a system
            # role inside the messages array, not only in the top-level system
            # field. The message is kept at its current position to avoid
            # losing instructions or breaking the order relative to
            # user/assistant.
            system_text = ""
            if isinstance(msg.content, str):
                system_text = msg.content
            elif isinstance(msg.content, list):
                text_parts = []
                for block in msg.content:
                    if hasattr(block, "type") and block.type == Constants.CONTENT_TEXT:
                        text_parts.append(block.text)
                    elif (
                        isinstance(block, dict)
                        and block.get("type") == Constants.CONTENT_TEXT
                    ):
                        text_parts.append(block.get("text", ""))
                system_text = "\n\n".join(text_parts)

            if system_text.strip():
                openai_messages.append(
                    {"role": Constants.ROLE_SYSTEM, "content": system_text.strip()}
                )
        elif msg.role == Constants.ROLE_ASSISTANT:
            openai_message = convert_claude_assistant_message(msg)
            openai_messages.append(openai_message)

            if i + 1 < len(claude_request.messages):
                next_msg = claude_request.messages[i + 1]
                if (
                    next_msg.role == Constants.ROLE_USER
                    and isinstance(next_msg.content, list)
                    and any(
                        block.type == Constants.CONTENT_TOOL_RESULT
                        for block in next_msg.content
                        if hasattr(block, "type")
                    )
                ):
                    i += 1
                    tool_results = convert_claude_tool_results(next_msg)
                    openai_messages.extend(tool_results)

        i += 1

    openai_request: dict[str, Any] = {
        "model": openai_model,
        "messages": openai_messages,
        "max_tokens": min(
            max(claude_request.max_tokens, min_tokens_limit),
            max_tokens_limit,
        ),
        "temperature": claude_request.temperature,
        "stream": claude_request.stream,
    }
    logger.debug(
        "converted claude request to openai format",
        openai_request=openai_request,
    )
    if claude_request.stop_sequences:
        openai_request["stop"] = claude_request.stop_sequences
    if claude_request.top_p is not None:
        openai_request["top_p"] = claude_request.top_p

    if claude_request.tools:
        openai_tools = []
        for tool in claude_request.tools:
            if tool.name and tool.name.strip():
                openai_tools.append(
                    {
                        "type": Constants.TOOL_FUNCTION,
                        Constants.TOOL_FUNCTION: {
                            "name": tool.name,
                            "description": tool.description or "",
                            "parameters": tool.input_schema,
                        },
                    }
                )
        if openai_tools:
            openai_request["tools"] = openai_tools

    if claude_request.tool_choice:
        choice_type = claude_request.tool_choice.get("type")
        if choice_type == "auto":
            openai_request["tool_choice"] = "auto"
        elif choice_type == "any":
            # Anthropic's "any" forces a tool call; the OpenAI equivalent is
            # "required", not "auto" (auto leaves the call up to the model).
            openai_request["tool_choice"] = "required"
        elif choice_type == "tool" and "name" in claude_request.tool_choice:
            openai_request["tool_choice"] = {
                "type": Constants.TOOL_FUNCTION,
                Constants.TOOL_FUNCTION: {"name": claude_request.tool_choice["name"]},
            }
        else:
            openai_request["tool_choice"] = "auto"

    return openai_request


def convert_claude_user_message(msg: ClaudeMessage) -> dict[str, Any]:
    """Convert a Claude user message into OpenAI format.

    Args:
        msg: message with role ``user`` from the Anthropic request.

    Returns:
        OpenAI message (string ``content`` or a list of multimodal parts).
    """
    if msg.content is None:
        return {"role": Constants.ROLE_USER, "content": ""}

    if isinstance(msg.content, str):
        return {"role": Constants.ROLE_USER, "content": msg.content}

    openai_content: list[dict[str, Any]] = []
    for block in msg.content:
        if block.type == Constants.CONTENT_TEXT:
            openai_content.append({"type": "text", "text": block.text})
        elif block.type == Constants.CONTENT_IMAGE:  # noqa: SIM102
            if (
                isinstance(block.source, dict)
                and block.source.get("type") == "base64"
                and "media_type" in block.source
                and "data" in block.source
            ):
                openai_content.append(
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": (
                                f"data:{block.source['media_type']};"
                                f"base64,{block.source['data']}"
                            )
                        },
                    }
                )

    if len(openai_content) == 1 and openai_content[0]["type"] == "text":
        return {"role": Constants.ROLE_USER, "content": openai_content[0]["text"]}
    return {"role": Constants.ROLE_USER, "content": openai_content}


def convert_claude_assistant_message(msg: ClaudeMessage) -> dict[str, Any]:
    """Convert a Claude assistant message into OpenAI format.

    Args:
        msg: message with role ``assistant`` from the Anthropic request.

    Returns:
        OpenAI message, with a ``tool_calls`` field when applicable.
    """
    text_parts: list[str] = []
    tool_calls: list[dict[str, Any]] = []

    if msg.content is None:
        return {"role": Constants.ROLE_ASSISTANT, "content": None}

    if isinstance(msg.content, str):
        return {"role": Constants.ROLE_ASSISTANT, "content": msg.content}

    for block in msg.content:
        if block.type == Constants.CONTENT_TEXT:
            text_parts.append(block.text)
        elif block.type == Constants.CONTENT_TOOL_USE:
            tool_calls.append(
                {
                    "id": block.id,
                    "type": Constants.TOOL_FUNCTION,
                    Constants.TOOL_FUNCTION: {
                        "name": block.name,
                        "arguments": json.dumps(block.input, ensure_ascii=False),
                    },
                }
            )

    openai_message: dict[str, Any] = {"role": Constants.ROLE_ASSISTANT}

    if text_parts:
        openai_message["content"] = "".join(text_parts)
    else:
        openai_message["content"] = None

    if tool_calls:
        openai_message["tool_calls"] = tool_calls

    return openai_message


def convert_claude_tool_results(msg: ClaudeMessage) -> list[dict[str, Any]]:
    """Convert Anthropic ``tool_result`` blocks into OpenAI role=tool messages.

    Args:
        msg: user message containing tool result blocks.

    Returns:
        List of OpenAI messages with role ``tool``.
    """
    tool_messages: list[dict[str, Any]] = []

    if isinstance(msg.content, list):
        for block in msg.content:
            if block.type == Constants.CONTENT_TOOL_RESULT:
                content = parse_tool_result_content(block.content)
                tool_messages.append(
                    {
                        "role": Constants.ROLE_TOOL,
                        "tool_call_id": block.tool_use_id,
                        "content": content,
                    }
                )

    return tool_messages


def parse_tool_result_content(content: Any) -> str:  # noqa: PLR0911, PLR0912
    """Normalize ``tool_result`` content into a string.

    Args:
        content: arbitrary content (str, list, dict, None).

    Returns:
        String representation of the content, suitable for sending to OpenAI.
    """
    if content is None:
        return "No content provided"

    if isinstance(content, str):
        return content

    if isinstance(content, list):
        result_parts: list[str] = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == Constants.CONTENT_TEXT:
                result_parts.append(item.get("text", ""))
            elif isinstance(item, str):
                result_parts.append(item)
            elif isinstance(item, dict):
                if "text" in item:
                    result_parts.append(item.get("text", ""))
                else:
                    try:
                        result_parts.append(json.dumps(item, ensure_ascii=False))
                    except (TypeError, ValueError):
                        result_parts.append(str(item))
        return "\n".join(result_parts).strip()

    if isinstance(content, dict):
        if content.get("type") == Constants.CONTENT_TEXT:
            return content.get("text", "")
        try:
            return json.dumps(content, ensure_ascii=False)
        except (TypeError, ValueError):
            return str(content)

    try:
        return str(content)
    except Exception:
        return "Unparseable content"


def extract_claude_system_text(content: str | list[Any]) -> str:
    """Join the text blocks of system instructions into a single string.

    Args:
        content: string or list of blocks -- the top-level ``system`` field
            of the request, or the content of a message with role ``system``.

    Returns:
        System instruction text with surrounding whitespace stripped.
    """
    if isinstance(content, str):
        return content.strip()

    text_parts = [
        block.text for block in content if block.type == Constants.CONTENT_TEXT
    ]
    return "\n\n".join(text_parts).strip()


def convert_claude_user_message_to_input(msg: ClaudeMessage) -> list[dict[str, Any]]:
    """Convert a Claude user message into ``input`` items.

    Tool results are extracted into standalone ``function_call_output``
    items: in the Responses API they are not messages and must follow their
    paired ``function_call`` (otherwise the upstream responds with 400
    "No tool call found for function call output").

    Args:
        msg: message with role ``user`` from the Anthropic request.

    Returns:
        List of ``input`` items: tool results and, if there is text or
        images, the user message itself.
    """
    if isinstance(msg.content, str):
        return [{"role": Constants.ROLE_USER, "content": msg.content}]

    items: list[dict[str, Any]] = []
    parts: list[dict[str, Any]] = []
    for block in msg.content:
        if block.type == Constants.CONTENT_TEXT:
            parts.append({"type": "input_text", "text": block.text})
        elif block.type == Constants.CONTENT_IMAGE:  # noqa: SIM102
            # image_url only accepts a data-URL string; the upstream rejects
            # a nested {"url": ...} object.
            if (
                isinstance(block.source, dict)
                and block.source.get("type") == "base64"
                and "media_type" in block.source
                and "data" in block.source
            ):
                parts.append(
                    {
                        "type": "input_image",
                        "image_url": (
                            f"data:{block.source['media_type']};"
                            f"base64,{block.source['data']}"
                        ),
                    }
                )
        elif block.type == Constants.CONTENT_TOOL_RESULT:
            items.append(
                {
                    "type": "function_call_output",
                    "call_id": block.tool_use_id,
                    "output": parse_tool_result_content(block.content),
                }
            )

    if parts:
        items.append({"role": Constants.ROLE_USER, "content": parts})
    return items


def convert_claude_assistant_message_to_input(
    msg: ClaudeMessage,
    *,
    reasoning_cache: ReasoningCache,
) -> list[dict[str, Any]]:
    """Convert a Claude assistant message into ``input`` items.

    Previously stored reasoning items for the same call are inserted before
    each ``function_call``: the upstream restores the chain of reasoning
    only if they come strictly BEFORE the call (it accepts them after the
    call too, but with no effect). On a cache miss nothing is inserted --
    behavior falls back to the previous one.

    Args:
        msg: message with role ``assistant`` from the Anthropic request.
        reasoning_cache: cache of reasoning items keyed by ``call_id``.

    Returns:
        List of ``input`` items: a text message (if there is text) and a
        ``function_call`` item for each ``tool_use`` block, each preceded by
        its reasoning items.
    """
    if isinstance(msg.content, str):
        return [{"role": Constants.ROLE_ASSISTANT, "content": msg.content}]

    text_parts: list[str] = []
    calls: list[dict[str, Any]] = []
    for block in msg.content:
        if block.type == Constants.CONTENT_TEXT:
            text_parts.append(block.text)
        elif block.type == Constants.CONTENT_TOOL_USE:
            calls.extend(reasoning_cache.get(block.id))
            calls.append(
                {
                    "type": "function_call",
                    "call_id": block.id,
                    "name": block.name,
                    "arguments": json.dumps(block.input, ensure_ascii=False),
                }
            )

    items: list[dict[str, Any]] = []
    if text_parts:
        items.append(
            {"role": Constants.ROLE_ASSISTANT, "content": "".join(text_parts)}
        )
    items.extend(calls)
    return items


def convert_claude_messages_to_input(
    messages: list[ClaudeMessage],
    *,
    reasoning_cache: ReasoningCache,
) -> list[dict[str, Any]]:
    """Convert a Claude ``messages`` array into a Responses API ``input`` array.

    The Responses API has no ``tool`` role (only user, assistant, system,
    developer are allowed), so tool_use/tool_result pairs are unpacked into
    standalone ``function_call``/``function_call_output`` items.

    Args:
        messages: conversation messages from the Anthropic request.
        reasoning_cache: cache of reasoning items keyed by ``call_id``.

    Returns:
        Flat list of ``input`` items.
    """
    items: list[dict[str, Any]] = []
    for msg in messages:
        if msg.role == Constants.ROLE_USER:
            items.extend(convert_claude_user_message_to_input(msg))
        elif msg.role == Constants.ROLE_ASSISTANT:
            items.extend(
                convert_claude_assistant_message_to_input(
                    msg, reasoning_cache=reasoning_cache
                )
            )
        elif msg.role == Constants.ROLE_SYSTEM:
            # A positional system role inside messages is kept in place:
            # moving it into instructions would break the ordering of
            # instructions relative to user/assistant.
            system_text = extract_claude_system_text(msg.content)
            if system_text:
                items.append(
                    {"role": Constants.ROLE_SYSTEM, "content": system_text}
                )
    return items


def convert_claude_to_responses(
    claude_request: ClaudeMessagesRequest,
    openai_model: str,
    *,
    max_tokens_limit: int,
    reasoning_effort_fallback: ReasoningEffort,
    reasoning_cache: ReasoningCache,
    min_tokens_limit: int = 100,
) -> dict[str, Any]:
    """Convert an Anthropic Messages request into OpenAI Responses API format.

    The reasoning level is taken from the request's ``output_config.effort``
    without translating the value: the Anthropic and OpenAI level sets are
    identical (low/medium/high/xhigh/max). If the client did not send a
    level, the provider's configured setting is used.

    ``temperature``/``top_p`` are not carried over: reasoning models reject
    them with "Unsupported parameter ... is not supported with this model".
    ``stop_sequences`` is not carried over: the Responses API has no such
    parameter.

    Args:
        claude_request: parsed Anthropic ``/v1/messages`` request.
        openai_model: name of the upstream OpenAI model.
        max_tokens_limit: maximum allowed value for ``max_output_tokens``.
        reasoning_effort_fallback: reasoning level from the provider
            configuration, applied when the request did not set
            ``output_config.effort``.
        reasoning_cache: cache of reasoning items, used to insert them
            before the corresponding ``function_call``.
        min_tokens_limit: minimum allowed value for ``max_output_tokens``.

    Returns:
        OpenAI Responses API request dict.
    """
    effort: str = reasoning_effort_fallback
    if claude_request.output_config is not None and claude_request.output_config.effort:
        effort = claude_request.output_config.effort

    responses_request: dict[str, Any] = {
        "model": openai_model,
        "input": convert_claude_messages_to_input(
            claude_request.messages, reasoning_cache=reasoning_cache
        ),
        "max_output_tokens": min(
            max(claude_request.max_tokens, min_tokens_limit),
            max_tokens_limit,
        ),
        "reasoning": {"effort": effort},
        "stream": bool(claude_request.stream),
    }

    if claude_request.system is not None:
        instructions = extract_claude_system_text(claude_request.system)
        if instructions:
            responses_request["instructions"] = instructions

    if claude_request.tools:
        # Flat shape: the upstream rejects a nested function object
        # ("Missing required parameter: 'tools[0].name'").
        responses_tools = [
            {
                "type": Constants.TOOL_FUNCTION,
                "name": tool.name,
                "description": tool.description or "",
                "parameters": tool.input_schema,
            }
            for tool in claude_request.tools
            if tool.name and tool.name.strip()
        ]
        if responses_tools:
            responses_request["tools"] = responses_tools

    if claude_request.tool_choice:
        choice_type = claude_request.tool_choice.get("type")
        if choice_type == "tool" and "name" in claude_request.tool_choice:
            responses_request["tool_choice"] = {
                "type": Constants.TOOL_FUNCTION,
                "name": claude_request.tool_choice["name"],
            }
        elif choice_type == "any":
            # Anthropic's "any" forces a tool call; the OpenAI equivalent is
            # "required", not "auto" (auto leaves the call up to the model).
            responses_request["tool_choice"] = "required"
        else:
            responses_request["tool_choice"] = "auto"

    logger.debug(
        "converted claude request to responses format",
        responses_request=responses_request,
    )
    return responses_request
