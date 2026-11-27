"""Responses clients can use Messages and Chat Completions with isolated credentials."""

from __future__ import annotations

import gzip
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import pytest_asyncio
import yaml
from pytest_httpx import HTTPXMock, IteratorStream

from main import create_app
from routing.config_loader import load_routing_config
from routing.registry import ProviderRegistry
from settings import Settings

MESSAGE = {
    "id": "msg_test",
    "type": "message",
    "role": "assistant",
    "model": "vendor",
    "content": [{"type": "text", "text": "Hello"}],
    "stop_reason": "end_turn",
    "usage": {"input_tokens": 10, "output_tokens": 2, "cache_read_input_tokens": 3},
}


@pytest_asyncio.fixture
async def bridge_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest
) -> AsyncIterator[httpx.AsyncClient]:
    config = {
        "version": 1,
        "providers": {
            "native": {"type": "passthrough", "base_url": "https://native.example"},
            "messages": {
                "type": "passthrough",
                "base_url": "https://messages.example",
                "forward_client_auth": False,
                "api_key_env": "BRIDGE_KEY",
                "auth_header": "x-api-key",
            },
            "chat": {
                "type": "openai-translate",
                "base_url": "https://chat.example/v1",
                "api_key_env": "BRIDGE_KEY",
                "max_tokens_limit": 4096,
                "forward_chat_reasoning_effort": getattr(request, "param", False),
            },
        },
        "rules": [
            {"match": {"type": "exact", "value": "messages-model"}, "provider": "messages"},
            {
                "match": {"type": "exact", "value": "chat-model"},
                "provider": "chat",
                "upstream_model": "vendor-model",
            },
        ],
        "default": {"provider": "native"},
    }
    path = tmp_path / "routing.yaml"
    path.write_text(yaml.safe_dump(config))
    monkeypatch.setenv("ROUTER_CONFIG_PATH", str(path))
    monkeypatch.setenv("BRIDGE_KEY", "own-key")
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


@pytest.mark.parametrize(
    "input_items",
    [
        "Hello",
        [
            {
                "type": "function_call_output",
                "namespace": "codex_tui",
                "name": "send_message_to_thread",
                "output": "Synthetic delivery receipt",
                "internal_chat_message_metadata_passthrough": {"turn_id": "test", "create_time": 0},
            },
            {"type": "message", "role": "user", "content": "Hello"},
        ],
    ],
    ids=["text", "inherited-thread-receipt"],
)
async def test_messages_roundtrip_and_auth_isolation(
    bridge_client: httpx.AsyncClient,
    httpx_mock: HTTPXMock,
    input_items: str | list[dict[str, Any]],
) -> None:
    httpx_mock.add_response(url="https://messages.example/v1/messages", json=MESSAGE)
    response = await bridge_client.post(
        "/v1/responses",
        headers={
            "authorization": "Bearer codex-private",
            "chatgpt-account-id": "private-account",
        },
        json={"model": "messages-model", "instructions": "Be helpful", "input": input_items},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["output"][0]["content"][0]["text"] == "Hello"
    assert data["usage"]["input_tokens"] == 13
    assert data["status"] == "completed"
    request = httpx_mock.get_request()
    assert request is not None
    assert request.headers["x-api-key"] == "own-key"
    assert "authorization" not in request.headers
    assert "chatgpt-account-id" not in request.headers
    assert request.headers["content-type"] == "application/json"
    payload = json.loads(request.content)
    assert payload["system"] == "Be helpful"
    assert payload["messages"] == [{"role": "user", "content": [{"type": "text", "text": "Hello"}]}]
    catalog = (await bridge_client.get("/codex/models")).json()["models"]
    assert [m["slug"] for m in catalog] == ["messages-model", "chat-model"]


@pytest.mark.parametrize(
    ("bridge_client", "task"),
    [
        (False, {"role": "user", "content": "Use echo"}),
        (
            True,
            {
                "type": "agent_message",
                "author": "/root",
                "recipient": "/root/worker",
                "content": [{"type": "input_text", "text": "Use echo"}],
            },
        ),
    ],
    ids=["user", "agent"],
    indirect=["bridge_client"],
)
async def test_chat_function_call_and_tool_result_continuation(
    bridge_client: httpx.AsyncClient,
    httpx_mock: HTTPXMock,
    task: dict[str, Any],
    request: pytest.FixtureRequest,
) -> None:
    httpx_mock.add_response(
        url="https://chat.example/v1/chat/completions",
        json={
            "id": "chat_1",
            "object": "chat.completion",
            "created": 1,
            "model": "vendor-model",
            "choices": [
                {
                    "index": 0,
                    "finish_reason": "tool_calls",
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "call_2",
                                "type": "function",
                                "function": {"name": "echo", "arguments": '{"value":"two"}'},
                            }
                        ],
                    },
                }
            ],
            "usage": {"prompt_tokens": 10, "completion_tokens": 4, "total_tokens": 14},
        },
    )
    payload = {
        "model": "chat-model",
        "reasoning": {"effort": "high"},
        "tools": [
            {
                "type": "function",
                "name": "echo",
                "parameters": {"type": "object", "properties": {"value": {"type": "string"}}},
            }
        ],
        "input": [
            task,
            {
                "type": "function_call",
                "call_id": "call_1",
                "name": "echo",
                "arguments": '{"value":"one"}',
            },
            {"type": "function_call_output", "call_id": "call_1", "output": "one"},
        ],
    }
    response = await bridge_client.post("/v1/responses", json=payload)
    assert response.status_code == 200
    item = response.json()["output"][0]
    assert item["type"] == "function_call"
    assert item["call_id"] == "call_2"
    assert json.loads(item["arguments"]) == {"value": "two"}
    upstream = httpx_mock.get_request()
    assert upstream is not None
    body = json.loads(upstream.content)
    assert body["model"] == "vendor-model"
    expected_effort = "high" if request.node.callspec.params["bridge_client"] else None
    assert body.get("reasoning_effort") == expected_effort
    assert body["messages"][0] == {"role": "user", "content": "Use echo"}
    assert body["messages"][-1] == {"role": "tool", "tool_call_id": "call_1", "content": "one"}
    assert upstream.headers["authorization"] == "Bearer own-key"


@pytest.mark.parametrize(
    ("bridge_client", "effort", "expected_effort"),
    [
        (True, "none", "none"),
        (False, "none", None),
        (True, "high", "high"),
    ],
    indirect=["bridge_client"],
)
async def test_chat_bridge_preserves_explicit_reasoning_effort(
    bridge_client: httpx.AsyncClient,
    httpx_mock: HTTPXMock,
    effort: str,
    expected_effort: str | None,
) -> None:
    httpx_mock.add_response(
        url="https://chat.example/v1/chat/completions",
        json={
            "id": "chat_effort",
            "object": "chat.completion",
            "created": 1,
            "model": "vendor-model",
            "choices": [
                {
                    "index": 0,
                    "finish_reason": "stop",
                    "message": {"role": "assistant", "content": "OK"},
                }
            ],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        },
    )
    response = await bridge_client.post(
        "/v1/responses",
        json={
            "model": "chat-model",
            "reasoning": {"effort": effort},
            "input": "Hello",
        },
    )
    assert response.status_code == 200
    upstream = httpx_mock.get_request()
    assert upstream is not None
    assert json.loads(upstream.content).get("reasoning_effort") == expected_effort


@pytest.mark.parametrize(
    "input_value",
    [
        "Hello",
        [{"type": "agent_message", "content": [{"type": "input_text", "text": "Task"}]}],
    ],
    ids=["ordinary", "plaintext-agent"],
)
async def test_standard_access_programs_is_removed_on_translated_responses(
    bridge_client: httpx.AsyncClient,
    httpx_mock: HTTPXMock,
    input_value: Any,
) -> None:
    httpx_mock.add_response(
        url="https://chat.example/v1/chat/completions",
        json={
            "id": "chat_access",
            "object": "chat.completion",
            "created": 1,
            "model": "vendor-model",
            "choices": [
                {
                    "index": 0,
                    "finish_reason": "stop",
                    "message": {"role": "assistant", "content": "OK"},
                }
            ],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        },
    )
    response = await bridge_client.post(
        "/v1/responses",
        json={
            "model": "chat-model",
            "access_programs": {"cyber": "standard"},
            "input": input_value,
        },
    )
    assert response.status_code == 200
    upstream = httpx_mock.get_request()
    assert upstream is not None
    assert "access_programs" not in json.loads(upstream.content)


@pytest.mark.parametrize(
    "access_programs",
    [
        {"cyber": "named"},
        {"cyber": "standard", "extra": True},
        None,
        ["cyber", "standard"],
        "standard",
    ],
    ids=["named", "extra-key", "null", "array", "string"],
)
async def test_nonstandard_access_programs_is_rejected_before_translated_upstream(
    bridge_client: httpx.AsyncClient,
    httpx_mock: HTTPXMock,
    access_programs: Any,
) -> None:
    response = await bridge_client.post(
        "/v1/responses",
        json={
            "model": "chat-model",
            "access_programs": access_programs,
            "input": "Hello",
        },
    )
    assert response.status_code == 400
    error = response.json()["error"]
    assert error["code"] == "unsupported_access_programs"
    assert error["message"].startswith(
        "access_programs is supported only by the official OpenAI Responses endpoint."
    )
    assert httpx_mock.get_requests() == []


def frames(events: list[dict[str, Any]]) -> bytes:
    return b"".join(f"event: {e['type']}\r\ndata: {json.dumps(e)}\r\n\r\n".encode() for e in events)


@pytest.mark.parametrize("interrupted", [False, True])
async def test_streaming_lifecycle_and_interruption(
    bridge_client: httpx.AsyncClient,
    httpx_mock: HTTPXMock,
    interrupted: bool,
) -> None:
    events = [
        {"type": "message_start", "message": {**MESSAGE, "content": []}},
        {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "text_delta", "text": "Привет"},
        },
        {"type": "content_block_stop", "index": 0},
        {
            "type": "message_delta",
            "delta": {"stop_reason": "end_turn"},
            "usage": {"output_tokens": 2},
        },
    ]
    if not interrupted:
        events.append({"type": "message_stop"})
    raw = frames(events)
    httpx_mock.add_response(
        url="https://messages.example/v1/messages",
        headers={"content-type": "text/event-stream"},
        stream=IteratorStream([raw[i : i + 1] for i in range(len(raw))]),
    )
    response = await bridge_client.post(
        "/v1/responses",
        json={
            "model": "messages-model",
            "input": "Hello",
            "stream": True,
        },
    )
    data = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data:")]
    assert [e["sequence_number"] for e in data] == list(range(len(data)))
    assert data[0]["type"] == "response.created"
    assert any(e["type"] == "response.output_text.delta" and e["delta"] == "Привет" for e in data)
    assert data[-1]["type"] == ("error" if interrupted else "response.completed")
    if not interrupted:
        assert data[-1]["response"]["output"][0]["content"][0]["text"] == "Привет"


async def test_upstream_errors_are_responses_errors(
    bridge_client: httpx.AsyncClient,
    httpx_mock: HTTPXMock,
) -> None:
    httpx_mock.add_response(
        url="https://messages.example/v1/messages",
        status_code=403,
        json={"type": "error", "error": {"type": "permission_error", "message": "Forbidden"}},
    )
    response = await bridge_client.post(
        "/v1/responses", json={"model": "messages-model", "input": "Hi"}
    )
    assert response.status_code == 403
    assert response.json()["error"]["message"] == "Forbidden"
    assert response.json()["error"]["param"] is None


@pytest.mark.parametrize(
    "extra",
    [
        {"previous_response_id": "resp_missing"},
        {"input": [{"type": "item_reference", "id": "x"}]},
        {"tools": [{"type": "web_search"}], "tool_choice": "required"},
        {"max_output_tokens": -1},
        {"input": [{"type": "function_call", "name": "x", "call_id": "c", "arguments": "bad"}]},
    ],
)
async def test_unsupported_or_malformed_input_fails_before_upstream(
    bridge_client: httpx.AsyncClient,
    extra: dict[str, Any],
) -> None:
    response = await bridge_client.post(
        "/v1/responses",
        json={
            "model": "messages-model",
            "input": "Hi",
            **extra,
        },
    )
    assert response.status_code == 400
    assert "error" in response.json()


@pytest.mark.parametrize(
    "encrypted_fields",
    [
        {"content": [{"type": "encrypted_content", "encrypted_content": "ciphertext"}]},
        {
            "encrypted_content": "ciphertext",
            "content": [{"type": "input_text", "text": "Payload:"}],
        },
        {"encrypted_content": "ciphertext"},
    ],
    ids=["nested", "top-level-mixed", "top-level-only"],
)
async def test_chat_rejects_encrypted_agent_task_before_upstream(
    bridge_client: httpx.AsyncClient, httpx_mock: HTTPXMock, encrypted_fields: dict[str, Any]
) -> None:
    response = await bridge_client.post(
        "/v1/responses",
        json={
            "model": "chat-model",
            "input": [
                {
                    "type": "agent_message",
                    "author": "/root",
                    "recipient": "/root/worker",
                    **encrypted_fields,
                }
            ],
        },
    )
    assert response.status_code == 400
    error = response.json()["error"]
    assert error["code"] == "unsupported_encrypted_agent_message"
    assert error["message"].startswith(
        "Encrypted subagent prompts or context are unsupported on this model route."
    )
    assert "Codex Code Mode" in error["message"]
    assert httpx_mock.get_requests() == []


async def test_encrypted_agent_task_precedes_conversation_validation(
    bridge_client: httpx.AsyncClient, httpx_mock: HTTPXMock
) -> None:
    response = await bridge_client.post(
        "/v1/responses",
        json={
            "model": "chat-model",
            "conversation": "conv_previous",
            "input": [
                {
                    "type": "agent_message",
                    "content": [{"type": "encrypted_content", "encrypted_content": "ciphertext"}],
                }
            ],
        },
    )
    assert response.status_code == 400
    assert response.json() == {
        "error": {
            "message": (
                "Encrypted subagent prompts or context are unsupported on this model route. "
                "Use Codex Code Mode to send the task as plaintext, or use an authenticated "
                "native OpenAI GPT route for encrypted tasks."
            ),
            "type": "unsupported_encrypted_agent_message",
            "code": "unsupported_encrypted_agent_message",
            "param": None,
        }
    }
    assert httpx_mock.get_requests() == []


@pytest.mark.parametrize("stream", [False, True])
async def test_compressed_messages_and_stream_errors(
    bridge_client: httpx.AsyncClient,
    httpx_mock: HTTPXMock,
    stream: bool,
) -> None:
    if stream:
        # Passthrough streams HTTP error bodies too; the adapter must return JSON, not SSE.
        data = {"type": "error", "error": {"type": "permission_error", "message": "Denied"}}
        httpx_mock.add_response(
            url="https://messages.example/v1/messages",
            status_code=403,
            content=gzip.compress(json.dumps(data).encode()),
            headers={"content-type": "application/json", "content-encoding": "gzip"},
        )
    else:
        httpx_mock.add_response(
            url="https://messages.example/v1/messages",
            content=gzip.compress(json.dumps(MESSAGE).encode()),
            headers={"content-type": "application/json", "content-encoding": "gzip"},
        )
    response = await bridge_client.post(
        "/v1/responses",
        json={
            "model": "messages-model",
            "input": "Hi",
            "stream": stream,
        },
    )
    if stream:
        assert response.status_code == 403
        assert response.json()["error"]["message"] == "Denied"
    else:
        assert response.json()["output"][0]["content"][0]["text"] == "Hello"
