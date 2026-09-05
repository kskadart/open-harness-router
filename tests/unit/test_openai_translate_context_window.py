"""Tests for context-window awareness of the openai-translate provider.

Covers the pre-flight guard in ``handle_messages`` (reject when the prompt
alone overflows the window, clamp the completion budget otherwise) on the
non-streaming AND the streaming path, the estimator-backed ``count_tokens``
with its silent tools cap, and the remap of upstream context-length 400s to
the Anthropic-shaped ``invalid_request_error`` carrying the stable
``capability_rejected: prompt_too_long`` token. Wire bodies are captured
with pytest-httpx on top of a real ``AsyncOpenAI`` (same pattern as
``test_openai_translate_stream_flag``).

Log events are asserted through a recorder substituted for the provider
module's logger rather than ``structlog.testing.capture_logs``: once an
earlier test has run ``setup_logging()`` (``cache_logger_on_first_use``),
the module-level lazy logger proxy keeps its cached processor chain and a
later ``capture_logs`` sees nothing, so the assertions would depend on
test order.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest
from openai import BadRequestError, RateLimitError
from pydantic import SecretStr
from pytest_httpx import HTTPXMock

import providers.openai_translate as openai_translate_module
from const import (
    CONTEXT_WINDOW_RESERVE_TOKENS,
    MIN_COMPLETION_TOKENS,
    MIN_USEFUL_COMPLETION_TOKENS,
)
from conversion.request_converter import convert_claude_to_openai
from errors import ProviderError
from models.claude import ClaudeMessagesRequest
from providers.base import ProviderResult
from providers.openai_translate import OpenAITranslateProvider
from routing.schema import (
    ApiFlavor,
    MatchRule,
    ProviderCfg,
    RouteLimits,
    RoutingRule,
    TokenParam,
)
from services.token_estimator import estimate_openai_request_tokens
from settings import UpstreamSettings

_BASE_URL = "https://gateway.example/v1"
_MODEL = "minimax-m3"
# The numbers are shaped around the guard's floor
# (``MIN_USEFUL_COMPLETION_TOKENS``): the default prompt leaves more than the
# floor but less than the completion cap (so it is clamped), and the
# oversized prompt alone overflows the whole budget.
_MAX_TOKENS_LIMIT = 16384
_CONTEXT_WINDOW = 32768
_PROMPT_CHARS = 60000
_OVERSIZED_PROMPT_CHARS = 120000
_PROMPT_TOO_LONG_TOKEN = "capability_rejected: prompt_too_long"
_ENDPOINT_PATH: dict[ApiFlavor, str] = {
    "chat": "/chat/completions",
    "responses": "/responses",
}
_TOKEN_KEY: dict[ApiFlavor, str] = {
    "chat": "max_tokens",
    "responses": "max_output_tokens",
}
_CHAT_COMPLETION_BODY: dict[str, Any] = {
    "id": "chatcmpl-test",
    "object": "chat.completion",
    "created": 0,
    "model": _MODEL,
    "choices": [
        {
            "index": 0,
            "message": {"role": "assistant", "content": "Hi"},
            "finish_reason": "stop",
        }
    ],
    "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
}
_RESPONSES_BODY: dict[str, Any] = {
    "id": "resp_test",
    "object": "response",
    "created_at": 0,
    "model": _MODEL,
    "status": "completed",
    "output": [
        {
            "type": "message",
            "id": "msg_test",
            "role": "assistant",
            "status": "completed",
            "content": [{"type": "output_text", "text": "Hi", "annotations": []}],
        }
    ],
    "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
}
_UPSTREAM_BODY: dict[ApiFlavor, dict[str, Any]] = {
    "chat": _CHAT_COMPLETION_BODY,
    "responses": _RESPONSES_BODY,
}
_TOOLS: list[dict[str, Any]] = [
    {
        "name": "read_file",
        "description": "Read a UTF-8 text file from the workspace and return its contents.",
        "input_schema": {
            "type": "object",
            "properties": {"path": {"type": "string", "description": "Absolute path"}},
            "required": ["path"],
        },
    },
    {
        "name": "run_shell",
        "description": "Execute a shell command and return stdout, stderr and the exit code.",
        "input_schema": {
            "type": "object",
            "properties": {"command": {"type": "string"}},
            "required": ["command"],
        },
    },
]


class _ConnectedChannel:
    """Client channel that never disconnects."""

    async def is_disconnected(self) -> bool:
        """Report the client as still connected."""
        return False


class _LogRecorder:
    """Stand-in for the provider module logger that records structlog-style events."""

    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def __getattr__(self, level: str) -> Callable[..., None]:
        def record(event: str, **fields: Any) -> None:
            self.events.append({"event": event, "level": level, **fields})

        return record

    def named(self, event: str) -> list[dict[str, Any]]:
        """All recorded events with the given name."""
        return [entry for entry in self.events if entry["event"] == event]


@pytest.fixture
def provider_log(monkeypatch: pytest.MonkeyPatch) -> _LogRecorder:
    """Replace the provider module logger with a recorder for the test's duration."""
    recorder = _LogRecorder()
    monkeypatch.setattr(openai_translate_module, "logger", recorder)
    return recorder


def _provider(
    api_flavor: ApiFlavor = "chat",
    context_window: int | None = None,
    token_param: TokenParam = "max_tokens",
    tools_max: int = 0,
) -> OpenAITranslateProvider:
    """Create a provider on a real AsyncOpenAI so captured bodies are what the SDK sends."""
    cfg = ProviderCfg(
        type="openai-translate",
        base_url=_BASE_URL,
        api_key_env="GATEWAY_API_KEY",
        api_flavor=api_flavor,
        token_param=token_param,
        tools_max=tools_max,
        max_tokens_limit=_MAX_TOKENS_LIMIT,
        context_window=context_window,
    )
    return OpenAITranslateProvider(
        name="gateway",
        cfg=cfg,
        api_key=SecretStr("test-gateway-key"),
        ca_bundle_path=None,
        upstream=UpstreamSettings(),
    )


def _rule(
    max_tokens_limit: int | None = None, context_window: int | None = None
) -> RoutingRule:
    """A routing rule carrying per-model limit overrides for this provider."""
    return RoutingRule(
        match=MatchRule(type="exact", value=_MODEL),
        provider="gateway",
        max_tokens_limit=max_tokens_limit,
        context_window=context_window,
    )


def _claude_body(**overrides: Any) -> bytes:
    """A minimal Anthropic request body with overridable fields."""
    payload: dict[str, Any] = {
        "model": "claude-sonnet-4",
        "max_tokens": _MAX_TOKENS_LIMIT,
        "messages": [{"role": "user", "content": "x" * _PROMPT_CHARS}],
    }
    payload.update(overrides)
    return json.dumps(payload).encode()


def _sdk_error_body(message: str) -> dict[str, Any]:
    """Upstream JSON error body in OpenAI format."""
    return {"error": {"message": message, "type": "invalid_request_error", "code": None}}


def _bad_request(message: str) -> BadRequestError:
    """A BadRequestError as the SDK raises it for an upstream 400."""
    response = httpx.Response(
        400, request=httpx.Request("POST", "https://x/v1/chat/completions")
    )
    return BadRequestError(message, response=response, body=None)


async def _handle(
    provider: OpenAITranslateProvider,
    claude_body: bytes,
    rule: RoutingRule | None = None,
) -> ProviderResult:
    """Run ``handle_messages`` under the route's effective limits and close the provider.

    ``rule`` is resolved against the provider exactly as
    ``ProviderRegistry.resolve`` does, so a test that passes none gets the
    provider's own numbers.
    """
    try:
        return await provider.handle_messages(
            claude_body, {}, _ConnectedChannel(), _MODEL, RouteLimits.resolve(provider.cfg, rule)
        )
    finally:
        await provider.aclose()


async def _handle_stream(
    provider: OpenAITranslateProvider, claude_body: bytes
) -> tuple[ProviderResult, list[bytes]]:
    """Run a streaming ``handle_messages`` and drain the body before closing the provider.

    Returns the result together with the frames the client would receive --
    empty for a pre-flight refusal, which answers with a JSON body instead
    of a stream.
    """
    try:
        result = await provider.handle_messages(
            claude_body,
            {},
            _ConnectedChannel(),
            _MODEL,
            RouteLimits.resolve(provider.cfg, None),
        )
        if isinstance(result.body, bytes):
            return result, []
        return result, [chunk async for chunk in result.body]
    finally:
        await provider.aclose()


def _json_body(result: ProviderResult) -> dict[str, Any]:
    """Decode a non-streaming ProviderResult body."""
    assert isinstance(result.body, bytes)
    return json.loads(result.body)


def _sent_json(httpx_mock: HTTPXMock) -> dict[str, Any]:
    """The JSON body the SDK put on the wire."""
    upstream_request = httpx_mock.get_request()
    assert upstream_request is not None
    return json.loads(upstream_request.content)


@pytest.mark.parametrize("api_flavor", ["chat", "responses"])
async def test_handle_messages_prompt_over_budget_rejects_without_upstream_call(
    httpx_mock: HTTPXMock, provider_log: _LogRecorder, api_flavor: ApiFlavor
) -> None:
    """A prompt that alone exceeds the prompt budget is rejected locally, with no upstream call."""
    provider = _provider(api_flavor, context_window=_CONTEXT_WINDOW)
    oversized = _claude_body(
        messages=[{"role": "user", "content": "x" * _OVERSIZED_PROMPT_CHARS}]
    )
    prompt_budget = (
        _CONTEXT_WINDOW - CONTEXT_WINDOW_RESERVE_TOKENS - MIN_USEFUL_COMPLETION_TOKENS
    )

    result = await _handle(provider, oversized)

    assert result.status_code == 400
    assert httpx_mock.get_request() is None
    reject_events = provider_log.named("context_window_reject")
    assert len(reject_events) == 1
    assert reject_events[0]["provider"] == "gateway"
    assert reject_events[0]["model"] == _MODEL
    assert reject_events[0]["context_window"] == _CONTEXT_WINDOW
    estimate = reject_events[0]["estimate"]
    assert estimate > prompt_budget
    body = _json_body(result)
    assert body["type"] == "error"
    assert body["error"]["type"] == "invalid_request_error"
    assert body["error"]["message"] == (
        f"prompt is too long: {estimate} tokens > {prompt_budget} maximum "
        f"({_PROMPT_TOO_LONG_TOKEN})"
    )


async def test_handle_messages_chat_clamps_max_tokens_to_remaining_budget(
    httpx_mock: HTTPXMock, provider_log: _LogRecorder
) -> None:
    """Chat flavor: ``max_tokens`` on the wire becomes ``context_window - reserve - estimate``."""
    httpx_mock.add_response(url=_BASE_URL + _ENDPOINT_PATH["chat"], json=_CHAT_COMPLETION_BODY)
    provider = _provider("chat", context_window=_CONTEXT_WINDOW)

    result = await _handle(provider, _claude_body())

    assert result.status_code == 200
    sent = _sent_json(httpx_mock)
    estimate = estimate_openai_request_tokens(sent)
    expected = _CONTEXT_WINDOW - CONTEXT_WINDOW_RESERVE_TOKENS - estimate
    assert expected < _MAX_TOKENS_LIMIT
    assert sent["max_tokens"] == expected
    clamp_events = provider_log.named("context_window_clamp")
    assert len(clamp_events) == 1
    assert clamp_events[0]["provider"] == "gateway"
    assert clamp_events[0]["model"] == _MODEL
    assert clamp_events[0]["estimate"] == estimate
    assert clamp_events[0]["requested"] == _MAX_TOKENS_LIMIT
    assert clamp_events[0]["clamped"] == expected


async def test_handle_messages_responses_clamps_max_output_tokens_only(
    httpx_mock: HTTPXMock,
) -> None:
    """Responses flavor: ``max_output_tokens`` is clamped and no stray ``max_tokens`` appears."""
    httpx_mock.add_response(url=_BASE_URL + _ENDPOINT_PATH["responses"], json=_RESPONSES_BODY)
    provider = _provider("responses", context_window=_CONTEXT_WINDOW)

    result = await _handle(provider, _claude_body())

    assert result.status_code == 200
    sent = _sent_json(httpx_mock)
    estimate = estimate_openai_request_tokens(sent)
    expected = _CONTEXT_WINDOW - CONTEXT_WINDOW_RESERVE_TOKENS - estimate
    assert expected < _MAX_TOKENS_LIMIT
    assert sent["max_output_tokens"] == expected
    assert "max_tokens" not in sent


@pytest.mark.parametrize("api_flavor", ["chat", "responses"])
async def test_handle_messages_without_context_window_leaves_token_limit_unchanged(
    httpx_mock: HTTPXMock, provider_log: _LogRecorder, api_flavor: ApiFlavor
) -> None:
    """No ``context_window`` -> no pre-flight: the wire body carries the requested value."""
    httpx_mock.add_response(
        url=_BASE_URL + _ENDPOINT_PATH[api_flavor], json=_UPSTREAM_BODY[api_flavor]
    )
    provider = _provider(api_flavor)

    result = await _handle(provider, _claude_body())

    assert result.status_code == 200
    assert _sent_json(httpx_mock)[_TOKEN_KEY[api_flavor]] == _MAX_TOKENS_LIMIT
    assert provider_log.named("context_window_clamp") == []
    assert provider_log.named("context_window_reject") == []


async def test_handle_messages_below_budget_with_context_window_keeps_requested_value(
    httpx_mock: HTTPXMock, provider_log: _LogRecorder
) -> None:
    """A small prompt within the window is neither clamped nor logged."""
    httpx_mock.add_response(url=_BASE_URL + _ENDPOINT_PATH["chat"], json=_CHAT_COMPLETION_BODY)
    provider = _provider("chat", context_window=_CONTEXT_WINDOW)
    small = _claude_body(messages=[{"role": "user", "content": "Hello"}])

    result = await _handle(provider, small)

    assert result.status_code == 200
    assert _sent_json(httpx_mock)["max_tokens"] == _MAX_TOKENS_LIMIT
    assert provider_log.named("context_window_clamp") == []


async def test_count_tokens_returns_estimator_value_for_request_with_tools() -> None:
    """``count_tokens`` estimates the converted wire payload, tools included."""
    count_body: dict[str, Any] = {
        "model": "claude-sonnet-4",
        "system": "You are a careful assistant.",
        "messages": [{"role": "user", "content": "List the files in src."}],
        "tools": _TOOLS,
    }
    expected_request = convert_claude_to_openai(
        ClaudeMessagesRequest(max_tokens=100, **count_body),
        _MODEL,
        max_tokens_limit=_MAX_TOKENS_LIMIT,
    )
    expected = estimate_openai_request_tokens(expected_request)
    provider = _provider("chat")
    try:
        limits = RouteLimits.resolve(provider.cfg, None)
        with_tools = await provider.count_tokens(
            json.dumps(count_body).encode(), {}, _MODEL, limits
        )
        without_tools = await provider.count_tokens(
            json.dumps({**count_body, "tools": []}).encode(), {}, _MODEL, limits
        )
    finally:
        await provider.aclose()

    assert with_tools.status_code == 200
    assert _json_body(with_tools) == {"input_tokens": expected}
    assert _json_body(without_tools)["input_tokens"] < expected


@pytest.mark.parametrize(
    "message",
    [
        "Error code: 400 - {'error': {'message': \"This model's maximum context length is "
        "8192 tokens. However, you requested 9000 tokens\", 'code': 'context_length_exceeded'}}",
        "Error code: 400 - {'error': {'message': 'Prompt is too long for this deployment'}}",
    ],
)
def test_to_provider_error_context_length_bad_request_remaps_to_prompt_too_long(
    message: str,
) -> None:
    """Upstream context-length 400s become invalid_request_error with the stable token."""
    provider = _provider("chat")
    mapped = provider._to_provider_error(_bad_request(message))
    assert mapped.status_code == 400
    assert mapped.error_type == "invalid_request_error"
    assert mapped.message.startswith("prompt is too long: ")
    assert message in mapped.message
    assert _PROMPT_TOO_LONG_TOKEN in mapped.message


def test_to_provider_error_unrelated_bad_request_keeps_api_error_type() -> None:
    """A 400 without a context-length marker keeps the generic mapping."""
    provider = _provider("chat")
    message = "Error code: 400 - {'error': {'message': 'Unknown parameter: stream_options'}}"
    mapped = provider._to_provider_error(_bad_request(message))
    assert mapped.status_code == 400
    assert mapped.error_type == "api_error"
    assert mapped.message == message
    assert _PROMPT_TOO_LONG_TOKEN not in mapped.message


def test_to_provider_error_rate_limit_mentioning_tokens_is_not_remapped() -> None:
    """A 429 whose text mentions tokens stays a rate-limit error (no false positive)."""
    provider = _provider("chat")
    message = (
        "Error code: 429 - {'error': {'message': 'Request too large: too many tokens per "
        "minute, maximum context length quota exceeded for this key'}}"
    )
    response = httpx.Response(
        429, request=httpx.Request("POST", "https://x/v1/chat/completions")
    )
    mapped = provider._to_provider_error(RateLimitError(message, response=response, body=None))
    assert mapped.status_code == 429
    assert mapped.error_type == "rate_limit_error"
    assert _PROMPT_TOO_LONG_TOKEN not in mapped.message


@pytest.mark.parametrize(
    ("status_code", "expected_type"),
    [
        (401, "authentication_error"),
        (429, "rate_limit_error"),
        (503, "overloaded_error"),
        (502, "api_error"),
        (400, "api_error"),
    ],
)
def test_provider_error_without_an_explicit_type_derives_it_from_the_status(
    status_code: int, expected_type: str
) -> None:
    """An unclassified failure reports the same type wherever it is rendered.

    The mid-stream error event derives the type from the status; the JSON
    body renders ``error_type``. With a hard-coded ``api_error`` default the
    two disagreed on 401/429/503, and carrying the field into the stream
    event would have downgraded those.
    """
    assert ProviderError("upstream said no", status_code=status_code).error_type == (
        expected_type
    )


def test_provider_error_with_an_explicit_type_keeps_it() -> None:
    """The context-length remap's own type outranks the status-derived default."""
    remapped = ProviderError(
        "prompt is too long", status_code=400, error_type="invalid_request_error"
    )
    assert remapped.error_type == "invalid_request_error"


@pytest.mark.parametrize("api_flavor", ["chat", "responses"])
async def test_handle_messages_upstream_context_length_400_renders_invalid_request_error(
    httpx_mock: HTTPXMock, api_flavor: ApiFlavor
) -> None:
    """The remapped error_type reaches the client body (not the old hard-coded api_error)."""
    httpx_mock.add_response(
        url=_BASE_URL + _ENDPOINT_PATH[api_flavor],
        status_code=400,
        json=_sdk_error_body(
            "This model's maximum context length is 8192 tokens. However, you requested 9000."
        ),
    )
    provider = _provider(api_flavor)

    result = await _handle(provider, _claude_body())

    assert result.status_code == 400
    body = _json_body(result)
    assert body["error"]["type"] == "invalid_request_error"
    assert body["error"]["message"].startswith("prompt is too long: ")
    assert _PROMPT_TOO_LONG_TOKEN in body["error"]["message"]


_OVERRIDE_MAX_TOKENS_LIMIT = 256
_OVERRIDE_WINDOW_BELOW_PROVIDER = 24576
_OVERRIDE_WINDOW_ABOVE_PROVIDER = 65536


async def test_handle_messages_rule_max_tokens_limit_override_caps_the_wire_body_at_the_override(
    httpx_mock: HTTPXMock,
) -> None:
    """The completion cap on the wire comes from the rule, not from the provider block."""
    httpx_mock.add_response(url=_BASE_URL + _ENDPOINT_PATH["chat"], json=_CHAT_COMPLETION_BODY)
    provider = _provider("chat")

    result = await _handle(
        provider, _claude_body(), _rule(max_tokens_limit=_OVERRIDE_MAX_TOKENS_LIMIT)
    )

    assert result.status_code == 200
    assert _sent_json(httpx_mock)["max_tokens"] == _OVERRIDE_MAX_TOKENS_LIMIT
    assert _OVERRIDE_MAX_TOKENS_LIMIT != _MAX_TOKENS_LIMIT


async def test_handle_messages_rule_window_override_clamps_against_the_override_budget(
    httpx_mock: HTTPXMock, provider_log: _LogRecorder
) -> None:
    """A rule window below the provider's tightens the clamp to the rule's own budget."""
    httpx_mock.add_response(url=_BASE_URL + _ENDPOINT_PATH["chat"], json=_CHAT_COMPLETION_BODY)
    provider = _provider("chat", context_window=_CONTEXT_WINDOW)

    result = await _handle(
        provider, _claude_body(), _rule(context_window=_OVERRIDE_WINDOW_BELOW_PROVIDER)
    )

    assert result.status_code == 200
    sent = _sent_json(httpx_mock)
    estimate = estimate_openai_request_tokens(sent)
    expected = _OVERRIDE_WINDOW_BELOW_PROVIDER - CONTEXT_WINDOW_RESERVE_TOKENS - estimate
    assert sent["max_tokens"] == expected
    assert expected < _CONTEXT_WINDOW - CONTEXT_WINDOW_RESERVE_TOKENS - estimate
    assert provider_log.named("context_window_clamp")[0]["clamped"] == expected


async def test_handle_messages_rule_window_override_above_provider_leaves_the_budget_unclamped(
    httpx_mock: HTTPXMock, provider_log: _LogRecorder
) -> None:
    """The DeepSeek case: the same gateway, a wider window on this model only."""
    httpx_mock.add_response(url=_BASE_URL + _ENDPOINT_PATH["chat"], json=_CHAT_COMPLETION_BODY)
    provider = _provider("chat", context_window=_CONTEXT_WINDOW)

    result = await _handle(
        provider, _claude_body(), _rule(context_window=_OVERRIDE_WINDOW_ABOVE_PROVIDER)
    )

    assert result.status_code == 200
    sent = _sent_json(httpx_mock)
    estimate = estimate_openai_request_tokens(sent)
    # The provider's own window would have clamped this request.
    assert _CONTEXT_WINDOW - CONTEXT_WINDOW_RESERVE_TOKENS - estimate < _MAX_TOKENS_LIMIT
    assert sent["max_tokens"] == _MAX_TOKENS_LIMIT
    assert provider_log.named("context_window_clamp") == []


async def test_handle_messages_rule_window_override_reject_quotes_the_override_budget(
    httpx_mock: HTTPXMock, provider_log: _LogRecorder
) -> None:
    """A rule window turns the pre-flight on for a provider that declares none."""
    provider = _provider("chat")
    oversized = _claude_body(
        messages=[{"role": "user", "content": "x" * _OVERSIZED_PROMPT_CHARS}]
    )
    prompt_budget = (
        _CONTEXT_WINDOW - CONTEXT_WINDOW_RESERVE_TOKENS - MIN_USEFUL_COMPLETION_TOKENS
    )

    result = await _handle(provider, oversized, _rule(context_window=_CONTEXT_WINDOW))

    assert result.status_code == 400
    assert httpx_mock.get_request() is None
    reject_events = provider_log.named("context_window_reject")
    assert len(reject_events) == 1
    assert reject_events[0]["context_window"] == _CONTEXT_WINDOW
    estimate = reject_events[0]["estimate"]
    assert _json_body(result)["error"]["message"] == (
        f"prompt is too long: {estimate} tokens > {prompt_budget} maximum "
        f"({_PROMPT_TOO_LONG_TOKEN})"
    )


async def test_handle_messages_rule_without_overrides_keeps_the_provider_limits(
    httpx_mock: HTTPXMock, provider_log: _LogRecorder
) -> None:
    """Regression: a rule that overrides nothing routes exactly as before the feature."""
    httpx_mock.add_response(url=_BASE_URL + _ENDPOINT_PATH["chat"], json=_CHAT_COMPLETION_BODY)
    provider = _provider("chat", context_window=_CONTEXT_WINDOW)

    result = await _handle(provider, _claude_body(), _rule())

    assert result.status_code == 200
    sent = _sent_json(httpx_mock)
    estimate = estimate_openai_request_tokens(sent)
    assert sent["max_tokens"] == _CONTEXT_WINDOW - CONTEXT_WINDOW_RESERVE_TOKENS - estimate
    assert provider_log.named("context_window_clamp")[0]["requested"] == _MAX_TOKENS_LIMIT


async def test_count_tokens_with_a_rule_override_estimates_the_same_converted_prompt() -> None:
    """``count_tokens`` runs the builder under the route's effective limits.

    The estimate covers the prompt only -- the completion budget is not part
    of the wire payload's token cost -- so the number is the one the
    provider's own limits produce; what the override must not do is change
    the payload the estimate is taken over.
    """
    count_body: dict[str, Any] = {
        "model": "claude-sonnet-4",
        "messages": [{"role": "user", "content": "List the files in src."}],
        "tools": _TOOLS,
    }
    expected = estimate_openai_request_tokens(
        convert_claude_to_openai(
            ClaudeMessagesRequest(max_tokens=MIN_COMPLETION_TOKENS, **count_body),
            _MODEL,
            max_tokens_limit=_OVERRIDE_MAX_TOKENS_LIMIT,
        )
    )
    provider = _provider("chat")
    raw_body = json.dumps(count_body).encode()
    try:
        overridden = await provider.count_tokens(
            raw_body,
            {},
            _MODEL,
            RouteLimits.resolve(
                provider.cfg, _rule(max_tokens_limit=_OVERRIDE_MAX_TOKENS_LIMIT)
            ),
        )
        plain = await provider.count_tokens(
            raw_body, {}, _MODEL, RouteLimits.resolve(provider.cfg, None)
        )
    finally:
        await provider.aclose()

    assert _json_body(overridden) == {"input_tokens": expected}
    assert _json_body(plain) == _json_body(overridden)


# Leaves ~2250 tokens of the budget: above the converter's own floor, far
# below what a reasoning upstream needs before its first visible token.
_NEARLY_FULL_PROMPT_CHARS = 105000


@pytest.mark.parametrize("api_flavor", ["chat", "responses"])
async def test_handle_messages_remaining_budget_below_the_useful_floor_is_rejected(
    httpx_mock: HTTPXMock, provider_log: _LogRecorder, api_flavor: ApiFlavor
) -> None:
    """A budget too small to be worth sending is a prompt_too_long, not a wasted call.

    A reasoning upstream spends a thousand-token budget on reasoning and
    answers with empty content and ``stop_reason: max_tokens``; the client
    only compacts when it is told the prompt is too long.
    """
    provider = _provider(api_flavor, context_window=_CONTEXT_WINDOW)
    nearly_full = _claude_body(
        messages=[{"role": "user", "content": "x" * _NEARLY_FULL_PROMPT_CHARS}]
    )

    result = await _handle(provider, nearly_full)

    assert result.status_code == 400
    assert httpx_mock.get_request() is None
    estimate = provider_log.named("context_window_reject")[0]["estimate"]
    remaining = _CONTEXT_WINDOW - CONTEXT_WINDOW_RESERVE_TOKENS - estimate
    assert MIN_COMPLETION_TOKENS < remaining < MIN_USEFUL_COMPLETION_TOKENS
    assert _json_body(result)["error"]["message"] == (
        f"prompt is too long: {estimate} tokens > "
        f"{_CONTEXT_WINDOW - CONTEXT_WINDOW_RESERVE_TOKENS - MIN_USEFUL_COMPLETION_TOKENS} "
        f"maximum ({_PROMPT_TOO_LONG_TOKEN})"
    )


# create(stream=True) only needs the connection to open and the converter to
# reach a terminal event; the payload itself is irrelevant to the guard.
_STREAM_BODY: dict[ApiFlavor, str] = {
    "chat": (
        'data: {"id":"chatcmpl-test","choices":[{"index":0,'
        '"delta":{"role":"assistant","content":"Hi"},"finish_reason":"stop"}]}\n\n'
        "data: [DONE]\n\n"
    ),
    "responses": (
        'event: response.created\ndata: {"type":"response.created",'
        '"response":{"id":"resp_test"}}\n\n'
        'event: response.completed\ndata: {"type":"response.completed",'
        '"response":{"id":"resp_test","status":"completed","output":[],'
        '"usage":{"input_tokens":1,"output_tokens":1}}}\n\n'
    ),
}


@pytest.mark.parametrize("api_flavor", ["chat", "responses"])
async def test_handle_messages_streaming_prompt_over_budget_is_refused_with_json_not_sse(
    httpx_mock: HTTPXMock, provider_log: _LogRecorder, api_flavor: ApiFlavor
) -> None:
    """A streaming client asking too much gets the JSON 400, never a started stream.

    The pre-flight runs before the response is handed over, so the client
    can still read a body and act on ``prompt_too_long``; an error smuggled
    into an SSE frame after a 200 would not make Claude Code compact.
    """
    provider = _provider(api_flavor, context_window=_CONTEXT_WINDOW)
    oversized = _claude_body(
        stream=True, messages=[{"role": "user", "content": "x" * _OVERSIZED_PROMPT_CHARS}]
    )

    result, frames = await _handle_stream(provider, oversized)

    assert result.status_code == 400
    assert result.headers["content-type"] == "application/json"
    assert frames == []
    assert httpx_mock.get_request() is None
    assert len(provider_log.named("context_window_reject")) == 1
    body = _json_body(result)
    assert body["error"]["type"] == "invalid_request_error"
    assert _PROMPT_TOO_LONG_TOKEN in body["error"]["message"]


@pytest.mark.parametrize("api_flavor", ["chat", "responses"])
async def test_handle_messages_streaming_clamps_the_completion_budget_and_still_streams(
    httpx_mock: HTTPXMock, provider_log: _LogRecorder, api_flavor: ApiFlavor
) -> None:
    """A streaming request that fits is clamped on the wire and keeps its SSE response."""
    httpx_mock.add_response(
        url=_BASE_URL + _ENDPOINT_PATH[api_flavor],
        headers={"content-type": "text/event-stream"},
        text=_STREAM_BODY[api_flavor],
    )
    provider = _provider(api_flavor, context_window=_CONTEXT_WINDOW)

    result, frames = await _handle_stream(provider, _claude_body(stream=True))

    assert result.status_code == 200
    assert result.headers["content-type"] == "text/event-stream"
    assert frames
    sent = _sent_json(httpx_mock)
    assert sent["stream"] is True
    estimate = estimate_openai_request_tokens(sent)
    expected = _CONTEXT_WINDOW - CONTEXT_WINDOW_RESERVE_TOKENS - estimate
    assert expected < _MAX_TOKENS_LIMIT
    assert sent[_TOKEN_KEY[api_flavor]] == expected
    assert provider_log.named("context_window_clamp")[0]["clamped"] == expected


async def test_handle_messages_clamps_the_renamed_token_param_leaving_no_stale_max_tokens(
    httpx_mock: HTTPXMock,
) -> None:
    """A provider renaming the limit is clamped under its own key, not ``max_tokens``.

    The clamp writes back into the CONVERTED body, where
    ``apply_param_compat`` has already renamed the key; writing
    ``max_tokens`` there would send the upstream both a stale unclamped
    value and an unknown parameter.
    """
    httpx_mock.add_response(url=_BASE_URL + _ENDPOINT_PATH["chat"], json=_CHAT_COMPLETION_BODY)
    provider = _provider(
        "chat", context_window=_CONTEXT_WINDOW, token_param="max_completion_tokens"
    )

    result = await _handle(provider, _claude_body())

    assert result.status_code == 200
    sent = _sent_json(httpx_mock)
    estimate = estimate_openai_request_tokens(sent)
    expected = _CONTEXT_WINDOW - CONTEXT_WINDOW_RESERVE_TOKENS - estimate
    assert expected < _MAX_TOKENS_LIMIT
    assert sent["max_completion_tokens"] == expected
    assert "max_tokens" not in sent


@pytest.mark.parametrize("api_flavor", ["chat", "responses"])
async def test_handle_messages_streaming_upstream_context_length_400_renders_invalid_request_error(
    httpx_mock: HTTPXMock, api_flavor: ApiFlavor
) -> None:
    """The pre-stream path keeps the remapped type: a 400 before the stream is JSON, not SSE."""
    httpx_mock.add_response(
        url=_BASE_URL + _ENDPOINT_PATH[api_flavor],
        status_code=400,
        json=_sdk_error_body(
            "This model's maximum context length is 8192 tokens. However, you requested 9000."
        ),
    )
    provider = _provider(api_flavor)

    result, frames = await _handle_stream(provider, _claude_body(stream=True))

    assert result.status_code == 400
    assert result.headers["content-type"] == "application/json"
    assert frames == []
    body = _json_body(result)
    assert body["error"]["type"] == "invalid_request_error"
    assert _PROMPT_TOO_LONG_TOKEN in body["error"]["message"]


_TOOLS_CAP_WARNING = "tools array capped for openai provider"


async def test_count_tokens_applies_the_tools_cap_without_logging_it(
    provider_log: _LogRecorder,
) -> None:
    """Counting caps the tools array silently: nothing goes upstream, so nothing is dropped.

    The cap has to run -- the count must describe the payload the request
    would send -- but Claude Code issues a count per turn, so the warning
    would repeat on every one of them and bury the entry that reports a
    real truncation.
    """
    count_body: dict[str, Any] = {
        "model": "claude-sonnet-4",
        "messages": [{"role": "user", "content": "List the files in src."}],
        "tools": _TOOLS,
    }
    provider = _provider("chat", tools_max=1)
    try:
        limits = RouteLimits.resolve(provider.cfg, None)
        capped = await provider.count_tokens(
            json.dumps(count_body).encode(), {}, _MODEL, limits
        )
        single_tool = await provider.count_tokens(
            json.dumps({**count_body, "tools": _TOOLS[:1]}).encode(), {}, _MODEL, limits
        )
    finally:
        await provider.aclose()

    assert provider_log.named(_TOOLS_CAP_WARNING) == []
    # The cap was applied, not skipped: the count matches the payload that
    # keeps only the first tool.
    assert _json_body(capped) == _json_body(single_tool)


async def test_handle_messages_still_logs_the_tools_cap_warning(
    httpx_mock: HTTPXMock, provider_log: _LogRecorder
) -> None:
    """Silencing the counting path must not silence a real request dropping tools."""
    httpx_mock.add_response(url=_BASE_URL + _ENDPOINT_PATH["chat"], json=_CHAT_COMPLETION_BODY)
    provider = _provider("chat", tools_max=1)

    result = await _handle(
        provider,
        _claude_body(messages=[{"role": "user", "content": "hi"}], tools=_TOOLS),
    )

    assert result.status_code == 200
    assert len(provider_log.named(_TOOLS_CAP_WARNING)) == 1
    assert provider_log.named(_TOOLS_CAP_WARNING)[0]["dropped_count"] == 1
