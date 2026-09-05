"""Anthropic <-> OpenAI translating provider for fleet models.

Ported client logic from claude-code-proxy (streaming, cooperative
cancellation, error classification), with the addition of a per-provider CA
bundle via a dedicated ``httpx.AsyncClient``. The incoming Claude request is
parsed by a pydantic model and translated to one of two OpenAI endpoints
based on ``cfg.api_flavor``: ``chat`` -> /v1/chat/completions,
``responses`` -> /v1/responses.

Upstream failures before the stream starts and on the non-streaming path
are rendered with ``ProviderError.error_type``; mid-stream ``event: error``
frames keep the status-derived type from ``anthropic_error_type_for_status``.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import uuid
from collections.abc import AsyncGenerator, AsyncIterator, Coroutine, Mapping
from pathlib import Path
from typing import Any, cast

import httpx
from openai import (
    APIError,
    AsyncOpenAI,
    AsyncStream,
    AuthenticationError,
    BadRequestError,
    RateLimitError,
)
from openai.types.chat import ChatCompletionChunk
from openai.types.responses import ResponseStreamEvent
from pydantic import BaseModel, SecretStr, ValidationError

from const import (
    CLAUDE_BUILTIN_TOOL_NAMES,
    CONTEXT_WINDOW_RESERVE_TOKENS,
    DROPPED_TOOLS_LOG_SAMPLE,
    MIN_COMPLETION_TOKENS,
    MIN_USEFUL_COMPLETION_TOKENS,
    UPSTREAM_MAX_RETRIES,
    Constants,
)
from conversion.request_converter import convert_claude_to_openai, convert_claude_to_responses
from conversion.response_converter import (
    convert_openai_streaming_to_claude_with_cancellation,
    convert_openai_to_claude_response,
    convert_responses_streaming_to_claude_with_cancellation,
    convert_responses_to_claude_response,
)
from errors import ProviderError, anthropic_error_body
from log import get_logger
from models.claude import ClaudeMessagesRequest, ClaudeTokenCountRequest, ClaudeTool
from providers.base import ClientChannel, ProviderResult
from routing.schema import ProviderCfg, RouteLimits
from services.http_transport import build_upstream_transport, build_upstream_verify
from services.reasoning_cache import ReasoningCache
from services.token_estimator import estimate_openai_request_tokens
from settings import UpstreamSettings

logger = get_logger(__name__)

# Transient 401 from OpenAI on reasoning models gpt-5.6-*: the upstream
# fluctuates with "insufficient permissions" even with a valid key. Retried
# before the stream starts.
_TRANSIENT_401_MARKER = "insufficient permissions"
_MAX_STREAM_401_RETRIES = 2  # 2 extra attempts (3 total)
_STREAM_401_RETRY_DELAY_S = 0.3

# Upstream 400 texts that mean the prompt overflowed the model's context
# window (OpenAI, vLLM/SGLang-style gateways, DeepSeek, Anthropic-shaped
# wrappers). Matched case-insensitively on BadRequestError only, BEFORE
# ``classify_error``, so the client gets the Anthropic-shaped
# invalid_request_error with the stable token Claude Code parses. "too
# many tokens" is deliberately absent: it also appears in 429 texts.
_CONTEXT_LENGTH_ERROR_MARKERS: tuple[str, ...] = (
    "maximum context length",
    "context length exceeded",
    "context_length_exceeded",
    "prompt is too long",
    "exceeds the model's maximum context",
)
# Stable token from the Claude Code gateway protocol: its recovery path
# (retry with a smaller max_tokens, then compact) keys on this substring.
_PROMPT_TOO_LONG_TOKEN = "capability_rejected: prompt_too_long"


def apply_param_compat(
    openai_request: dict[str, Any], cfg: ProviderCfg
) -> dict[str, Any]:
    """Adapt request parameters to upstream requirements (GPT-5.x reasoning models).

    Modifies and returns the same ``openai_request`` dict: renames the
    ``max_tokens`` key to the name from ``cfg.token_param`` (when it
    differs) and drops parameters listed in ``cfg.drop_params``. Values of
    ``cfg.drop_params`` are already validated by the ``ProviderCfg`` schema.

    The rename is driven by ``cfg.token_param`` rather than a hardcoded
    literal, so the limit's name in the request always matches the
    provider's configuration. For the Responses API the call is a no-op:
    ``convert_claude_to_responses`` sets ``max_output_tokens`` directly and
    never creates a ``max_tokens`` key, so no double rename occurs, while
    ``drop_params`` remains a working emergency lever for both endpoint
    flavors.

    Args:
        openai_request: OpenAI request dict (Chat Completions or Responses).
        cfg: provider configuration.

    Returns:
        The same dict with compatibility applied.
    """
    if cfg.token_param != "max_tokens" and "max_tokens" in openai_request:
        openai_request[cfg.token_param] = openai_request.pop("max_tokens")
    for name in cfg.drop_params:
        openai_request.pop(name, None)
    return openai_request


def cap_tools(
    tools: list[ClaudeTool] | None,
    tools_max: int,
    provider_name: str,
) -> list[ClaudeTool] | None:
    """Truncate the ``tools`` array to the provider's limit, keeping builtins.

    OpenAI hard-limits the ``tools`` array to 128 elements. When exceeded,
    all Claude Code builtin tools (Bash, Read, Edit, ...) are kept, MCP
    tools are filled up to the cap in their original order, and the tail is
    dropped. If ``tools_max <= 0``, no truncation is applied.

    Args:
        tools: array of tools from the Claude request (or None).
        tools_max: maximum number of elements; 0 = no limit.
        provider_name: provider name for logging.

    Returns:
        Truncated tools array (or the original, or None).
    """
    if tools is None or tools_max <= 0 or len(tools) <= tools_max:
        return tools

    builtins = [t for t in tools if t.name in CLAUDE_BUILTIN_TOOL_NAMES]
    mcp_tools = [t for t in tools if t.name not in CLAUDE_BUILTIN_TOOL_NAMES]

    slots_for_mcp = max(0, tools_max - len(builtins))
    kept_mcp = mcp_tools[:slots_for_mcp]
    dropped = mcp_tools[slots_for_mcp:]

    if dropped:
        dropped_names = [t.name for t in dropped[:DROPPED_TOOLS_LOG_SAMPLE]]
        logger.warning(
            "tools array capped for openai provider",
            provider=provider_name,
            tools_max=tools_max,
            total_tools=len(tools),
            builtin_tools=len(builtins),
            mcp_tools=len(mcp_tools),
            dropped_count=len(dropped),
            dropped_sample=dropped_names,
        )

    # builtins first, then MCP tools in original order; result <= tools_max.
    return builtins + kept_mcp


def _json_result(status_code: int, content: dict[str, Any]) -> ProviderResult:
    """Build a ProviderResult with a JSON body, matching fastapi.JSONResponse serialization.

    The ``json.dumps`` parameters match ``starlette.responses.JSONResponse``,
    so the provider's move to ProviderResult does not change the response
    bytes.

    Args:
        status_code: HTTP status of the response.
        content: response body (Anthropic-compatible JSON).

    Returns:
        ProviderResult with a UTF-8 JSON body and a ``content-type`` header.
    """
    body = json.dumps(
        content, ensure_ascii=False, allow_nan=False, indent=None, separators=(",", ":")
    ).encode("utf-8")
    return ProviderResult(
        status_code=status_code, headers={"content-type": "application/json"}, body=body
    )


async def _encode_sse(chunks: AsyncIterator[str]) -> AsyncIterator[bytes]:
    """Encode a response_converter SSE-string stream into a ProviderResult byte stream.

    Args:
        chunks: async iterator of Anthropic SSE lines from response_converter.

    Yields:
        The same frames, encoded as UTF-8.
    """
    async for chunk in chunks:
        yield chunk.encode("utf-8")


class OpenAITranslateProvider:
    """Translating provider on top of an OpenAI-compatible upstream."""

    def __init__(
        self,
        name: str,
        cfg: ProviderCfg,
        api_key: SecretStr,
        ca_bundle_path: Path | None,
        upstream: UpstreamSettings,
    ) -> None:
        """Create the provider, the httpx client with the CA bundle, and AsyncOpenAI.

        Args:
            name: provider name in the registry.
            cfg: provider configuration.
            api_key: upstream API key.
            ca_bundle_path: path to the CA bundle, or None for system roots.
            upstream: outbound connection settings (IPv4 binding, pool
                limits).
        """
        self.name = name
        self.cfg = cfg
        timeout = httpx.Timeout(
            connect=10.0,
            read=float(cfg.stream_read_timeout_s),
            write=60.0,
            pool=10.0,
        )
        verify = build_upstream_verify(ca_bundle_path, cfg.tls_verify_hostname)
        self._http_client = httpx.AsyncClient(
            timeout=timeout, transport=build_upstream_transport(upstream, verify)
        )
        # max_retries is set explicitly: the SDK default (2) was exhausted in
        # ~1.5s and dropped the client on 429/5xx that outlived that window.
        # Retries happen inside the SDK before AsyncStream is created, so the
        # streaming response never risks splicing two different generations
        # together.
        self._client = AsyncOpenAI(
            api_key=api_key.get_secret_value(),
            base_url=cfg.base_url,
            default_headers=dict(cfg.extra_headers),
            http_client=self._http_client,
            max_retries=UPSTREAM_MAX_RETRIES,
        )
        self.active_requests: dict[str, asyncio.Event] = {}
        # Lives as long as the provider (the registry is created at process
        # startup), so a tool call chain outlives individual requests.
        # Populated only on the Responses API path.
        self._reasoning_cache = ReasoningCache()

    async def create_chat_completion(
        self, request: dict[str, Any], request_id: str | None = None
    ) -> dict[str, Any]:
        """Perform a non-streaming request to the upstream with cancellation support.

        Endpoint is selected by ``cfg.api_flavor``: ``chat`` -> chat
        completions, ``responses`` -> responses.

        Args:
            request: OpenAI request body (Chat Completions or Responses).
            request_id: identifier for cooperative cancellation.

        Returns:
            OpenAI response as a dict.

        Raises:
            ProviderError: on upstream error or cancellation (499).
        """
        if request_id:
            cancel_event = asyncio.Event()
            self.active_requests[request_id] = cancel_event

        try:
            # The SDK overload cannot be inferred from a dict argument, but
            # in the non-streaming branch both create coroutines return a
            # pydantic model -- the cast is warranted.
            completion_task = asyncio.create_task(
                cast(
                    "Coroutine[Any, Any, BaseModel]",
                    self._client.responses.create(**request)
                    if self.cfg.api_flavor == "responses"
                    else self._client.chat.completions.create(**request),
                )
            )
            if request_id:
                cancel_task = asyncio.create_task(self.active_requests[request_id].wait())
                done, pending = await asyncio.wait(
                    [completion_task, cancel_task],
                    return_when=asyncio.FIRST_COMPLETED,
                )
                for task in pending:
                    task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await task
                if cancel_task in done:
                    completion_task.cancel()
                    raise ProviderError(
                        message="Request cancelled by client", status_code=499
                    )
                completion = await completion_task
            else:
                completion = await completion_task
            result: dict[str, Any] = completion.model_dump()
            return result
        except APIError as exc:
            raise self._to_provider_error(exc) from exc
        finally:
            if request_id and request_id in self.active_requests:
                del self.active_requests[request_id]

    async def create_stream(
        self, request: dict[str, Any], request_id: str | None
    ) -> AsyncStream[ChatCompletionChunk] | AsyncStream[ResponseStreamEvent]:
        """Establish a streaming connection to OpenAI, retrying on a transient 401.

        The first contact with the upstream happens BEFORE handing a
        StreamingResponse to the client: a 401 is raised synchronously as
        ``AuthenticationError`` during ``await create(stream=True)`` (via
        ``raise_for_status`` in ``_base_client``), before ``AsyncStream`` is
        created. A transient 401 with the text "insufficient permissions" is
        retried; invalid_api_key is not. If all attempts are exhausted,
        ``ProviderError(401)`` is raised (the response has not started yet,
        the status is valid).

        This loop does not retry 429 or 5xx: the SDK itself does that inside
        ``create`` (budget ``UPSTREAM_MAX_RETRIES``, exponential backoff with
        jitter, respecting ``Retry-After``). The mechanisms are kept separate
        because 401 is not part of the SDK's retryable status set and also
        requires inspecting the error text to avoid hammering a genuinely
        broken key uselessly; merging the two policies would reduce both to
        the worse of the two.

        Cooperative cancellation is checked between attempts via
        ``client_channel.is_disconnected()``.

        The endpoint is selected by ``cfg.api_flavor``; 401 classification is
        based on the SDK exception class and the error text, so it is the
        same for both flavors.

        Args:
            request: OpenAI request body, Chat Completions or Responses
                (stream=True already set).
            request_id: identifier for cooperative cancellation.

        Returns:
            ``AsyncStream`` for further iteration.

        Raises:
            ProviderError: 401 when retries are exhausted; 499 on client
                cancellation; other statuses via ``_to_provider_error``.
        """
        if request_id and request_id not in self.active_requests:
            self.active_requests[request_id] = asyncio.Event()

        last_exc: ProviderError | None = None
        for attempt in range(_MAX_STREAM_401_RETRIES + 1):
            if (
                request_id
                and request_id in self.active_requests
                and self.active_requests[request_id].is_set()
            ):
                raise ProviderError(message="Request cancelled by client", status_code=499)
            try:
                # request["stream"]=True; the SDK overload returns
                # AsyncStream, but mypy cannot infer that from a dict
                # argument -- the cast is warranted.
                if self.cfg.api_flavor == "responses":
                    return cast(
                        "AsyncStream[ResponseStreamEvent]",
                        await self._client.responses.create(**request),
                    )
                return cast(
                    "AsyncStream[ChatCompletionChunk]",
                    await self._client.chat.completions.create(**request),
                )
            except AuthenticationError as exc:
                last_exc = self._to_provider_error(exc)
                # Only transient 401 "insufficient permissions" is retried:
                # invalid_api_key / unauthorized means the key is genuinely
                # broken.
                if _TRANSIENT_401_MARKER not in str(exc).lower():
                    raise last_exc from exc
                if attempt < _MAX_STREAM_401_RETRIES:
                    logger.warning(
                        "transient 401 from openai; retrying",
                        provider=self.name,
                        attempt=attempt + 1,
                        max_retries=_MAX_STREAM_401_RETRIES,
                    )
                    await asyncio.sleep(_STREAM_401_RETRY_DELAY_S)
                    continue
                raise last_exc from exc
            except APIError as exc:
                raise self._to_provider_error(exc) from exc

        # unreachable: the loop either returns or raises on every iteration.
        if last_exc is not None:  # pragma: no cover
            raise last_exc
        raise ProviderError(  # pragma: no cover
            message="Unexpected stream creation failure", status_code=502
        )

    async def create_chat_completion_stream(
        self,
        streaming_completion: AsyncStream[ChatCompletionChunk]
        | AsyncStream[ResponseStreamEvent],
        request_id: str | None = None,
    ) -> AsyncGenerator[str]:
        """Iterate an already-established ``AsyncStream``, yielding OpenAI SSE lines.

        Accepts an already-established stream connection (created via
        ``create_stream``), so a 401 is caught BEFORE the response stream
        starts for the client. Every event flavor is a pydantic model, so
        serialization is shared; only stream termination differs.

        Args:
            streaming_completion: ``AsyncStream`` from ``create(stream=True)``.
            request_id: identifier for cooperative cancellation.

        Yields:
            Lines of the form ``data: {json}``; for chat completions, a
            final ``data: [DONE]``.

        Raises:
            ProviderError: on upstream error or cancellation (499).
        """
        try:
            async for chunk in streaming_completion:
                if (
                    request_id
                    and request_id in self.active_requests
                    and self.active_requests[request_id].is_set()
                ):
                    raise ProviderError(
                        message="Request cancelled by client", status_code=499
                    )
                yield f"data: {json.dumps(chunk.model_dump(), ensure_ascii=False)}"
            # The Responses API does not send a [DONE] sentinel: the stream
            # terminates with a response.completed / response.incomplete /
            # response.failed event.
            if self.cfg.api_flavor == "chat":
                yield "data: [DONE]"
        except APIError as exc:
            raise self._to_provider_error(exc) from exc
        finally:
            if request_id and request_id in self.active_requests:
                del self.active_requests[request_id]

    def _to_provider_error(self, exc: APIError) -> ProviderError:
        """Map an OpenAI upstream error into a ProviderError.

        A ``BadRequestError`` whose text names a context-length overflow
        (``_CONTEXT_LENGTH_ERROR_MARKERS``) is remapped before
        ``classify_error`` into ``invalid_request_error`` with the
        ``capability_rejected: prompt_too_long`` token; other statuses are
        never remapped, so a 429 mentioning tokens keeps its meaning.

        Args:
            exc: OpenAI SDK exception.

        Returns:
            ProviderError with an appropriate status code and a readable
            message.
        """
        if isinstance(exc, BadRequestError) and any(
            marker in exc.message.lower() for marker in _CONTEXT_LENGTH_ERROR_MARKERS
        ):
            return ProviderError(
                message=f"prompt is too long: {exc.message} ({_PROMPT_TOO_LONG_TOKEN})",
                status_code=400,
                error_type="invalid_request_error",
            )
        detail = self.classify_error(str(exc))
        if isinstance(exc, AuthenticationError):
            return ProviderError(message=detail, status_code=401)
        if isinstance(exc, RateLimitError):
            return ProviderError(message=detail, status_code=429)
        if isinstance(exc, BadRequestError):
            return ProviderError(message=detail, status_code=400)
        status_code = getattr(exc, "status_code", 502) or 502
        return ProviderError(message=detail, status_code=status_code)

    def classify_error(self, error_detail: str) -> str:
        """Produce a readable message for common upstream error patterns.

        Args:
            error_detail: upstream error string.

        Returns:
            Human-readable message (or the original, if unrecognized).
        """
        error_str = error_detail.lower()
        if "invalid_api_key" in error_str or "unauthorized" in error_str:
            return f"Invalid API key for provider '{self.name}'."
        if "rate_limit" in error_str or "quota" in error_str:
            return f"Rate limit exceeded for provider '{self.name}'."
        if "model" in error_str and ("not found" in error_str or "does not exist" in error_str):
            return f"Model not found on provider '{self.name}'."
        if "billing" in error_str or "payment" in error_str:
            return f"Billing issue on provider '{self.name}'."
        return error_detail

    def cancel_request(self, request_id: str) -> bool:
        """Cancel an active request by identifier.

        Args:
            request_id: request identifier.

        Returns:
            True if the request was active and marked for cancellation.
        """
        event = self.active_requests.get(request_id)
        if event is not None:
            event.set()
            return True
        return False

    async def handle_messages(
        self,
        raw_body: bytes,
        client_headers: Mapping[str, str],
        client_channel: ClientChannel,
        upstream_model: str | None,
        limits: RouteLimits,
    ) -> ProviderResult:
        """Translate ``/v1/messages`` into OpenAI and return an Anthropic response."""
        try:
            parsed = ClaudeMessagesRequest.model_validate_json(raw_body)
        except ValidationError as exc:
            return _json_result(
                400, anthropic_error_body("invalid_request_error", str(exc))
            )

        request_id = str(uuid.uuid4())
        is_responses = self.cfg.api_flavor == "responses"
        openai_request = self._build_openai_request(parsed, upstream_model, limits)

        if limits.context_window is not None:
            rejected = self._enforce_context_window(openai_request, limits.context_window)
            if rejected is not None:
                return rejected

        if parsed.stream:
            if not is_responses:
                # stream_options is a chat-completions-only parameter (usage
                # is read from it there). The Responses API rejects it as an
                # unknown parameter; usage arrives in the terminal event
                # instead.
                openai_request.setdefault("stream_options", {})
                openai_request["stream_options"]["include_usage"] = True

            # First contact with OpenAI happens BEFORE the stream is handed
            # to the client: transient 401 is retried; exhaustion produces a
            # proper JSON response (the response has not started yet).
            try:
                streaming_completion = await self.create_stream(
                    openai_request, request_id
                )
            except ProviderError as exc:
                if request_id and request_id in self.active_requests:
                    del self.active_requests[request_id]
                # Terminal failure before the stream starts: SDK attempts are
                # exhausted. Without this log entry, the incident is only
                # visible as a client-side failure.
                logger.warning(
                    "upstream error before stream start; returning error response",
                    provider=self.name,
                    request_id=request_id,
                    status_code=exc.status_code,
                    detail=exc.message,
                )
                return _json_result(
                    exc.status_code, anthropic_error_body(exc.error_type, exc.message)
                )

            stream = self.create_chat_completion_stream(
                streaming_completion, request_id
            )
            # Branching instead of picking a function: the Responses
            # converter needs the reasoning cache, which the chat converter
            # does not have.
            converted = (
                convert_responses_streaming_to_claude_with_cancellation(
                    stream,
                    parsed,
                    logger,
                    client_channel,
                    self,
                    request_id,
                    reasoning_cache=self._reasoning_cache,
                )
                if is_responses
                else convert_openai_streaming_to_claude_with_cancellation(
                    stream, parsed, logger, client_channel, self, request_id
                )
            )
            return ProviderResult(
                status_code=200,
                headers={
                    "content-type": "text/event-stream",
                    "Cache-Control": "no-cache",
                    "Connection": "keep-alive",
                },
                body=_encode_sse(converted),
            )

        try:
            openai_response = await self.create_chat_completion(openai_request, request_id)
        except ProviderError as exc:
            logger.warning(
                "upstream error on non-streaming request; returning error response",
                provider=self.name,
                request_id=request_id,
                status_code=exc.status_code,
                detail=exc.message,
            )
            return _json_result(
                exc.status_code, anthropic_error_body(exc.error_type, exc.message)
            )
        claude_response = (
            convert_responses_to_claude_response(
                openai_response, parsed, reasoning_cache=self._reasoning_cache
            )
            if is_responses
            else convert_openai_to_claude_response(openai_response, parsed)
        )
        # An upstream that closes the turn with no text and no tool call
        # (DeepSeek on a chat array ending in a system message) is otherwise
        # indistinguishable from a normal end_turn in the log. Text that is
        # only whitespace counts as none: the client sees a blank turn.
        if not any(
            block["type"] == Constants.CONTENT_TOOL_USE
            or (block["type"] == Constants.CONTENT_TEXT and block["text"].strip())
            for block in claude_response["content"]
        ):
            logger.warning(
                "empty_completion",
                provider=self.name,
                model=parsed.model,
                upstream_model=openai_request["model"],
                stream=False,
                finish_reason=(
                    openai_response.get("status")
                    if is_responses
                    else openai_response["choices"][0].get("finish_reason")
                ),
            )
        return _json_result(200, claude_response)

    def _build_openai_request(
        self,
        parsed: ClaudeMessagesRequest,
        upstream_model: str | None,
        limits: RouteLimits,
    ) -> dict[str, Any]:
        """Translate a parsed Claude request into the upstream wire body.

        Shared by ``handle_messages`` and ``count_tokens`` so the token
        estimate is computed on exactly the payload that would be sent:
        tools capped to ``cfg.tools_max``, converted for ``cfg.api_flavor``,
        parameter names adapted by ``apply_param_compat``, and an explicit
        ``stream`` bool.

        Args:
            parsed: validated Anthropic ``/v1/messages`` request; its
                ``tools`` list is replaced in place when capped.
            upstream_model: upstream model name from the routing rule, or
                ``None`` to keep ``parsed.model``.
            limits: the route's effective token limits.

        Returns:
            OpenAI request dict (Chat Completions or Responses).
        """
        # Tools array truncated to the openai upstream limit (128 for
        # OpenAI). Applied BEFORE conversion: works with ClaudeTool models
        # (the .name field); the logic does not touch the passthrough
        # provider (byte-for-byte forwarding).
        if self.cfg.tools_max > 0 and parsed.tools is not None:
            parsed.tools = cap_tools(parsed.tools, self.cfg.tools_max, self.name)

        # The schema validator guarantees that openai-translate sets
        # max_tokens_limit, and a rule override is an int too, so the
        # effective value is never None. The cast is warranted:
        # OpenAITranslateProvider is only constructed for openai-translate.
        max_tokens_limit = cast(int, limits.max_tokens_limit)
        if self.cfg.api_flavor == "responses":
            openai_request = convert_claude_to_responses(
                parsed,
                upstream_model if upstream_model is not None else parsed.model,
                max_tokens_limit=max_tokens_limit,
                reasoning_effort_fallback=self.cfg.reasoning_effort,
                reasoning_cache=self._reasoning_cache,
            )
        else:
            openai_request = convert_claude_to_openai(
                parsed, upstream_model, max_tokens_limit=max_tokens_limit
            )
        apply_param_compat(openai_request, self.cfg)

        # Always an explicit bool: some OpenAI-compatible gateways answer 400
        # to a body whose ``stream`` is missing or ``null``, and the SDK
        # serializes a ``None`` kwarg as ``null`` instead of dropping it.
        openai_request["stream"] = bool(parsed.stream)
        return openai_request

    def _enforce_context_window(
        self, openai_request: dict[str, Any], context_window: int
    ) -> ProviderResult | None:
        """Reject or clamp the request so prompt plus completion fits the window.

        Runs on the converted wire body before any upstream call, for the
        streaming and non-streaming paths alike. The budget is the window
        minus ``CONTEXT_WINDOW_RESERVE_TOKENS``; a prompt that leaves less
        than ``MIN_USEFUL_COMPLETION_TOKENS`` of it is rejected with the
        Anthropic-shaped ``invalid_request_error`` carrying the stable
        ``capability_rejected: prompt_too_long`` token (Claude Code then
        retries with a smaller ``max_tokens`` and compacts when nothing
        fits); otherwise the completion budget is lowered to what remains.
        The floor is the USEFUL one, not the converters'
        ``MIN_COMPLETION_TOKENS``: a reasoning upstream handed the thousand
        tokens a nearly full window leaves burns them on reasoning and
        returns empty content with ``stop_reason: max_tokens``, which the
        client cannot act on -- unlike ``prompt_too_long``, which makes it
        compact. This is also the only protection for upstreams that answer
        an overflow with a retried 500 instead of a context-length 400
        (MiniMax-M3).

        Args:
            openai_request: converted wire body; its token-limit key is
                lowered in place when clamped.
            context_window: the route's effective ``context_window`` (the
                rule's override, or the provider's own value).

        Returns:
            The 400 ProviderResult for the client, or ``None`` when the
            request may proceed.
        """
        budget = context_window - CONTEXT_WINDOW_RESERVE_TOKENS
        estimate = estimate_openai_request_tokens(openai_request)
        model = openai_request.get("model")
        if estimate + MIN_USEFUL_COMPLETION_TOKENS > budget:
            logger.warning(
                "context_window_reject",
                provider=self.name,
                model=model,
                estimate=estimate,
                context_window=context_window,
            )
            message = (
                f"prompt is too long: {estimate} tokens > "
                f"{budget - MIN_USEFUL_COMPLETION_TOKENS} maximum "
                f"({_PROMPT_TOO_LONG_TOKEN})"
            )
            return _json_result(400, anthropic_error_body("invalid_request_error", message))

        # The Responses converter writes max_output_tokens directly, while
        # apply_param_compat renames the chat converter's max_tokens to
        # cfg.token_param -- so the key differs per flavor.
        token_key = (
            "max_output_tokens" if self.cfg.api_flavor == "responses" else self.cfg.token_param
        )
        requested = openai_request[token_key]
        clamped = min(requested, budget - estimate)
        if clamped != requested:
            openai_request[token_key] = clamped
            logger.info(
                "context_window_clamp",
                provider=self.name,
                model=model,
                estimate=estimate,
                requested=requested,
                clamped=clamped,
            )
        return None

    async def count_tokens(
        self,
        raw_body: bytes,
        client_headers: Mapping[str, str],
        upstream_model: str | None,
        limits: RouteLimits,
    ) -> ProviderResult:
        """Estimate the input token count locally (the upstream has no count_tokens).

        The count request is wrapped into a ``ClaudeMessagesRequest`` with
        the minimum completion budget and pushed through the same builder
        as ``handle_messages``, so the number reflects the converted wire
        payload (capped tools, tool definitions, tool-call arguments)
        rather than the raw Anthropic text.
        """
        try:
            parsed = ClaudeTokenCountRequest.model_validate_json(raw_body)
        except ValidationError as exc:
            return _json_result(
                400, anthropic_error_body("invalid_request_error", str(exc))
            )
        messages_request = ClaudeMessagesRequest(
            max_tokens=MIN_COMPLETION_TOKENS, **parsed.model_dump()
        )
        openai_request = self._build_openai_request(messages_request, upstream_model, limits)
        return _json_result(200, {"input_tokens": estimate_openai_request_tokens(openai_request)})

    async def aclose(self) -> None:
        """Close the OpenAI and httpx clients."""
        await self._client.close()
        await self._http_client.aclose()
