"""Integration tests for the ChatGPT subscription OAuth Messages route.

Covers the review-requested cases: non-streaming aggregation of terminal
Responses events (completed / incomplete / failed), store/stream/request
scoping against ordinary API-key Responses providers, credential header
injection, active_requests cleanup, and the two-turn
``reasoning.encrypted_content`` replay.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import pytest_asyncio
import yaml
from pytest_httpx import HTTPXMock

from auth.chatgpt_oauth import ChatGPTTokenStore, _b64url
from main import create_app
from routing.config_loader import load_routing_config
from routing.registry import ProviderRegistry
from settings import Settings

SUBSCRIPTION_URL = "https://chatgpt.com/backend-api/codex/responses"
ORDINARY_URL = "https://api.openai.example/v1/responses"


def _sse(events: list[dict[str, Any]]) -> bytes:
    frames = [f"data: {json.dumps(e)}" for e in events]
    return ("\n\n".join(frames) + "\n\n").encode()


def _completed_event(text: str = "Hello from the subscription") -> dict[str, Any]:
    return {
        "type": "response.completed",
        "response": {
            "id": "resp_1",
            "object": "response",
            "status": "completed",
            "model": "gpt-test",
            "output": [
                {
                    "type": "message",
                    "id": "msg_1",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": text}],
                }
            ],
            "usage": {
                "input_tokens": 5,
                "output_tokens": 4,
                "input_tokens_details": {"cached_tokens": 0},
                "output_tokens_details": {"reasoning_tokens": 0},
            },
        },
    }


def _incomplete_event() -> dict[str, Any]:
    return {
        "type": "response.incomplete",
        "response": {
            "id": "resp_2",
            "object": "response",
            "status": "incomplete",
            "incomplete_details": {"reason": "max_output_tokens"},
            "model": "gpt-test",
            "output": [
                {
                    "type": "message",
                    "id": "msg_2",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "partial answ"}],
                }
            ],
            "usage": {
                "input_tokens": 5,
                "output_tokens": 8,
                "input_tokens_details": {"cached_tokens": 0},
                "output_tokens_details": {"reasoning_tokens": 3},
            },
        },
    }


def _failed_event() -> dict[str, Any]:
    return {
        "type": "response.failed",
        "response": {
            "id": "resp_3",
            "object": "response",
            "status": "failed",
            "error": {"code": "err", "message": "upstream exploded"},
            "output": [],
        },
    }


def _fake_jwt(account_id: str = "acc-int") -> str:
    def enc(payload: dict[str, Any]) -> str:
        return _b64url(json.dumps(payload).encode())

    return (
        f"{enc({'alg': 'RS256', 'typ': 'JWT'})}."
        f"{enc({'https://api.openai.com/auth': {'chatgpt_account_id': account_id}})}.sig"
    )


def _write_credential(tmp_path: Path, account_id: str = "acc-int") -> Path:
    # The provider resolves state/chatgpt_auth.json relative to the cwd;
    # the fixture chdirs into tmp_path, so that is where it must land.
    store = ChatGPTTokenStore(tmp_path / "state" / "chatgpt_auth.json")
    store.save(
        {
            "access_token": _fake_jwt(account_id),
            "refresh_token": "rt-int",
            "id_token": "",
            "account_id": account_id,
            "email": "",
            "plan_type": "pro",
            "expires_at": time.time() + 3600,
        }
    )
    return store.path


@pytest_asyncio.fixture
async def subscription_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[httpx.AsyncClient]:
    """A router with one OAuth subscription provider and one ordinary one."""
    _write_credential(tmp_path)
    monkeypatch.chdir(tmp_path)
    config = {
        "version": 1,
        "providers": {
            "native": {"type": "passthrough", "base_url": "https://native.example"},
            "chatgpt_subscription": {
                "type": "openai-translate",
                "base_url": "https://chatgpt.com/backend-api/codex",
                "api_flavor": "responses",
                "chatgpt_oauth": True,
                "max_tokens_limit": 128000,
                "context_window": 1048576,
            },
            "ordinary": {
                "type": "openai-translate",
                "base_url": "https://api.openai.example/v1",
                "api_flavor": "responses",
                "api_key_env": "ORDINARY_KEY",
                "max_tokens_limit": 128000,
                "context_window": 1048576,
            },
        },
        "rules": [
            {
                "match": {"type": "exact", "value": "OAI/gpt-test"},
                "provider": "chatgpt_subscription",
                "upstream_model": "gpt-test",
            },
            {
                "match": {"type": "exact", "value": "plain-model"},
                "provider": "ordinary",
                "upstream_model": "gpt-plain",
            },
        ],
        "default": {"provider": "native"},
    }
    path = tmp_path / "routing.yaml"
    path.write_text(yaml.safe_dump(config))
    monkeypatch.setenv("ROUTER_CONFIG_PATH", str(path))
    monkeypatch.setenv("ORDINARY_KEY", "ordinary-key")
    settings = Settings()
    registry = ProviderRegistry.build(load_routing_config(path), settings)
    app = create_app(settings, registry)
    app.state.settings = settings
    app.state.registry = registry
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as client:
        try:
            yield client
        finally:
            await registry.close_all()


async def _post_messages(
    client: httpx.AsyncClient, model: str, messages: list[dict[str, Any]]
) -> httpx.Response:
    return await client.post(
        "/v1/messages",
        headers={"authorization": "Bearer ohr-local", "anthropic-version": "2023-06-01"},
        json={"model": model, "max_tokens": 500, "messages": messages},
    )


async def test_non_streaming_completed_aggregates_full_response(
    subscription_client: httpx.AsyncClient, httpx_mock: HTTPXMock
) -> None:
    httpx_mock.add_response(
        url=SUBSCRIPTION_URL, content=_sse([_completed_event("hi there")])
    )
    response = await _post_messages(
        subscription_client, "OAI/gpt-test", [{"role": "user", "content": "hello"}]
    )
    assert response.status_code == 200
    data = response.json()
    assert [b["text"] for b in data["content"] if b["type"] == "text"] == ["hi there"]
    assert data["stop_reason"] == "end_turn"

    upstream = httpx_mock.get_request()
    assert upstream is not None
    payload = json.loads(upstream.content)
    # Streaming-only backend: stream forced even for a non-streaming client.
    assert payload["stream"] is True
    # store=false scoped to the subscription path only.
    assert payload["store"] is False
    assert "reasoning.encrypted_content" in payload.get("include", [])
    # The subscription credential headers are attached.
    stored = json.loads((Path("state") / "chatgpt_auth.json").read_text())
    assert upstream.headers["authorization"] == f"Bearer {stored['access_token']}"
    assert upstream.headers["chatgpt-account-id"] == "acc-int"
    assert upstream.headers["originator"] == "codex-tui"


async def test_non_streaming_incomplete_keeps_partial_answer(
    subscription_client: httpx.AsyncClient, httpx_mock: HTTPXMock
) -> None:
    httpx_mock.add_response(url=SUBSCRIPTION_URL, content=_sse([_incomplete_event()]))
    response = await _post_messages(
        subscription_client, "OAI/gpt-test", [{"role": "user", "content": "hello"}]
    )
    assert response.status_code == 200
    data = response.json()
    # The partial answer is preserved, not dropped.
    assert [b["text"] for b in data["content"] if b["type"] == "text"] == ["partial answ"]


async def test_non_streaming_failed_maps_upstream_error(
    subscription_client: httpx.AsyncClient, httpx_mock: HTTPXMock
) -> None:
    httpx_mock.add_response(url=SUBSCRIPTION_URL, content=_sse([_failed_event()]))
    response = await _post_messages(
        subscription_client, "OAI/gpt-test", [{"role": "user", "content": "hello"}]
    )
    assert response.status_code == 502
    body = response.json()
    # The upstream failure detail survives the provider error path.
    assert "upstream exploded" in json.dumps(body)


async def test_missing_credential_is_a_per_route_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    httpx_mock: HTTPXMock,
) -> None:
    # No credential file: the route errors clearly, the router still boots.
    monkeypatch.chdir(tmp_path)
    config = {
        "version": 1,
        "providers": {
            "native": {"type": "passthrough", "base_url": "https://native.example"},
            "chatgpt_subscription": {
                "type": "openai-translate",
                "base_url": "https://chatgpt.com/backend-api/codex",
                "api_flavor": "responses",
                "chatgpt_oauth": True,
                "max_tokens_limit": 128000,
                "context_window": 1048576,
            },
        },
        "rules": [
            {
                "match": {"type": "exact", "value": "OAI/gpt-test"},
                "provider": "chatgpt_subscription",
                "upstream_model": "gpt-test",
            }
        ],
        "default": {"provider": "native"},
    }
    path = tmp_path / "routing.yaml"
    path.write_text(yaml.safe_dump(config))
    monkeypatch.setenv("ROUTER_CONFIG_PATH", str(path))
    settings = Settings()
    registry = ProviderRegistry.build(load_routing_config(path), settings)
    app = create_app(settings, registry)
    app.state.settings = settings
    app.state.registry = registry
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as client:
        response = await _post_messages(
            client, "OAI/gpt-test", [{"role": "user", "content": "hello"}]
        )
        assert response.status_code >= 500
        assert "cli.chatgpt_login" in json.dumps(response.json())
        await registry.close_all()


async def test_ordinary_responses_provider_omits_store_and_includes(
    subscription_client: httpx.AsyncClient, httpx_mock: HTTPXMock
) -> None:
    """Ordinary API-key Responses routes keep their base behavior."""
    # The ordinary route is not streaming-only: a non-streaming client
    # request goes out as a plain JSON Responses call.
    httpx_mock.add_response(
        url=ORDINARY_URL,
        json={
            "id": "resp_plain",
            "object": "response",
            "status": "completed",
            "model": "gpt-plain",
            "output": [
                {
                    "type": "message",
                    "id": "msg_plain",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "plain answer"}],
                }
            ],
            "usage": {
                "input_tokens": 5,
                "output_tokens": 2,
                "input_tokens_details": {"cached_tokens": 0},
                "output_tokens_details": {"reasoning_tokens": 0},
            },
        },
    )
    response = await _post_messages(
        subscription_client, "plain-model", [{"role": "user", "content": "hello"}]
    )
    assert response.status_code == 200
    upstream = httpx_mock.get_request()
    assert upstream is not None
    payload = json.loads(upstream.content)
    assert "store" not in payload
    assert "include" not in payload
    # The ordinary route authenticates with its own key, not the OAuth holder.
    assert upstream.headers["authorization"] == "Bearer ordinary-key"
    assert "chatgpt-account-id" not in upstream.headers


async def test_no_request_entry_leaks_after_aggregation(
    subscription_client: httpx.AsyncClient, httpx_mock: HTTPXMock
) -> None:
    httpx_mock.add_response(url=SUBSCRIPTION_URL, content=_sse([_completed_event()]))
    await _post_messages(
        subscription_client, "OAI/gpt-test", [{"role": "user", "content": "hello"}]
    )
    # The provider instance is reachable through the app registry; after a
    # completed aggregation its request table must be empty.
    registry = subscription_client._transport  # noqa: SLF001 - test introspection
    app = getattr(registry, "app", None)
    assert app is not None
    for provider in app.state.registry.providers.values():
        assert getattr(provider, "active_requests", {}) == {}


async def test_two_turn_tool_use_replays_encrypted_reasoning(
    subscription_client: httpx.AsyncClient, httpx_mock: HTTPXMock
) -> None:
    """Turn 1: tool call with encrypted reasoning; turn 2: reasoning replayed."""
    turn1 = {
        "type": "response.completed",
        "response": {
            "id": "resp_t1",
            "object": "response",
            "status": "completed",
            "model": "gpt-test",
            "output": [
                {
                    "type": "reasoning",
                    "id": "rs_1",
                    "summary": [],
                    "encrypted_content": "ENC-REASONING-PAYLOAD",
                },
                {
                    "type": "function_call",
                    "id": "fc_1",
                    "call_id": "call_1",
                    "name": "calc",
                    "arguments": "{\"expr\": \"7*6\"}",
                },
            ],
            "usage": {
                "input_tokens": 5,
                "output_tokens": 6,
                "input_tokens_details": {"cached_tokens": 0},
                "output_tokens_details": {"reasoning_tokens": 2},
            },
        },
    }
    turn2 = _completed_event("42")
    httpx_mock.add_response(url=SUBSCRIPTION_URL, content=_sse([turn1]))
    httpx_mock.add_response(url=SUBSCRIPTION_URL, content=_sse([turn2]))

    tools = [
        {
            "name": "calc",
            "description": "calculator",
            "input_schema": {
                "type": "object",
                "properties": {"expr": {"type": "string"}},
            },
        }
    ]
    first = await subscription_client.post(
        "/v1/messages",
        headers={"authorization": "Bearer ohr-local", "anthropic-version": "2023-06-01"},
        json={
            "model": "OAI/gpt-test",
            "max_tokens": 500,
            "messages": [{"role": "user", "content": "compute 7*6"}],
            "tools": tools,
        },
    )
    assert first.status_code == 200
    calls = [
        b
        for b in first.json()["content"]
        if b["type"] == "tool_use" and b["name"] == "calc"
    ]
    assert calls, first.json()

    second = await subscription_client.post(
        "/v1/messages",
        headers={"authorization": "Bearer ohr-local", "anthropic-version": "2023-06-01"},
        json={
            "model": "OAI/gpt-test",
            "max_tokens": 500,
            "messages": [
                {"role": "user", "content": "compute 7*6"},
                {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": calls[0]["id"],
                            "name": "calc",
                            "input": {"expr": "7*6"},
                        }
                    ],
                },
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": calls[0]["id"],
                            "content": "42",
                        }
                    ],
                },
            ],
            "tools": tools,
        },
    )
    assert second.status_code == 200
    assert [b["text"] for b in second.json()["content"] if b["type"] == "text"] == ["42"]

    requests = httpx_mock.get_requests()
    assert len(requests) == 2
    second_payload = json.loads(requests[1].content)
    # The encrypted reasoning item is replayed before the function call.
    input_items = second_payload["input"]
    reasoning_positions = [
        i for i, item in enumerate(input_items) if item.get("type") == "reasoning"
    ]
    function_positions = [
        i
        for i, item in enumerate(input_items)
        if item.get("type") == "function_call" and item.get("call_id") == calls[0]["id"]
    ]
    assert reasoning_positions, "encrypted reasoning item was not replayed"
    assert function_positions
    assert reasoning_positions[0] < function_positions[0]
    replayed = input_items[reasoning_positions[0]]
    assert replayed.get("encrypted_content") == "ENC-REASONING-PAYLOAD"


async def test_empty_token_limited_response(
    subscription_client: httpx.AsyncClient, httpx_mock: HTTPXMock
) -> None:
    event = _incomplete_event()
    event["response"]["output"] = []
    httpx_mock.add_response(url=SUBSCRIPTION_URL, content=_sse([event]))
    response = await _post_messages(
        subscription_client, "OAI/gpt-test", [{"role": "user", "content": "hi"}]
    )
    assert response.status_code == 200
    assert response.json()["stop_reason"] == "max_tokens"
    assert response.json()["usage"]["output_tokens"] == 8


async def test_flat_stream_error_preserves_message(
    subscription_client: httpx.AsyncClient, httpx_mock: HTTPXMock
) -> None:
    event = {"type": "error", "code": "server_error", "message": "synthetic upstream detail"}
    httpx_mock.add_response(url=SUBSCRIPTION_URL, content=_sse([event]))
    response = await _post_messages(
        subscription_client, "OAI/gpt-test", [{"role": "user", "content": "hi"}]
    )
    assert response.status_code == 502
    assert event["message"] in response.json()["error"]["message"]


@pytest.mark.parametrize("phase", ["opening", "reading"])
async def test_cancellation_during_subscription_aggregation(
    subscription_client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch, phase: str
) -> None:
    provider = subscription_client._transport.app.state.registry.providers["chatgpt_subscription"]
    paused, closed = asyncio.Event(), asyncio.Event()

    async def stall():
        paused.set()
        await asyncio.Event().wait()

    class Stream:
        async def __aiter__(self):
            await stall()
            yield _completed_event()

        async def close(self):
            closed.set()

    async def open_stream(*args):
        if phase == "opening":
            await stall()
        return Stream()

    monkeypatch.setattr(provider, "create_stream", open_stream)
    task = asyncio.create_task(
        _post_messages(subscription_client, "OAI/gpt-test", [{"role": "user", "content": "hi"}])
    )
    try:
        await asyncio.wait_for(paused.wait(), 2)
        assert provider.cancel_request(next(iter(provider.active_requests)))
        response = await asyncio.wait_for(task, 2)
        assert response.status_code == 499
        assert not provider.active_requests
        assert closed.is_set() == (phase == "reading")
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
