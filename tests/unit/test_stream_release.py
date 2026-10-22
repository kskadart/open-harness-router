"""A streaming result releases its upstream connection even when its body is never read.

The transport sends the response headers before it starts the body; a
client that is gone by then (Esc in Claude Code before the first token, a
client-side retry) breaks that send, and the body generator never runs its
own cleanup. Each such request kept one upstream response open and its
connection checked out of the provider's pool, until the pool ran dry and
every later request waited out the pool timeout and got a 504.

Verify:

- ``PassthroughProvider``: on a real httpx pool of one connection, a stream
  released unread gives its connection back, so the next request is served.
- ``OpenAITranslateProvider``: releasing an unread stream closes the SDK
  stream's httpx response and drops the request's cancellation entry.
- ``to_fastapi_response``: the ASGI response releases the result when the
  client is gone before the headers, on both disconnect paths Starlette has.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

import pytest
from openai import AsyncStream
from pydantic import SecretStr
from pytest_httpx import HTTPXMock
from starlette.requests import ClientDisconnect
from starlette.types import Message

from api.adapters import to_fastapi_response
from providers.base import ProviderResult
from providers.openai_translate import OpenAITranslateProvider
from providers.passthrough import PassthroughProvider
from routing.schema import ProviderCfg, RouteLimits
from settings import UpstreamSettings

# Long enough for a free pool slot, far below the provider's 10 s pool
# timeout that a leaked slot would make the request wait out.
_POOL_WAIT_S = 2.0
_STREAM_REQUEST = json.dumps(
    {
        "model": "claude-test",
        "max_tokens": 16,
        "stream": True,
        "messages": [{"role": "user", "content": "hi"}],
    }
).encode()
_SSE_CHUNK = b"event: ping\ndata: {}\n\n"
_GATEWAY_URL = "https://gateway.example/v1"


class _ConnectedChannel:
    """Client channel that never disconnects."""

    async def is_disconnected(self) -> bool:
        """Report the client as still connected."""
        return False


async def _serve_endless_stream(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    """Answer one request with the start of an SSE stream that never ends.

    The connection stays busy until the client closes it, so it can only
    return to the client's pool by being closed.
    """
    head = await reader.readuntil(b"\r\n\r\n")
    length = 0
    for line in head.decode("latin-1").split("\r\n"):
        name, _, value = line.partition(":")
        if name.strip().lower() == "content-length":
            length = int(value)
    await reader.readexactly(length)
    writer.write(
        b"HTTP/1.1 200 OK\r\n"
        b"content-type: text/event-stream\r\n"
        b"transfer-encoding: chunked\r\n\r\n"
        + f"{len(_SSE_CHUNK):x}\r\n".encode()
        + _SSE_CHUNK
        + b"\r\n"
    )
    await writer.drain()
    await reader.read()
    writer.close()


async def test_unread_passthrough_stream_gives_its_connection_back_to_the_pool() -> None:
    server = await asyncio.start_server(_serve_endless_stream, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    cfg = ProviderCfg(type="passthrough", base_url=f"http://127.0.0.1:{port}")
    provider = PassthroughProvider(
        name="anthropic",
        cfg=cfg,
        upstream=UpstreamSettings(
            max_connections=1, max_keepalive_connections=1, retry_backoff_s=[]
        ),
        api_key=None,
        ca_bundle_path=None,
    )
    limits = RouteLimits.resolve(cfg, None)
    try:
        first = await provider.handle_messages(
            _STREAM_REQUEST, {}, _ConnectedChannel(), None, limits
        )
        assert not isinstance(first.body, bytes)
        await first.aclose()

        # The pool holds a single connection: with the first one still
        # checked out this would wait for the pool and fail as a 504.
        second = await asyncio.wait_for(
            provider.handle_messages(_STREAM_REQUEST, {}, _ConnectedChannel(), None, limits),
            timeout=_POOL_WAIT_S,
        )
        assert not isinstance(second.body, bytes)
        assert [chunk async for chunk in _first_chunk(second.body)] == [_SSE_CHUNK]
        await second.aclose()
    finally:
        await provider.aclose()
        server.close()
        await server.wait_closed()


async def _first_chunk(body: AsyncIterator[bytes]) -> AsyncIterator[bytes]:
    """Yield only the first chunk of a stream that never ends on its own."""
    async for chunk in body:
        yield chunk
        return


@pytest.mark.parametrize(
    ("api_flavor", "stream_body"),
    [
        (
            "chat",
            'data: {"id":"c","choices":[{"index":0,"delta":{"role":"assistant"},'
            '"finish_reason":null}]}\n\ndata: [DONE]\n\n',
        ),
        (
            "responses",
            'event: response.created\ndata: {"type":"response.created",'
            '"response":{"id":"resp_test"}}\n\n',
        ),
    ],
    ids=["chat", "responses"],
)
async def test_unread_translated_stream_closes_the_upstream_response(
    httpx_mock: HTTPXMock,
    monkeypatch: pytest.MonkeyPatch,
    api_flavor: str,
    stream_body: str,
) -> None:
    path = "/chat/completions" if api_flavor == "chat" else "/responses"
    httpx_mock.add_response(
        url=_GATEWAY_URL + path,
        method="POST",
        headers={"content-type": "text/event-stream"},
        text=stream_body,
    )
    cfg = ProviderCfg(
        type="openai-translate",
        base_url=_GATEWAY_URL,
        api_key_env="GATEWAY_API_KEY",
        api_flavor=api_flavor,  # type: ignore[arg-type]
        max_tokens_limit=8192,
    )
    provider = OpenAITranslateProvider(
        name="gateway",
        cfg=cfg,
        api_key=SecretStr("test-gateway-key"),
        ca_bundle_path=None,
        upstream=UpstreamSettings(),
    )
    opened: list[AsyncStream[Any]] = []
    create_stream = provider.create_stream

    async def recording_create_stream(
        request: dict[str, Any], request_id: str | None
    ) -> AsyncStream[Any]:
        stream = await create_stream(request, request_id)
        opened.append(stream)
        return stream

    monkeypatch.setattr(provider, "create_stream", recording_create_stream)
    try:
        result = await provider.handle_messages(
            _STREAM_REQUEST, {}, _ConnectedChannel(), "glm", RouteLimits.resolve(cfg, None)
        )
        assert not isinstance(result.body, bytes)
        assert len(opened) == 1
        assert opened[0].response.is_closed is False
        assert provider.active_requests

        await result.aclose()

        assert opened[0].response.is_closed is True
        assert provider.active_requests == {}
    finally:
        await provider.aclose()


def _unread_result() -> tuple[ProviderResult, list[str]]:
    """A streaming result that records whether its body ran and whether it was released."""
    log: list[str] = []

    async def body() -> AsyncIterator[bytes]:
        log.append("body started")
        yield _SSE_CHUNK

    async def release() -> None:
        log.append("released")

    return ProviderResult(200, {"content-type": "text/event-stream"}, body(), release), log


async def test_asgi_response_releases_the_result_when_the_headers_cannot_be_sent() -> None:
    """ASGI 2.4: the send of the headers raises, Starlette reports ClientDisconnect."""
    result, log = _unread_result()
    response = to_fastapi_response(result)

    async def receive() -> Message:
        return {"type": "http.disconnect"}

    async def send(_message: Message) -> None:
        raise OSError("client went away")

    scope = {"type": "http", "asgi": {"version": "3.0", "spec_version": "2.4"}}
    with pytest.raises(ClientDisconnect):
        await response(scope, receive, send)

    assert log == ["released"]


async def test_asgi_response_releases_the_result_when_the_disconnect_cancels_it() -> None:
    """ASGI < 2.4: the disconnect listener cancels the stream before its first byte."""
    result, log = _unread_result()
    response = to_fastapi_response(result)

    async def receive() -> Message:
        return {"type": "http.disconnect"}

    async def send(_message: Message) -> None:
        await asyncio.Event().wait()

    scope = {"type": "http", "asgi": {"version": "3.0", "spec_version": "2.3"}}
    await asyncio.wait_for(response(scope, receive, send), timeout=_POOL_WAIT_S)

    assert log == ["released"]
