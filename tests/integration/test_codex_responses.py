"""Codex ingress: native wire preservation, routing, discovery and isolation."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import pytest_asyncio
import yaml
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pytest_httpx import HTTPXMock, IteratorStream
from starlette.testclient import WebSocketDenialResponse

from api.responses import router as responses_router
from main import create_app
from routing.config_loader import load_routing_config
from routing.registry import ProviderRegistry
from services.monitor import Monitor
from settings import Settings

_URL = "https://api.kimi.com/coding/v1/responses"
_RESPONSE = {
    "id": "resp_test",
    "object": "response",
    "status": "completed",
    "output": [{"type": "message", "content": [{"type": "output_text", "text": "OK"}]}],
    "usage": {"input_tokens": 12, "input_tokens_details": {"cached_tokens": 4}, "output_tokens": 2},
}
_NATIVE_MODEL = {
    "slug": "gpt-native",
    "display_name": "Native GPT",
    "visibility": "list",
    "priority": 10,
    "context_window": 123456,
    "model_messages": {"custom": "keep intact"},
    "supported_reasoning_levels": [{"effort": "high", "description": "Native setting"}],
}
_ENCRYPTED_AGENT_MESSAGE = {
    "type": "agent_message",
    "author": "/root",
    "recipient": "/root/worker",
    "content": [
        {"type": "input_text", "text": "Payload:"},
        {"type": "encrypted_content", "encrypted_content": "opaque-task"},
    ],
}
_PLAINTEXT_AGENT_MESSAGE = {
    "type": "agent_message",
    "author": "/root",
    "recipient": "/root/worker",
    "content": [{"type": "input_text", "text": "Plaintext task"}],
}
_CODEX_THREAD_RECEIPT = {
    "type": "function_call_output",
    "id": "fco_receipt",
    "namespace": "codex_tui",
    "name": "send_message_to_thread",
    "output": "Synthetic delivery receipt",
    "internal_chat_message_metadata_passthrough": {"turn_id": "test", "create_time": 0},
}
_STANDARD_ACCESS_PROGRAMS = {"cyber": "standard"}


def test_builtin_codex_websocket_probe_gets_immediate_http_fallback() -> None:
    """A native Codex client must not retry unsupported WebSockets five times."""
    app = FastAPI()
    app.include_router(responses_router)
    client = TestClient(app)
    with (
        pytest.raises(WebSocketDenialResponse) as error,
        client.websocket_connect("/v1/responses"),
    ):
        pytest.fail("The router only supports HTTP Responses")
    assert error.value.status_code == 426


@pytest_asyncio.fixture
async def codex_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest
) -> AsyncIterator[httpx.AsyncClient]:
    config: dict[str, Any] = {
        "version": 1,
        "providers": {
            "anthropic": {"type": "passthrough", "base_url": "https://api.anthropic.com"},
            "kimi": {
                "type": "passthrough",
                "base_url": "https://api.kimi.com/coding/",
                "responses_base_url": "https://api.kimi.com/coding/v1",
                "responses_custom_tools_as_functions": ["exec"],
                "forward_client_auth": False,
                "api_key_env": "KIMI_TEST_KEY",
                "auth_header": "x-api-key",
                "fold_tool_additions": True,
            },
            "openai": {
                "type": "openai-translate",
                "base_url": "https://openai.example/v1",
                "api_key_env": "KIMI_TEST_KEY",
                "api_flavor": "responses",
                "max_tokens_limit": 4096,
            },
        },
        "rules": [
            {"match": {"type": "prefix", "value": "claude-"}, "provider": "anthropic"},
            {
                "match": {"type": "prefix", "value": "kimi-"},
                "provider": "kimi",
                "client_models": ["kimi-for-coding"],
                "codex": {
                    "display_name": "Kimi",
                    "context_window": 1048576,
                    "reasoning_efforts": ["low", "high", "max"],
                    "default_reasoning_effort": "high",
                    "input_modalities": ["text", "image"],
                },
            },
            {
                "match": {"type": "exact", "value": "reasoning-alias"},
                "provider": "openai",
                "upstream_model": "gpt-upstream",
            },
            {"match": {"type": "prefix", "value": "unadvertised-"}, "provider": "openai"},
        ],
        "default": {"provider": "anthropic"},
    }
    if getattr(request, "param", False):
        catalog_path = tmp_path / "native-models.json"
        catalog_path.write_text(json.dumps({"models": [_NATIVE_MODEL], "identity": "private"}))
        config["codex_catalog_path"] = str(catalog_path)
        config["responses_default"] = {"provider": "codex_openai"}
        native_url = {
            "api-key": "https://api.openai.com/v1",
            "foreign-native": "https://custom.example/v1",
            "foreign-port": "https://api.openai.com:444/v1",
            "not-default-api-key": "https://api.openai.com/v1",
            "own-key-native": "https://api.openai.com/v1",
        }.get(request.param, "https://chatgpt.com/backend-api/codex")
        messages_url = {
            "api-key": "https://messages.example",
            "foreign-native": "https://api.openai.com/v1",
        }.get(request.param, native_url)
        config["providers"]["codex_openai"] = {
            "type": "passthrough",
            "base_url": messages_url,
            "responses_base_url": native_url,
            "forward_client_auth": request.param != "own-key-native",
            "responses_forward_client_auth": request.param != "own-key-native",
            "tls_verify_hostname": request.param != "unverified-hostname",
        }
        if request.param == "own-key-native":
            config["providers"]["codex_openai"]["api_key_env"] = "KIMI_TEST_KEY"
        if request.param == "not-default-api-key":
            config["providers"]["default_codex"] = {
                "type": "passthrough",
                "base_url": "https://api.openai.com/v1",
                "responses_base_url": "https://api.openai.com/v1",
                "forward_client_auth": True,
                "responses_forward_client_auth": True,
            }
            config["responses_default"] = {"provider": "default_codex"}
            config["rules"].append(
                {
                    "match": {"type": "exact", "value": "gpt-native"},
                    "provider": "codex_openai",
                }
            )
        if request.param == "own-key":
            config["responses_default"] = {"provider": "kimi"}
    path = tmp_path / "routing.yaml"
    path.write_text(yaml.safe_dump(config))
    monkeypatch.setenv("ROUTER_CONFIG_PATH", str(path))
    monkeypatch.setenv("KIMI_TEST_KEY", "provider-secret")
    settings = Settings()
    registry = ProviderRegistry.build(load_routing_config(path), settings)
    app = create_app(settings, registry)
    app.state.settings = settings
    app.state.registry = registry
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        try:
            yield client
        finally:
            await registry.close_all()


@pytest.mark.parametrize("codex_client", [False, True, "own-key"], indirect=True)
async def test_dashboard_describes_independent_protocol_defaults(
    codex_client: httpx.AsyncClient,
    request: pytest.FixtureRequest,
) -> None:
    """The dashboard reports the same native fallback and image trust policy as ingress."""
    configured = request.node.callspec.params["codex_client"]
    state = (await codex_client.get("/dashboard/state")).json()
    protocols = {row["protocol"]: row for row in state["protocols"]}
    assert protocols["messages"]["default_provider"] == "anthropic"
    assert protocols["messages"]["model_rules"] is True
    assert protocols["responses"]["model_rules"] is True
    assert protocols["images"]["model_rules"] is False
    expected = "kimi" if configured == "own-key" else "codex_openai" if configured else None
    assert protocols["responses"]["default_provider"] == (expected or "anthropic")
    assert protocols["responses"]["available"] is bool(configured)
    assert protocols["images"]["default_provider"] == expected
    assert protocols["images"]["available"] is (configured is True)
    assert state["routes"][-1] == {"match_type": "default", "provider": "anthropic"}


async def test_catalog_lists_only_explicit_responses_models(
    codex_client: httpx.AsyncClient,
) -> None:
    catalog = (await codex_client.get("/codex/models")).json()["models"]
    assert [m["slug"] for m in catalog] == ["kimi-for-coding", "reasoning-alias"]
    assert catalog[0]["display_name"] == "Kimi"
    assert catalog[0]["context_window"] == 1048576
    assert catalog[0]["default_reasoning_level"] == "high"
    assert catalog[0]["input_modalities"] == ["text", "image"]
    standard = (await codex_client.get("/v1/models")).json()
    assert standard["object"] == "list"
    assert [m["id"] for m in standard["data"]] == [m["slug"] for m in catalog]


@pytest.mark.parametrize("codex_client", [False, True], indirect=True)
async def test_native_request_preserves_tools_reasoning_and_replaces_credentials(
    codex_client: httpx.AsyncClient, httpx_mock: HTTPXMock
) -> None:
    httpx_mock.add_response(url=_URL + "?test=true", json=_RESPONSE)
    payload = {
        "model": "kimi-for-coding",
        "input": [
            {"type": "custom_tool_call_output", "call_id": "call_1", "output": "done"},
            {"type": "reasoning", "encrypted_content": "opaque-reasoning"},
        ],
        "tools": [{"type": "custom", "name": "apply_patch", "format": {"type": "text"}}],
        "reasoning": {"effort": "high"},
        "include": ["reasoning.encrypted_content"],
    }
    raw = json.dumps(payload, indent=2).encode()
    response = await codex_client.post(
        "/v1/responses?test=true",
        content=raw,
        headers={
            "authorization": "Bearer client-secret",
            "x-api-key": "client-key",
            "cookie": "private-session",
            "user-agent": "real-codex-client/1",
            "content-type": "application/json",
        },
    )
    assert response.status_code == 200
    assert response.json() == _RESPONSE
    request = httpx_mock.get_request()
    assert request is not None
    assert request.content == raw
    assert request.headers["authorization"] == "Bearer provider-secret"
    assert "x-api-key" not in request.headers
    assert "cookie" not in request.headers
    assert request.headers["user-agent"] == "real-codex-client/1"


async def test_custom_native_responses_receives_plaintext_agent_task_as_user_message(
    codex_client: httpx.AsyncClient, httpx_mock: HTTPXMock
) -> None:
    httpx_mock.add_response(url=_URL, json=_RESPONSE)
    earlier_message = {"type": "message", "role": "user", "content": "Earlier instruction"}
    payload = {
        "model": "kimi-for-coding",
        "input": [earlier_message, _PLAINTEXT_AGENT_MESSAGE],
        "reasoning": {"effort": "max"},
    }
    response = await codex_client.post("/v1/responses", json=payload)
    assert response.status_code == 200
    outbound = httpx_mock.get_request()
    assert outbound is not None
    assert json.loads(outbound.content) == {
        **payload,
        "input": [
            earlier_message,
            {"type": "message", "role": "user", "content": _PLAINTEXT_AGENT_MESSAGE["content"]},
        ],
    }


async def test_custom_native_responses_drops_inherited_codex_thread_receipt(
    codex_client: httpx.AsyncClient, httpx_mock: HTTPXMock
) -> None:
    httpx_mock.add_response(url=_URL, json=_RESPONSE)
    paired_call = {
        "type": "function_call",
        "call_id": "call_test",
        "namespace": "codex_tui",
        "name": "send_message_to_thread",
        "arguments": "{}",
    }
    paired_output = {**_CODEX_THREAD_RECEIPT, "id": "fco_paired", "call_id": "call_test"}
    response = await codex_client.post(
        "/v1/responses",
        json={
            "model": "kimi-for-coding",
            "input": [_CODEX_THREAD_RECEIPT, _PLAINTEXT_AGENT_MESSAGE, paired_call, paired_output],
        },
    )
    assert response.status_code == 200
    outbound = httpx_mock.get_request()
    assert outbound is not None
    assert json.loads(outbound.content)["input"] == [
        {"type": "message", "role": "user", "content": _PLAINTEXT_AGENT_MESSAGE["content"]},
        paired_call,
        paired_output,
    ]


@pytest.mark.parametrize("codex_client", [True], indirect=True)
async def test_combined_catalog_preserves_native_metadata(codex_client: httpx.AsyncClient) -> None:
    catalog = (await codex_client.get("/codex/models")).json()
    assert catalog["models"][0] == _NATIVE_MODEL
    assert [model["slug"] for model in catalog["models"]] == [
        "gpt-native",
        "kimi-for-coding",
        "reasoning-alias",
    ]
    assert "identity" not in catalog


@pytest.mark.parametrize("codex_client", [True], indirect=True)
async def test_responses_default_forwards_native_subscription_credentials(
    codex_client: httpx.AsyncClient,
    httpx_mock: HTTPXMock,
) -> None:
    url = "https://chatgpt.com/backend-api/codex/responses"
    httpx_mock.add_response(url=url, json=_RESPONSE)
    response = await codex_client.post(
        "/v1/responses",
        json={
            "model": "gpt-native",
            "input": "hi",
        },
        headers={"authorization": "Bearer native-subscription", "chatgpt-account-id": "account"},
    )
    assert response.status_code == 200
    outbound = httpx_mock.get_request()
    assert outbound is not None
    assert outbound.headers["authorization"] == "Bearer native-subscription"
    assert outbound.headers["chatgpt-account-id"] == "account"
    assert "provider-secret" not in str(outbound.headers)


@pytest.mark.parametrize(
    "agent_message",
    [
        _ENCRYPTED_AGENT_MESSAGE,
        {**_PLAINTEXT_AGENT_MESSAGE, "encrypted_content": "opaque-task"},
        {"type": "agent_message", "encrypted_content": "opaque-task"},
    ],
    ids=["nested", "top-level-mixed", "top-level-only"],
)
@pytest.mark.parametrize(
    ("codex_client", "url"),
    [
        (True, "https://chatgpt.com/backend-api/codex/responses"),
        ("api-key", "https://api.openai.com/v1/responses"),
    ],
    indirect=["codex_client"],
)
async def test_encrypted_agent_message_is_forwarded_intact_to_native_gpt(
    codex_client: httpx.AsyncClient, httpx_mock: HTTPXMock, url: str, agent_message: dict[str, Any]
) -> None:
    httpx_mock.add_response(url=url, json=_RESPONSE)
    body = json.dumps(
        {
            "model": "gpt-native",
            "input": [_CODEX_THREAD_RECEIPT, _PLAINTEXT_AGENT_MESSAGE, agent_message],
        },
        indent=2,
    )
    response = await codex_client.post(
        "/v1/responses",
        content=body,
        headers={"authorization": "Bearer native-auth", "content-type": "application/json"},
    )
    assert response.status_code == 200
    outbound = httpx_mock.get_request()
    assert outbound is not None
    assert outbound.content == body.encode()
    assert outbound.headers["authorization"] == "Bearer native-auth"


@pytest.mark.parametrize("codex_client", [True], indirect=True)
async def test_encrypted_agent_message_without_auth_is_rejected_before_native_gpt(
    codex_client: httpx.AsyncClient, httpx_mock: HTTPXMock
) -> None:
    response = await codex_client.post(
        "/v1/responses",
        json={"model": "gpt-native", "input": [_ENCRYPTED_AGENT_MESSAGE]},
    )
    assert response.status_code == 400
    error = response.json()["error"]
    assert error["code"] == "unsupported_encrypted_agent_message"
    assert httpx_mock.get_requests() == []


@pytest.mark.parametrize(
    "agent_message",
    [
        _ENCRYPTED_AGENT_MESSAGE,
        {**_PLAINTEXT_AGENT_MESSAGE, "encrypted_content": "opaque-task"},
        {"type": "agent_message", "encrypted_content": "opaque-task"},
    ],
    ids=["nested", "top-level-mixed", "top-level-only"],
)
@pytest.mark.parametrize(
    ("codex_client", "model"),
    [
        (True, "kimi-for-coding"),
        (True, "reasoning-alias"),
        (True, "non-gpt"),
        ("own-key", "gpt-native"),
        ("foreign-native", "gpt-native"),
        ("foreign-port", "gpt-native"),
        ("own-key-native", "gpt-native"),
        ("unverified-hostname", "gpt-native"),
    ],
    indirect=["codex_client"],
)
async def test_encrypted_agent_message_stays_off_other_responses_routes(
    codex_client: httpx.AsyncClient,
    httpx_mock: HTTPXMock,
    model: str,
    agent_message: dict[str, Any],
) -> None:
    response = await codex_client.post(
        "/v1/responses",
        json={"model": model, "input": [agent_message]},
        headers={"authorization": "Bearer native-auth"},
    )
    assert response.status_code == 400
    error = response.json()["error"]
    assert error["code"] == "unsupported_encrypted_agent_message"
    assert error["message"].startswith(
        "Encrypted subagent prompts or context are unsupported on this model route."
    )
    assert "Codex Code Mode" in error["message"]
    assert httpx_mock.get_requests() == []


async def test_standard_access_programs_is_removed_on_custom_responses(
    codex_client: httpx.AsyncClient, httpx_mock: HTTPXMock
) -> None:
    httpx_mock.add_response(url=_URL, json=_RESPONSE)
    response = await codex_client.post(
        "/v1/responses",
        json={
            "model": "kimi-for-coding",
            "access_programs": _STANDARD_ACCESS_PROGRAMS,
            "input": [_PLAINTEXT_AGENT_MESSAGE],
        },
    )
    assert response.status_code == 200
    outbound = httpx_mock.get_request()
    assert outbound is not None
    payload = json.loads(outbound.content)
    assert "access_programs" not in payload
    assert payload["input"] == [
        {
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": "Plaintext task"}],
        }
    ]


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
@pytest.mark.parametrize(
    "codex_client",
    [
        False,
    ],
    indirect=["codex_client"],
)
async def test_nonstandard_access_programs_is_rejected_before_custom_upstream(
    codex_client: httpx.AsyncClient,
    httpx_mock: HTTPXMock,
    access_programs: Any,
) -> None:
    response = await codex_client.post(
        "/v1/responses",
        json={
            "model": "kimi-for-coding",
            "access_programs": access_programs,
            "input": [_PLAINTEXT_AGENT_MESSAGE],
        },
    )
    assert response.status_code == 400
    error = response.json()["error"]
    assert error["code"] == "unsupported_access_programs"
    assert error["message"].startswith(
        "access_programs is supported only by the official OpenAI Responses endpoint."
    )
    assert httpx_mock.get_requests() == []


@pytest.mark.parametrize(
    ("codex_client", "url"),
    [
        ("not-default-api-key", "https://api.openai.com/v1/responses"),
        ("api-key", "https://api.openai.com/v1/responses"),
        ("own-key-native", "https://api.openai.com/v1/responses"),
    ],
    indirect=["codex_client"],
)
async def test_official_openai_responses_preserves_access_programs(
    codex_client: httpx.AsyncClient, httpx_mock: HTTPXMock, url: str
) -> None:
    httpx_mock.add_response(url=url, json=_RESPONSE)
    body = json.dumps(
        {
            "model": "gpt-native",
            "access_programs": {"cyber": "named"},
            "input": "Hello",
        },
        indent=2,
    )
    response = await codex_client.post(
        "/v1/responses",
        content=body,
        headers={"authorization": "Bearer native-auth", "content-type": "application/json"},
    )
    assert response.status_code == 200
    outbound = httpx_mock.get_request()
    assert outbound is not None
    assert outbound.content == body.encode()


@pytest.mark.parametrize("codex_client", [True], indirect=True)
async def test_explicit_unsupported_rule_does_not_use_native_fallback(
    codex_client: httpx.AsyncClient,
) -> None:
    response = await codex_client.post("/v1/responses", json={"model": "claude-sonnet"})
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "unsupported_model"


async def test_native_alias_uses_shared_routing_rules(
    codex_client: httpx.AsyncClient, httpx_mock: HTTPXMock
) -> None:
    httpx_mock.add_response(url="https://openai.example/v1/responses", json=_RESPONSE)
    response = await codex_client.post(
        "/v1/responses", json={"model": "reasoning-alias", "input": "hello"}
    )
    assert response.status_code == 200
    request = httpx_mock.get_request()
    assert request is not None
    assert json.loads(request.content)["model"] == "gpt-upstream"


async def test_messages_keeps_its_original_auth_and_endpoint(
    codex_client: httpx.AsyncClient, httpx_mock: HTTPXMock
) -> None:
    """Enabling Responses must not switch Claude's transport or credential style."""
    url = "https://api.kimi.com/coding/v1/messages"
    body = {"id": "msg_test", "type": "message", "content": []}
    httpx_mock.add_response(url=url, json=body)
    response = await codex_client.post(
        "/v1/messages",
        json={
            "model": "kimi-for-coding",
            "messages": [{"role": "user", "content": "hi"}],
            "max_tokens": 16,
        },
    )
    assert response.json() == body
    request = httpx_mock.get_request()
    assert request is not None
    assert request.headers["x-api-key"] == "provider-secret"
    assert "authorization" not in request.headers


async def test_stream_failure_uses_openai_error_shape_and_releases_upstream(
    codex_client: httpx.AsyncClient, httpx_mock: HTTPXMock
) -> None:
    class BrokenStream(httpx.AsyncByteStream):
        closed = False

        async def __aiter__(self) -> AsyncIterator[bytes]:
            yield b'event: response.created\ndata: {"type":"response.created"}\n\n'
            raise httpx.ReadError("connection dropped")

        async def aclose(self) -> None:
            self.closed = True

    stream = BrokenStream()
    httpx_mock.add_response(
        url=_URL,
        headers={"content-type": "text/event-stream"},
        stream=stream,
    )
    response = await codex_client.post(
        "/v1/responses",
        json={
            "model": "kimi-for-coding",
            "input": "hi",
            "stream": True,
        },
    )
    payloads = [
        json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")
    ]
    assert payloads[-1]["type"] == "error"
    assert payloads[-1]["code"] == "upstream_error"
    assert "error" not in payloads[-1]  # not an Anthropic error envelope
    assert stream.closed


@pytest.mark.parametrize("cache_write", [0, 3])
async def test_native_stream_preserves_wire_events_and_usage(
    codex_client: httpx.AsyncClient,
    httpx_mock: HTTPXMock,
    cache_write: int,
) -> None:
    upstream_response = {
        **_RESPONSE,
        "usage": {
            **_RESPONSE["usage"],
            "input_tokens_details": {"cached_tokens": 4, "cache_write_tokens": cache_write},
        },
    }
    events = [
        {"type": "response.output_text.delta", "delta": "OK"},
        {"type": "response.completed", "response": upstream_response},
    ]
    wire = b"".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n".encode() for e in events)
    httpx_mock.add_response(
        url=_URL,
        headers={"content-type": "text/event-stream"},
        stream=IteratorStream([wire[:17], wire[17:]]),
    )
    response = await codex_client.post(
        "/v1/responses", json={"model": "kimi-for-coding", "input": "hi", "stream": True}
    )
    assert response.content == wire
    snapshot = Monitor.current().snapshot()
    assert snapshot["totals"]["usage"]["input_tokens"] == 8 - cache_write
    assert snapshot["totals"]["usage"]["cache_read_input_tokens"] == 4
    assert snapshot["totals"]["usage"]["cache_creation_input_tokens"] == cache_write
    assert snapshot["totals"]["usage"]["output_tokens"] == 2


@pytest.mark.parametrize("status", [400, 401, 429, 500])
async def test_upstream_error_is_preserved(
    codex_client: httpx.AsyncClient, httpx_mock: HTTPXMock, status: int
) -> None:
    error = {"error": {"message": "upstream failure", "type": "upstream_error"}}
    httpx_mock.add_response(url=_URL, status_code=status, json=error, headers={"retry-after": "5"})
    response = await codex_client.post(
        "/v1/responses", json={"model": "kimi-for-coding", "input": "hi"}
    )
    assert response.status_code == status
    assert response.json() == error
    assert response.headers["retry-after"] == "5"


@pytest.mark.parametrize(
    "payload", [None, [], {}, {"model": ""}, {"model": "kimi-for-coding", "stream": "false"}]
)
async def test_invalid_requests_never_reach_an_upstream(
    codex_client: httpx.AsyncClient, payload: object
) -> None:
    response = await codex_client.post("/v1/responses", content=json.dumps(payload))
    assert response.status_code == 400
    assert "error" in response.json()


async def test_unknown_model_does_not_fall_through_to_anthropic(
    codex_client: httpx.AsyncClient,
) -> None:
    response = await codex_client.post("/v1/responses", json={"model": "unknown", "input": "hi"})
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "unsupported_model"


@pytest.mark.parametrize("stream", [False, True])
async def test_exec_custom_tool_round_trip_and_replay(
    codex_client: httpx.AsyncClient,
    httpx_mock: HTTPXMock,
    stream: bool,
) -> None:
    code = 'console.log("привет\\nworld")'
    call = {
        "id": "fc_test",
        "type": "function_call",
        "name": "exec",
        "call_id": "call_next",
        "arguments": json.dumps({"input": code}),
        "status": "completed",
    }
    response_body = {**_RESPONSE, "output": [call]}
    if stream:
        events = [
            {
                "type": "response.output_item.added",
                "output_index": 0,
                "item": {**call, "arguments": "", "status": "in_progress"},
            },
            {
                "type": "response.function_call_arguments.delta",
                "item_id": "fc_test",
                "output_index": 0,
                "delta": call["arguments"][:7],
            },
            {
                "type": "response.function_call_arguments.done",
                "item_id": "fc_test",
                "output_index": 0,
                "arguments": call["arguments"],
            },
            {"type": "response.output_item.done", "output_index": 0, "item": call},
            {"type": "response.completed", "response": response_body},
        ]
        wire = b"".join(
            f"event: {e['type']}\r\ndata: {json.dumps(e, ensure_ascii=False)}\r\n\r\n".encode()
            for e in events
        )
        httpx_mock.add_response(
            url=_URL,
            headers={"content-type": "text/event-stream"},
            stream=IteratorStream([wire[i : i + 1] for i in range(len(wire))]),
        )
    else:
        httpx_mock.add_response(url=_URL, json=response_body)
    response = await codex_client.post(
        "/v1/responses",
        json={
            "model": "kimi-for-coding",
            "stream": stream,
            "tools": [
                {"type": "custom", "name": "exec", "description": "Run JavaScript"},
                {"type": "custom", "name": "apply_patch"},
            ],
            "input": [
                {
                    "type": "custom_tool_call",
                    "id": "ctc_old",
                    "call_id": "call_old",
                    "name": "exec",
                    "input": "old_code()",
                },
                {"type": "custom_tool_call_output", "call_id": "call_old", "output": "old result"},
                {
                    "type": "custom_tool_call",
                    "call_id": "patch_1",
                    "name": "apply_patch",
                    "input": "patch",
                },
                {"type": "custom_tool_call_output", "call_id": "patch_1", "output": "patched"},
                {
                    "type": "additional_tools",
                    "id": "tools_from_previous_model",
                    "role": "developer",
                    "tools": [
                        {
                            "type": "namespace",
                            "name": "functions",
                            "tools": [
                                {"type": "custom", "name": "exec", "format": {"type": "text"}},
                                {"type": "custom", "name": "apply_patch"},
                            ],
                        }
                    ],
                },
            ],
            "tool_choice": {"type": "custom", "name": "exec"},
        },
    )
    assert response.status_code == 200
    request = httpx_mock.get_request()
    assert request is not None
    sent = json.loads(request.content)
    assert sent["tools"][0]["type"] == "function"
    assert sent["tools"][1]["type"] == "custom"
    assert sent["input"][0]["type"] == "function_call"
    assert json.loads(sent["input"][0]["arguments"]) == {"input": "old_code()"}
    assert sent["input"][1]["type"] == "function_call_output"
    assert sent["input"][3]["type"] == "custom_tool_call_output"
    # Both historical definitions are already present at request level.
    assert len(sent["input"]) == 4
    assert sent["tool_choice"]["type"] == "function"
    assert request.headers["accept-encoding"] == "identity"
    if stream:
        received = [
            json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")
        ]
        assert [e["sequence_number"] for e in received] == list(range(len(received)))
        delta = next(e for e in received if e["type"] == "response.custom_tool_call_input.delta")
        assert delta["delta"] == code
        result = received[-1]["response"]
    else:
        result = response.json()
    assert result["output"][0]["type"] == "custom_tool_call"
    assert result["output"][0]["input"] == code
    assert result["output"][0]["call_id"] == "call_next"


@pytest.mark.parametrize("codex_client", [True], indirect=True)
@pytest.mark.parametrize("operation", ["generations", "edits"])
@pytest.mark.parametrize("model", ["gpt-image-2", "kimi-for-coding"])
async def test_native_images_preserve_json_auth_and_never_follow_custom_model_rules(
    codex_client: httpx.AsyncClient,
    httpx_mock: HTTPXMock,
    operation: str,
    model: str,
) -> None:
    response_body = {
        "created": 1,
        "data": [{"b64_json": "aW1hZ2U="}],
        "usage": {"input_tokens": 12, "output_tokens": 8},
    }
    url = f"https://chatgpt.com/backend-api/codex/images/{operation}?test=true"
    httpx_mock.add_response(
        url=url, json=response_body, headers={"x-codex-imagegen-request-id": "image-request-1"}
    )
    payload = {"model": model, "prompt": "samurai mask — ёжик", "background": "transparent"}
    if operation == "edits":
        payload["images"] = [{"image_url": "data:image/png;base64,aW1hZ2U="}]
    raw = json.dumps(payload, indent=2, ensure_ascii=False).encode()
    response = await codex_client.post(
        f"/v1/images/{operation}?test=true",
        content=raw,
        headers={
            "authorization": "Bearer native-client-token",
            "content-type": "application/json",
            "x-codex-image-turn-id": "turn-image-1",
            "Accept-Encoding": "gzip, deflate",
        },
    )
    assert response.status_code == 200
    assert response.json() == response_body
    assert response.headers["x-codex-imagegen-request-id"] == "image-request-1"
    forwarded = httpx_mock.get_request()
    assert forwarded is not None and forwarded.content == raw
    assert forwarded.headers["authorization"] == "Bearer native-client-token"
    assert forwarded.headers["x-codex-image-turn-id"] == "turn-image-1"
    assert forwarded.headers["accept-encoding"] == "identity"
    event = Monitor.current().snapshot()["events"][0]
    assert event["provider"] == "codex_openai"
    assert event["endpoint"] == f"images/{operation}"
    assert event["usage"]["output_tokens"] == 8
    assert "ttft_ms" not in event and "cost_usd" not in event


@pytest.mark.parametrize("codex_client", [False, "own-key"], indirect=True)
@pytest.mark.parametrize("operation", ["generations", "edits"])
async def test_images_require_an_explicit_authenticated_native_fallback(
    codex_client: httpx.AsyncClient,
    operation: str,
) -> None:
    response = await codex_client.post(
        f"/v1/images/{operation}",
        json={"model": "gpt-image-2", "prompt": "test"},
        headers={"authorization": "Bearer private-client-token"},
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "unsupported_image_provider"
    assert Monitor.current().snapshot()["active"] == []


@pytest.mark.parametrize("codex_client", [True], indirect=True)
@pytest.mark.parametrize("operation", ["generations", "edits"])
@pytest.mark.parametrize(
    "payload", [None, [], {}, {"model": ""}, {"model": "gpt-image-2", "stream": "false"}]
)
async def test_invalid_image_requests_do_not_reach_upstream(
    codex_client: httpx.AsyncClient,
    operation: str,
    payload: object,
) -> None:
    response = await codex_client.post(f"/v1/images/{operation}", content=json.dumps(payload))
    assert response.status_code == 400
    assert response.json()["error"]["message"]


@pytest.mark.parametrize("codex_client", [True], indirect=True)
@pytest.mark.parametrize("operation", ["generations", "edits"])
@pytest.mark.parametrize("status", [401, 429, 500])
async def test_native_image_upstream_errors_are_relayed(
    codex_client: httpx.AsyncClient,
    httpx_mock: HTTPXMock,
    operation: str,
    status: int,
) -> None:
    error = {"error": {"message": "upstream image error", "type": "api_error"}}
    httpx_mock.add_response(
        url=f"https://chatgpt.com/backend-api/codex/images/{operation}",
        status_code=status,
        json=error,
        headers={"retry-after": "5"},
    )
    response = await codex_client.post(
        f"/v1/images/{operation}", json={"model": "gpt-image-2", "prompt": "test"}
    )
    assert response.status_code == status
    assert response.json() == error
    assert response.headers["retry-after"] == "5"


@pytest.mark.parametrize("codex_client", [True], indirect=True)
async def test_native_image_transport_failure_finishes_the_tracker(
    codex_client: httpx.AsyncClient,
    httpx_mock: HTTPXMock,
) -> None:
    httpx_mock.add_exception(
        httpx.ReadTimeout("synthetic timeout"),
        url="https://chatgpt.com/backend-api/codex/images/generations",
    )
    response = await codex_client.post(
        "/v1/images/generations", json={"model": "gpt-image-2", "prompt": "test"}
    )
    assert response.status_code == 504
    snapshot = Monitor.current().snapshot()
    assert snapshot["active"] == []
    assert snapshot["totals"]["errors"] == 1


@pytest.mark.parametrize("codex_client", [True], indirect=True)
async def test_native_discovery_refreshes_removals_metadata_and_account_scope(
    codex_client: httpx.AsyncClient,
    httpx_mock: HTTPXMock,
) -> None:
    url = "https://chatgpt.com/backend-api/codex/models?client_version=0.169.0"
    first = {**_NATIVE_MODEL, "slug": "gpt-new", "context_window": 999999}
    second = {**_NATIVE_MODEL, "slug": "gpt-next"}
    httpx_mock.add_response(url=url, json={"models": [first], "protocol_extension": "kept"})
    httpx_mock.add_response(url=url, json={"models": [second]})
    for token, account, expected in [
        ("account-a-token", "a", first),
        ("account-b-token", "b", second),
    ]:
        response = await codex_client.get(
            "/v1/models?client_version=0.169.0",
            headers={
                "authorization": f"Bearer {token}",
                "chatgpt-account-id": account,
                "if-none-match": "upstream-old-etag",
            },
        )
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        models = response.json()["models"]
        assert models[0] == expected
        assert [m["slug"] for m in models] == [
            expected["slug"],
            "kimi-for-coding",
            "reasoning-alias",
        ]
        if account == "a":
            assert response.json()["protocol_extension"] == "kept"
    requests = httpx_mock.get_requests()
    assert [r.headers["authorization"] for r in requests] == [
        "Bearer account-a-token",
        "Bearer account-b-token",
    ]
    assert [r.headers["chatgpt-account-id"] for r in requests] == ["a", "b"]
    assert all("if-none-match" not in r.headers for r in requests)
    # No account-specific result is stored in the unauthenticated bootstrap catalog.
    local = (await codex_client.get("/codex/models")).json()["models"]
    assert local[0] == _NATIVE_MODEL


@pytest.mark.parametrize("codex_client", [True], indirect=True)
async def test_local_catalog_reload_does_not_require_router_restart(
    codex_client: httpx.AsyncClient,
    tmp_path: Path,
) -> None:
    path = tmp_path / "native-models.json"
    fresh = {**_NATIVE_MODEL, "slug": "gpt-local-new"}
    path.write_text(json.dumps({"models": [fresh]}))
    assert (await codex_client.get("/codex/models")).json()["models"][0] == fresh
    path.write_text("partial update")
    assert (await codex_client.get("/codex/models")).json()["models"][0] == fresh
    newer = {**_NATIVE_MODEL, "slug": "gpt-local-next"}
    path.write_text(json.dumps({"models": [newer]}))
    assert (await codex_client.get("/codex/models")).json()["models"][0] == newer


@pytest.mark.parametrize("codex_client", [True], indirect=True)
@pytest.mark.parametrize(
    "status,payload",
    [
        (401, {"error": "expired"}),
        (503, {"error": "unavailable"}),
        (200, {"data": [{"id": "standard-api-model"}]}),
    ],
)
async def test_native_discovery_errors_do_not_return_stale_account_data(
    codex_client: httpx.AsyncClient,
    httpx_mock: HTTPXMock,
    status: int,
    payload: dict[str, Any],
) -> None:
    httpx_mock.add_response(
        url="https://chatgpt.com/backend-api/codex/models?client_version=0.169.0",
        status_code=status,
        json=payload,
    )
    response = await codex_client.get(
        "/v1/models?client_version=0.169.0",
        headers={
            "authorization": "Bearer caller-token",
        },
    )
    assert response.status_code == (502 if status == 200 else status)
    assert "error" in response.json()
    assert "models" not in response.json()


@pytest.mark.parametrize("codex_client", ["own-key"], indirect=True)
async def test_discovery_never_sends_native_credentials_to_own_key_fallback(
    codex_client: httpx.AsyncClient,
) -> None:
    response = await codex_client.get(
        "/v1/models?client_version=0.169.0",
        headers={
            "authorization": "Bearer account-secret",
            "chatgpt-account-id": "private-account",
        },
    )
    assert response.status_code == 200
    assert response.json()["models"][0] == _NATIVE_MODEL


async def test_anthropic_discovery_preserves_format_auth_and_pagination(
    codex_client: httpx.AsyncClient,
    httpx_mock: HTTPXMock,
) -> None:
    catalogs = [
        {
            "data": [{"id": "claude-old", "type": "model", "display_name": "Old"}],
            "has_more": True,
            "first_id": "claude-old",
            "last_id": "claude-old",
        },
        {
            "data": [{"id": "claude-new", "type": "model", "display_name": "New"}],
            "has_more": False,
            "first_id": "claude-new",
            "last_id": "claude-new",
        },
    ]
    for catalog in catalogs:
        httpx_mock.add_response(
            method="GET", url="https://api.anthropic.com/v1/models?limit=100", json=catalog
        )
        response = await codex_client.get(
            "/v1/models?limit=100",
            headers={
                "anthropic-version": "2023-06-01",
                "authorization": "Bearer native-claude-login",
                "anthropic-beta": "test-beta",
            },
        )
        assert response.status_code == 200
        assert response.json() == catalog
    requests = httpx_mock.get_requests()
    assert len(requests) == 2
    assert all(r.headers["authorization"] == "Bearer native-claude-login" for r in requests)
    assert all(r.headers["anthropic-version"] == "2023-06-01" for r in requests)
    assert all(r.headers["anthropic-beta"] == "test-beta" for r in requests)


async def test_anthropic_discovery_preserves_upstream_errors(
    codex_client: httpx.AsyncClient,
    httpx_mock: HTTPXMock,
) -> None:
    error = {"type": "error", "error": {"type": "authentication_error", "message": "Expired"}}
    httpx_mock.add_response(
        method="GET", url="https://api.anthropic.com/v1/models", status_code=401, json=error
    )
    response = await codex_client.get("/v1/models", headers={"anthropic-version": "2023-06-01"})
    assert response.status_code == 401
    assert response.json() == error


@pytest.mark.parametrize("codex_client", [True], indirect=True)
async def test_standard_list_does_not_forward_unspecified_client_credentials(
    codex_client: httpx.AsyncClient,
) -> None:
    response = await codex_client.get(
        "/v1/models", headers={"authorization": "Bearer arbitrary-key"}
    )
    assert response.status_code == 200
    assert response.json()["object"] == "list"
    assert response.json()["data"][0]["id"] == "gpt-native"
