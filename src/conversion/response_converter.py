"""Convert OpenAI Chat Completions responses -> Anthropic Messages API.

Ported from claude-code-proxy (src/conversion/response_converter.py). Contains:

- ``convert_openai_to_claude_response`` for non-streaming responses;
- ``convert_openai_streaming_to_claude_with_cancellation`` for streaming
  conversion of SSE into Anthropic events (``message_start``,
  ``content_block_*``, ``message_delta``, ``message_stop``), tracking client
  disconnects via ``ClientChannel.is_disconnected()`` and calling
  ``openai_client.cancel_request(request_id)``.
"""

from __future__ import annotations

import json
import traceback
import uuid
from typing import TYPE_CHECKING, Any

import httpx
from openai import APIError

from const import Constants

# Building the error frame lives in errors: the same frame closes the
# passthrough stream, which does not depend on the conversion layer. The
# local name is kept.
from errors import ProviderError
from errors import stream_error_event as _stream_error_event

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from models.claude import ClaudeMessagesRequest
    from providers.base import ClientChannel
    from services.reasoning_cache import ReasoningCache


def convert_openai_to_claude_response(
    openai_response: dict[str, Any], original_request: ClaudeMessagesRequest
) -> dict[str, Any]:
    """Convert a non-streaming OpenAI response into Anthropic Messages format.

    Args:
        openai_response: OpenAI Chat Completions response body.
        original_request: original Anthropic request (needed for the
            ``model`` field).

    Returns:
        Response in Anthropic ``message`` format.
    """

    choices = openai_response.get("choices", [])
    if not choices:
        raise ProviderError(message="No choices in OpenAI response", status_code=500)

    choice = choices[0]
    message = choice.get("message", {})

    content_blocks: list[dict[str, Any]] = []

    text_content = message.get("content")
    if text_content is not None:
        content_blocks.append({"type": Constants.CONTENT_TEXT, "text": text_content})

    tool_calls = message.get("tool_calls", []) or []
    for tool_call in tool_calls:
        if tool_call.get("type") == Constants.TOOL_FUNCTION:
            function_data = tool_call.get(Constants.TOOL_FUNCTION, {})
            try:
                arguments = json.loads(function_data.get("arguments", "{}"))
            except json.JSONDecodeError:
                arguments = {"raw_arguments": function_data.get("arguments", "")}

            content_blocks.append(
                {
                    "type": Constants.CONTENT_TOOL_USE,
                    "id": tool_call.get("id", f"tool_{uuid.uuid4()}"),
                    "name": function_data.get("name", ""),
                    "input": arguments,
                }
            )

    if not content_blocks:
        content_blocks.append({"type": Constants.CONTENT_TEXT, "text": ""})

    finish_reason = choice.get("finish_reason", "stop")
    stop_reason = {
        "stop": Constants.STOP_END_TURN,
        "length": Constants.STOP_MAX_TOKENS,
        "tool_calls": Constants.STOP_TOOL_USE,
        "function_call": Constants.STOP_TOOL_USE,
    }.get(finish_reason, Constants.STOP_END_TURN)

    claude_response = {
        "id": openai_response.get("id", f"msg_{uuid.uuid4()}"),
        "type": "message",
        "role": Constants.ROLE_ASSISTANT,
        "model": original_request.model,
        "content": content_blocks,
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": {
            "input_tokens": openai_response.get("usage", {}).get("prompt_tokens", 0),
            "output_tokens": openai_response.get("usage", {}).get(
                "completion_tokens", 0
            ),
        },
    }

    return claude_response  # noqa: RET504


async def convert_openai_streaming_to_claude_with_cancellation(  # noqa: PLR0912, PLR0915
    openai_stream: AsyncIterator[str],
    original_request: ClaudeMessagesRequest,
    logger: Any,
    client_channel: ClientChannel,
    openai_client: Any,
    request_id: str,
) -> AsyncIterator[str]:
    """Convert an OpenAI SSE stream into an Anthropic SSE stream with cancellation support.

    Tracks client disconnects via ``client_channel.is_disconnected()`` and
    calls ``openai_client.cancel_request(request_id)`` on disconnect. Also
    extracts ``usage`` fields from OpenAI chunks, including
    ``cached_tokens`` from ``prompt_tokens_details``, and forwards them into
    the final ``message_delta``.

    Args:
        openai_stream: async iterator of SSE lines from OpenAI.
        original_request: original Anthropic request (needed for the
            ``model`` field).
        logger: logger for info/warning/error messages.
        client_channel: client channel used to check disconnect status (see
            ``providers.base.ClientChannel``).
        openai_client: provider exposing a ``cancel_request`` method.
        request_id: upstream request identifier used for cancellation.

    Yields:
        SSE lines in Anthropic Messages streaming API format.
    """

    message_id = f"msg_{uuid.uuid4().hex[:24]}"

    yield f"event: {Constants.EVENT_MESSAGE_START}\ndata: {json.dumps({'type': Constants.EVENT_MESSAGE_START, 'message': {'id': message_id, 'type': 'message', 'role': Constants.ROLE_ASSISTANT, 'model': original_request.model, 'content': [], 'stop_reason': None, 'stop_sequence': None, 'usage': {'input_tokens': 0, 'output_tokens': 0}}}, ensure_ascii=False)}\n\n"  # noqa: E501

    yield f"event: {Constants.EVENT_CONTENT_BLOCK_START}\ndata: {json.dumps({'type': Constants.EVENT_CONTENT_BLOCK_START, 'index': 0, 'content_block': {'type': Constants.CONTENT_TEXT, 'text': ''}}, ensure_ascii=False)}\n\n"  # noqa: E501

    yield f"event: {Constants.EVENT_PING}\ndata: {json.dumps({'type': Constants.EVENT_PING}, ensure_ascii=False)}\n\n"  # noqa: E501

    text_block_index = 0
    tool_block_counter = 0
    current_tool_calls: dict[int, dict[str, Any]] = {}
    final_stop_reason = Constants.STOP_END_TURN
    usage_data: dict[str, int] = {"input_tokens": 0, "output_tokens": 0}

    try:
        async for line in openai_stream:
            if await client_channel.is_disconnected():
                logger.info(f"Client disconnected, cancelling request {request_id}")
                openai_client.cancel_request(request_id)
                break

            if line.strip():  # noqa: SIM102
                if line.startswith("data: "):
                    chunk_data = line[6:]
                    if chunk_data.strip() == "[DONE]":
                        break

                    try:
                        chunk = json.loads(chunk_data)
                        usage = chunk.get("usage", None)
                        if usage:
                            cache_read_input_tokens = 0
                            prompt_tokens_details = usage.get("prompt_tokens_details", {})
                            if prompt_tokens_details:
                                cache_read_input_tokens = prompt_tokens_details.get(
                                    "cached_tokens", 0
                                )
                            usage_data = {
                                "input_tokens": usage.get("prompt_tokens", 0),
                                "output_tokens": usage.get("completion_tokens", 0),
                                "cache_read_input_tokens": cache_read_input_tokens,
                            }
                        choices = chunk.get("choices", [])
                        if not choices:
                            continue
                    except json.JSONDecodeError as e:
                        logger.warning(
                            f"Failed to parse chunk: {chunk_data}, error: {e}"
                        )
                        continue

                    choice = choices[0]
                    delta = choice.get("delta", {})
                    finish_reason = choice.get("finish_reason")

                    if delta and "content" in delta and delta["content"] is not None:
                        yield f"event: {Constants.EVENT_CONTENT_BLOCK_DELTA}\ndata: {json.dumps({'type': Constants.EVENT_CONTENT_BLOCK_DELTA, 'index': text_block_index, 'delta': {'type': Constants.DELTA_TEXT, 'text': delta['content']}}, ensure_ascii=False)}\n\n"  # noqa: E501

                    if "tool_calls" in delta and delta["tool_calls"]:
                        for tc_delta in delta["tool_calls"]:
                            tc_index = tc_delta.get("index", 0)

                            if tc_index not in current_tool_calls:
                                current_tool_calls[tc_index] = {
                                    "id": None,
                                    "name": None,
                                    "args_buffer": "",
                                    "json_sent": False,
                                    "claude_index": None,
                                    "started": False,
                                }

                            tool_call = current_tool_calls[tc_index]

                            if tc_delta.get("id"):
                                tool_call["id"] = tc_delta["id"]

                            function_data = tc_delta.get(Constants.TOOL_FUNCTION, {})
                            if function_data.get("name"):
                                tool_call["name"] = function_data["name"]

                            if (
                                tool_call["id"]
                                and tool_call["name"]
                                and not tool_call["started"]
                            ):
                                tool_block_counter += 1
                                claude_index = text_block_index + tool_block_counter
                                tool_call["claude_index"] = claude_index
                                tool_call["started"] = True

                                yield f"event: {Constants.EVENT_CONTENT_BLOCK_START}\ndata: {json.dumps({'type': Constants.EVENT_CONTENT_BLOCK_START, 'index': claude_index, 'content_block': {'type': Constants.CONTENT_TOOL_USE, 'id': tool_call['id'], 'name': tool_call['name'], 'input': {}}}, ensure_ascii=False)}\n\n"  # noqa: E501

                            if (
                                "arguments" in function_data
                                and tool_call["started"]
                                and function_data["arguments"] is not None
                            ):
                                tool_call["args_buffer"] += function_data["arguments"]

                                try:
                                    json.loads(tool_call["args_buffer"])
                                    if not tool_call["json_sent"]:
                                        yield f"event: {Constants.EVENT_CONTENT_BLOCK_DELTA}\ndata: {json.dumps({'type': Constants.EVENT_CONTENT_BLOCK_DELTA, 'index': tool_call['claude_index'], 'delta': {'type': Constants.DELTA_INPUT_JSON, 'partial_json': tool_call['args_buffer']}}, ensure_ascii=False)}\n\n"  # noqa: E501
                                        tool_call["json_sent"] = True
                                except json.JSONDecodeError:
                                    pass

                    if finish_reason:
                        if finish_reason == "length":
                            final_stop_reason = Constants.STOP_MAX_TOKENS
                        elif finish_reason in ["tool_calls", "function_call"]:
                            final_stop_reason = Constants.STOP_TOOL_USE
                        elif finish_reason == "stop":
                            final_stop_reason = Constants.STOP_END_TURN
                        else:
                            final_stop_reason = Constants.STOP_END_TURN

    except ProviderError as e:
        if e.status_code == 499:
            logger.info(f"Request {request_id} was cancelled")
            error_event = {
                "type": "error",
                "error": {
                    "type": "cancelled",
                    "message": "Request was cancelled by client",
                },
            }
            yield f"event: error\ndata: {json.dumps(error_event, ensure_ascii=False)}\n\n"
            return
        # The response has already started (the preamble went out) -- raising
        # would tear down the TCP socket (ECONNRESET). Close the SSE stream
        # with a proper error-event mapping the status to an Anthropic type,
        # then end the generator (no message_stop: the upstream was
        # interrupted, the terminal sequence would be invalid).
        logger.warning(
            "upstream error mid-stream; closing anthropic stream with error-event",
            provider=openai_client.name,
            request_id=request_id,
            status_code=e.status_code,
            detail=e.message,
        )
        yield _stream_error_event(e.status_code, e.message)
        return
    except APIError as e:
        # APIError from the OpenAI SDK mid-stream (not mapped to
        # ProviderError -- e.g. an error event in the SSE). The response has
        # already started -- same logic applies.
        status_code = getattr(e, "status_code", 502) or 502
        logger.warning(
            "openai api error mid-stream; closing anthropic stream with error-event",
            provider=openai_client.name,
            request_id=request_id,
            status_code=status_code,
            error=str(e),
        )
        yield _stream_error_event(status_code, str(e))
        return
    except (httpx.HTTPError, httpx.StreamError) as e:
        # Transport break with the upstream mid-stream (e.g. an incomplete
        # chunked read). Not an error-event, but a graceful finish: fall
        # through to the terminal events below (content_block_stop +
        # message_delta + message_stop), preserving already sent partial
        # content.
        logger.warning(
            "upstream stream aborted; closing anthropic stream gracefully",
            provider=openai_client.name,
            request_id=request_id,
            error_type=type(e).__name__,
        )
    except Exception as e:
        logger.error(f"Streaming error: {e}")
        logger.error(traceback.format_exc())
        error_event = {
            "type": "error",
            "error": {"type": "api_error", "message": f"Streaming error: {str(e)}"},
        }
        yield f"event: error\ndata: {json.dumps(error_event, ensure_ascii=False)}\n\n"
        return

    yield f"event: {Constants.EVENT_CONTENT_BLOCK_STOP}\ndata: {json.dumps({'type': Constants.EVENT_CONTENT_BLOCK_STOP, 'index': text_block_index}, ensure_ascii=False)}\n\n"  # noqa: E501

    for tool_data in current_tool_calls.values():
        if tool_data.get("started") and tool_data.get("claude_index") is not None:
            yield f"event: {Constants.EVENT_CONTENT_BLOCK_STOP}\ndata: {json.dumps({'type': Constants.EVENT_CONTENT_BLOCK_STOP, 'index': tool_data['claude_index']}, ensure_ascii=False)}\n\n"  # noqa: E501

    yield f"event: {Constants.EVENT_MESSAGE_DELTA}\ndata: {json.dumps({'type': Constants.EVENT_MESSAGE_DELTA, 'delta': {'stop_reason': final_stop_reason, 'stop_sequence': None}, 'usage': usage_data}, ensure_ascii=False)}\n\n"  # noqa: E501
    yield f"event: {Constants.EVENT_MESSAGE_STOP}\ndata: {json.dumps({'type': Constants.EVENT_MESSAGE_STOP}, ensure_ascii=False)}\n\n"  # noqa: E501


# Responses API event types observed in the live stream that are
# intentionally not mapped to Anthropic content: lifecycle markers, content
# part wrappers, and duplicates of already-collected data (``*.done``).
# Reasoning summary belongs here too: it is not carried into content,
# otherwise internal reasoning would leak out as assistant text. An event
# outside this set and outside the handlers below is treated as unknown and
# logged -- silently dropping content is not acceptable.
_RESPONSES_IGNORED_EVENTS: frozenset[str] = frozenset({
    "response.created",
    "response.in_progress",
    "response.output_item.done",
    "response.content_part.added",
    "response.content_part.done",
    "response.output_text.done",
    "response.reasoning_summary_part.added",
    "response.reasoning_summary_part.done",
    "response.reasoning_summary_text.delta",
    "response.reasoning_summary_text.done",
})

# Terminal events of the Responses API stream: carry the final ``response``
# object with ``status``, ``incomplete_details`` and ``usage``. The stream
# ends with one of these -- the raw Responses API SSE has no equivalent of
# ``data: [DONE]``.
_RESPONSES_TERMINAL_EVENTS: frozenset[str] = frozenset({
    "response.completed",
    "response.incomplete",
    "response.failed",
})

# Response status for a Responses API upstream error; returned to the
# client as api_error via 502 (the terminal event itself carries no HTTP
# status).
_RESPONSES_FAILED_STATUS_CODE = 502


def _responses_usage(usage: dict[str, Any] | None) -> dict[str, int]:
    """Extract the Responses API response usage into Anthropic format.

    The Responses API names fields differently from Chat Completions:
    ``input_tokens`` / ``output_tokens`` instead of ``prompt_tokens`` /
    ``completion_tokens``, and the cached token count lives in
    ``input_tokens_details.cached_tokens``.

    Args:
        usage: ``usage`` object from the Responses API response (may be
            absent).

    Returns:
        Usage dict in Anthropic format. When data is absent -- zeros without
        the ``cache_read_input_tokens`` key.
    """
    if not usage:
        return {"input_tokens": 0, "output_tokens": 0}

    input_details = usage.get("input_tokens_details") or {}
    return {
        "input_tokens": usage.get("input_tokens", 0),
        "output_tokens": usage.get("output_tokens", 0),
        "cache_read_input_tokens": input_details.get("cached_tokens", 0),
    }


def _responses_stop_reason(response_body: dict[str, Any], *, has_tool_use: bool) -> str:
    """Determine the Anthropic ``stop_reason`` from the final Responses API object.

    The Responses API has no equivalent of ``finish_reason``: hitting the
    token limit is expressed as status ``incomplete`` with
    ``incomplete_details.reason == "max_output_tokens"``, and a tool call is
    only visible by the presence of ``function_call`` items in the output.

    Args:
        response_body: the ``response`` object from the terminal event, or
            the non-streaming response body.
        has_tool_use: whether the output contained ``function_call`` items.

    Returns:
        Anthropic stop reason (``max_tokens``, ``tool_use``, ``end_turn``).
    """
    if response_body.get("status") == "incomplete":
        incomplete_details = response_body.get("incomplete_details") or {}
        if incomplete_details.get("reason") == "max_output_tokens":
            return Constants.STOP_MAX_TOKENS

    if has_tool_use:
        return Constants.STOP_TOOL_USE
    return Constants.STOP_END_TURN


def extract_reasoning_by_call_id(
    output_items: list[dict[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    """Map output reasoning items to the ``call_id`` of the next ``function_call``.

    Items are accumulated up to the nearest following ``function_call`` and
    attributed to it: with multiple tool calls in one response, repeating
    the same items for every ``call_id`` would duplicate them in the
    ``input`` of the next step. A trailing run with no ``function_call`` is
    dropped -- there is no anchor in the Anthropic history it could be
    returned under.

    Args:
        output_items: ``output`` array of the Responses API response (the
            non-streaming response body, or the final object of the
            stream's terminal event).

    Returns:
        Mapping of ``call_id`` -> ``reasoning`` items that preceded the call.
    """
    by_call_id: dict[str, list[dict[str, Any]]] = {}
    pending: list[dict[str, Any]] = []

    for item in output_items:
        item_type = item.get("type")
        if item_type == "reasoning":
            pending.append(item)
        elif item_type == "function_call":
            call_id = item.get("call_id")
            if pending and call_id:
                by_call_id[call_id] = pending
            pending = []

    return by_call_id


def convert_responses_to_claude_response(
    responses_response: dict[str, Any],
    original_request: ClaudeMessagesRequest,
    *,
    reasoning_cache: ReasoningCache,
) -> dict[str, Any]:
    """Convert a non-streaming OpenAI Responses API response into Anthropic format.

    Unlike Chat Completions, the payload lives in a flat ``output`` array
    (no ``choices``): ``message`` items carry ``output_text`` text parts,
    ``function_call`` items are a tool call with the identifier in
    ``call_id`` (there is no ``tool_calls[i]`` index here). ``reasoning``
    items are still not carried into Anthropic content (internal reasoning
    must not leak out as assistant text), but they are stored in the cache:
    on the next step of a tool call chain they are returned to the upstream.

    Args:
        responses_response: OpenAI Responses API response body.
        original_request: original Anthropic request (needed for the
            ``model`` field).
        reasoning_cache: cache of reasoning items, populated by ``call_id``.

    Returns:
        Response in Anthropic ``message`` format.

    Raises:
        ProviderError: if the response has no ``output`` array.
    """

    output_items = responses_response.get("output", [])
    if not output_items:
        raise ProviderError(
            message="No output in OpenAI Responses response", status_code=500
        )

    for call_id, reasoning_items in extract_reasoning_by_call_id(output_items).items():
        reasoning_cache.store(call_id, reasoning_items)

    content_blocks: list[dict[str, Any]] = []
    has_tool_use = False

    for item in output_items:
        item_type = item.get("type")

        if item_type == "message":
            for part in item.get("content", []) or []:
                if part.get("type") == "output_text":
                    content_blocks.append(
                        {"type": Constants.CONTENT_TEXT, "text": part.get("text", "")}
                    )

        elif item_type == "function_call":
            has_tool_use = True
            try:
                arguments = json.loads(item.get("arguments") or "{}")
            except json.JSONDecodeError:
                arguments = {"raw_arguments": item.get("arguments", "")}

            content_blocks.append(
                {
                    "type": Constants.CONTENT_TOOL_USE,
                    "id": item.get("call_id") or item.get("id") or f"tool_{uuid.uuid4()}",
                    "name": item.get("name", ""),
                    "input": arguments,
                }
            )

    if not content_blocks:
        content_blocks.append({"type": Constants.CONTENT_TEXT, "text": ""})

    return {
        "id": responses_response.get("id", f"msg_{uuid.uuid4()}"),
        "type": "message",
        "role": Constants.ROLE_ASSISTANT,
        "model": original_request.model,
        "content": content_blocks,
        "stop_reason": _responses_stop_reason(
            responses_response, has_tool_use=has_tool_use
        ),
        "stop_sequence": None,
        "usage": _responses_usage(responses_response.get("usage")),
    }


async def convert_responses_streaming_to_claude_with_cancellation(
    openai_stream: AsyncIterator[str],
    original_request: ClaudeMessagesRequest,
    logger: Any,
    client_channel: ClientChannel,
    openai_client: Any,
    request_id: str,
    *,
    reasoning_cache: ReasoningCache,
) -> AsyncIterator[str]:
    """Convert a Responses API SSE stream into an Anthropic SSE stream with cancellation.

    Counterpart to ``convert_openai_streaming_to_claude_with_cancellation``
    for ``/v1/responses``: sends the client the same sequence of Anthropic
    events, but parses Responses API semantic events (``response.*``)
    instead of ``choices[0].delta`` deltas. As in the Chat Completions
    version, the text block opens before the first upstream data and closes
    in a single terminal tail together with the tool blocks.

    Tool block indices are numbered in the order ``function_call`` items
    appear in the stream, not by the upstream's ``output_index``. Tool
    arguments are sent as a single ``input_json_delta`` in full, once the
    accumulated buffer first becomes valid JSON.

    Args:
        openai_stream: async iterator of SSE lines from the OpenAI Responses
            API.
        original_request: original Anthropic request (needed for the
            ``model`` field).
        logger: logger for info/warning/error messages.
        client_channel: client channel used to check disconnect status (see
            ``providers.base.ClientChannel``).
        openai_client: provider exposing a ``cancel_request`` method.
        request_id: upstream request identifier used for cancellation.
        reasoning_cache: cache of reasoning items, populated by ``call_id``.

    Yields:
        SSE lines in Anthropic Messages streaming API format.
    """

    message_id = f"msg_{uuid.uuid4().hex[:24]}"

    yield f"event: {Constants.EVENT_MESSAGE_START}\ndata: {json.dumps({'type': Constants.EVENT_MESSAGE_START, 'message': {'id': message_id, 'type': 'message', 'role': Constants.ROLE_ASSISTANT, 'model': original_request.model, 'content': [], 'stop_reason': None, 'stop_sequence': None, 'usage': {'input_tokens': 0, 'output_tokens': 0}}}, ensure_ascii=False)}\n\n"  # noqa: E501

    yield f"event: {Constants.EVENT_CONTENT_BLOCK_START}\ndata: {json.dumps({'type': Constants.EVENT_CONTENT_BLOCK_START, 'index': 0, 'content_block': {'type': Constants.CONTENT_TEXT, 'text': ''}}, ensure_ascii=False)}\n\n"  # noqa: E501

    yield f"event: {Constants.EVENT_PING}\ndata: {json.dumps({'type': Constants.EVENT_PING}, ensure_ascii=False)}\n\n"  # noqa: E501

    text_block_index = 0
    tool_block_counter = 0
    current_tool_calls: dict[int, dict[str, Any]] = {}
    final_stop_reason = Constants.STOP_END_TURN
    usage_data: dict[str, int] = {"input_tokens": 0, "output_tokens": 0}
    unknown_event_types: set[str] = set()

    try:
        async for line in openai_stream:
            if await client_channel.is_disconnected():
                logger.info(f"Client disconnected, cancelling request {request_id}")
                openai_client.cancel_request(request_id)
                break

            if not line.strip() or not line.startswith("data: "):
                continue

            chunk_data = line[6:]
            if chunk_data.strip() == "[DONE]":
                break

            try:
                event = json.loads(chunk_data)
            except json.JSONDecodeError as e:
                logger.warning(f"Failed to parse chunk: {chunk_data}, error: {e}")
                continue

            event_type = event.get("type") or ""

            if event_type == "response.output_text.delta":
                delta_text = event.get("delta")
                if delta_text:
                    yield f"event: {Constants.EVENT_CONTENT_BLOCK_DELTA}\ndata: {json.dumps({'type': Constants.EVENT_CONTENT_BLOCK_DELTA, 'index': text_block_index, 'delta': {'type': Constants.DELTA_TEXT, 'text': delta_text}}, ensure_ascii=False)}\n\n"  # noqa: E501

            elif event_type == "response.output_item.added":
                item = event.get("item") or {}
                # The id and name of a function_call item arrive in a single
                # event, so the tool block opens immediately -- nothing to
                # accumulate.
                if item.get("type") == "function_call":
                    tool_block_counter += 1
                    claude_index = text_block_index + tool_block_counter
                    tool_id = item.get("call_id") or item.get("id") or ""
                    tool_name = item.get("name") or ""
                    current_tool_calls[event.get("output_index", 0)] = {
                        "id": tool_id,
                        "name": tool_name,
                        "args_buffer": "",
                        "json_sent": False,
                        "claude_index": claude_index,
                    }

                    yield f"event: {Constants.EVENT_CONTENT_BLOCK_START}\ndata: {json.dumps({'type': Constants.EVENT_CONTENT_BLOCK_START, 'index': claude_index, 'content_block': {'type': Constants.CONTENT_TOOL_USE, 'id': tool_id, 'name': tool_name, 'input': {}}}, ensure_ascii=False)}\n\n"  # noqa: E501

            elif event_type == "response.function_call_arguments.delta":
                tool_call = current_tool_calls.get(event.get("output_index", 0))
                delta_arguments = event.get("delta")
                if tool_call is not None and delta_arguments and not tool_call["json_sent"]:
                    tool_call["args_buffer"] += delta_arguments
                    try:
                        json.loads(tool_call["args_buffer"])
                    except json.JSONDecodeError:
                        pass
                    else:
                        yield f"event: {Constants.EVENT_CONTENT_BLOCK_DELTA}\ndata: {json.dumps({'type': Constants.EVENT_CONTENT_BLOCK_DELTA, 'index': tool_call['claude_index'], 'delta': {'type': Constants.DELTA_INPUT_JSON, 'partial_json': tool_call['args_buffer']}}, ensure_ascii=False)}\n\n"  # noqa: E501
                        tool_call["json_sent"] = True

            elif event_type == "response.function_call_arguments.done":
                # Authoritative argument string in case the incremental
                # buffer never assembled into valid JSON.
                tool_call = current_tool_calls.get(event.get("output_index", 0))
                if tool_call is not None and not tool_call["json_sent"]:
                    tool_call["args_buffer"] = event.get("arguments") or "{}"

                    yield f"event: {Constants.EVENT_CONTENT_BLOCK_DELTA}\ndata: {json.dumps({'type': Constants.EVENT_CONTENT_BLOCK_DELTA, 'index': tool_call['claude_index'], 'delta': {'type': Constants.DELTA_INPUT_JSON, 'partial_json': tool_call['args_buffer']}}, ensure_ascii=False)}\n\n"  # noqa: E501
                    tool_call["json_sent"] = True

            elif event_type in _RESPONSES_TERMINAL_EVENTS:
                response_body = event.get("response") or {}
                usage_data = _responses_usage(response_body.get("usage"))

                if response_body.get("status") == "failed":
                    error_body = response_body.get("error") or {}
                    message = error_body.get("message") or "upstream response failed"
                    logger.warning(
                        "responses stream failed; closing anthropic stream with error-event",
                        provider=openai_client.name,
                        request_id=request_id,
                        error=message,
                    )
                    yield _stream_error_event(_RESPONSES_FAILED_STATUS_CODE, message)
                    return

                # The source of reasoning items is the final response object,
                # not response.output_item.added: at the time of "added" the
                # encrypted_content blob is still incomplete (measured: 932
                # vs 1036 characters for the same item in the final output).
                for call_id, reasoning_items in extract_reasoning_by_call_id(
                    response_body.get("output") or []
                ).items():
                    reasoning_cache.store(call_id, reasoning_items)

                final_stop_reason = _responses_stop_reason(
                    response_body, has_tool_use=bool(current_tool_calls)
                )
                break

            elif event_type == "error":
                message = event.get("message") or "upstream stream error"
                logger.warning(
                    "responses stream error event; closing anthropic stream with error-event",
                    provider=openai_client.name,
                    request_id=request_id,
                    error=message,
                )
                yield _stream_error_event(_RESPONSES_FAILED_STATUS_CODE, message)
                return

            elif event_type not in _RESPONSES_IGNORED_EVENTS:
                # Unknown event type: content may have been lost, so the fact
                # is recorded in the log (once per type, to avoid spamming
                # the log) -- but the stream continues.
                if event_type not in unknown_event_types:
                    unknown_event_types.add(event_type)
                    logger.warning(
                        "unknown responses stream event type; content may be dropped",
                        provider=openai_client.name,
                        request_id=request_id,
                        event_type=event_type,
                    )

    except ProviderError as e:
        if e.status_code == 499:
            logger.info(f"Request {request_id} was cancelled")
            error_event = {
                "type": "error",
                "error": {
                    "type": "cancelled",
                    "message": "Request was cancelled by client",
                },
            }
            yield f"event: error\ndata: {json.dumps(error_event, ensure_ascii=False)}\n\n"
            return
        # The response has already started (the preamble went out) -- raising
        # would tear down the TCP socket (ECONNRESET). Close the SSE stream
        # with a proper error-event mapping the status to an Anthropic type,
        # then end the generator (no message_stop: the upstream was
        # interrupted, the terminal sequence would be invalid).
        logger.warning(
            "upstream error mid-stream; closing anthropic stream with error-event",
            provider=openai_client.name,
            request_id=request_id,
            status_code=e.status_code,
            detail=e.message,
        )
        yield _stream_error_event(e.status_code, e.message)
        return
    except APIError as e:
        # APIError from the OpenAI SDK mid-stream (not mapped to
        # ProviderError -- e.g. an error event in the SSE). The response has
        # already started -- same logic applies.
        status_code = getattr(e, "status_code", 502) or 502
        logger.warning(
            "openai api error mid-stream; closing anthropic stream with error-event",
            provider=openai_client.name,
            request_id=request_id,
            status_code=status_code,
            error=str(e),
        )
        yield _stream_error_event(status_code, str(e))
        return
    except (httpx.HTTPError, httpx.StreamError) as e:
        # Transport break with the upstream mid-stream (e.g. an incomplete
        # chunked read). Not an error-event, but a graceful finish: fall
        # through to the terminal events below, preserving already sent
        # partial content.
        logger.warning(
            "upstream stream aborted; closing anthropic stream gracefully",
            provider=openai_client.name,
            request_id=request_id,
            error_type=type(e).__name__,
        )
    except Exception as e:
        logger.error(f"Streaming error: {e}")
        logger.error(traceback.format_exc())
        error_event = {
            "type": "error",
            "error": {"type": "api_error", "message": f"Streaming error: {str(e)}"},
        }
        yield f"event: error\ndata: {json.dumps(error_event, ensure_ascii=False)}\n\n"
        return

    yield f"event: {Constants.EVENT_CONTENT_BLOCK_STOP}\ndata: {json.dumps({'type': Constants.EVENT_CONTENT_BLOCK_STOP, 'index': text_block_index}, ensure_ascii=False)}\n\n"  # noqa: E501

    for tool_data in current_tool_calls.values():
        yield f"event: {Constants.EVENT_CONTENT_BLOCK_STOP}\ndata: {json.dumps({'type': Constants.EVENT_CONTENT_BLOCK_STOP, 'index': tool_data['claude_index']}, ensure_ascii=False)}\n\n"  # noqa: E501

    yield f"event: {Constants.EVENT_MESSAGE_DELTA}\ndata: {json.dumps({'type': Constants.EVENT_MESSAGE_DELTA, 'delta': {'stop_reason': final_stop_reason, 'stop_sequence': None}, 'usage': usage_data}, ensure_ascii=False)}\n\n"  # noqa: E501
    yield f"event: {Constants.EVENT_MESSAGE_STOP}\ndata: {json.dumps({'type': Constants.EVENT_MESSAGE_STOP}, ensure_ascii=False)}\n\n"  # noqa: E501
