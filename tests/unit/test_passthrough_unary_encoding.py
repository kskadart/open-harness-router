"""A unary passthrough response reaches the client as the upstream sent it.

Claude Code offers ``accept-encoding: gzip, deflate, br, zstd`` and
api.anthropic.com answers a non-streaming request in Brotli, which httpx has
no decoder for. Reading the decoded ``response.content`` handed the client
the still-encoded bytes with the ``content-encoding`` header dropped: an
unreadable JSON body, which is how the auto-mode classifier lost every
verdict from Sonnet and fell back to the session's model. The provider now
relays the raw body and the header, as it always did for streams.
"""

from __future__ import annotations

import gzip
import json

import pytest
from pytest_httpx import HTTPXMock

from providers.passthrough import PassthroughProvider
from routing.schema import ProviderCfg, RouteLimits
from settings import UpstreamSettings

_UPSTREAM_URL = "https://api.anthropic.com/v1/messages"
_CLIENT_HEADERS = {
    "authorization": "Bearer client-oauth-token",
    "anthropic-version": "2023-06-01",
    "content-type": "application/json",
    "accept-encoding": "gzip, deflate, br, zstd",
}
_UNARY_BODY = json.dumps({
    "model": "claude-sonnet-5",
    "max_tokens": 64,
    "messages": [{"role": "user", "content": "<transcript/>"}],
}).encode()
_VERDICT = json.dumps({
    "id": "msg_verdict",
    "type": "message",
    "role": "assistant",
    "content": [{"type": "text", "text": "<severity>0</severity>"}],
    "usage": {"input_tokens": 28154, "output_tokens": 9},
}).encode()
# What a Brotli body looks like to a router without a decoder: opaque bytes.
_BROTLI_LIKE = bytes.fromhex("8323010080aaaaaaeaff74e7b306a41b4406")


class _ConnectedChannel:
    """Client channel that never disconnects."""

    async def is_disconnected(self) -> bool:
        """Report the client as still connected."""
        return False


def _provider() -> PassthroughProvider:
    cfg = ProviderCfg(
        type="passthrough", base_url="https://api.anthropic.com", forward_client_auth=True
    )
    return PassthroughProvider(
        name="anthropic",
        cfg=cfg,
        upstream=UpstreamSettings(),
        api_key=None,
        ca_bundle_path=None,
    )


async def _relay(httpx_mock: HTTPXMock, content: bytes, headers: dict[str, str]) -> tuple:
    httpx_mock.add_response(url=_UPSTREAM_URL, method="POST", content=content, headers=headers)
    provider = _provider()
    try:
        result = await provider.handle_messages(
            _UNARY_BODY,
            _CLIENT_HEADERS,
            _ConnectedChannel(),
            None,
            RouteLimits.resolve(provider.cfg, None),
        )
    finally:
        await provider.aclose()
    request = httpx_mock.get_request()
    assert request is not None
    return result, request


@pytest.mark.parametrize(
    ("encoding", "content"),
    [("gzip", gzip.compress(_VERDICT)), ("br", _BROTLI_LIKE), ("zstd", _BROTLI_LIKE)],
)
async def test_encoded_unary_body_is_relayed_with_its_encoding(
    httpx_mock: HTTPXMock, encoding: str, content: bytes
) -> None:
    """The bytes and the content-encoding go to the client untouched, whatever the codec."""
    result, request = await _relay(
        httpx_mock, content, {"content-type": "application/json", "content-encoding": encoding}
    )

    assert result.status_code == 200
    assert result.body == content
    assert result.headers["content-encoding"] == encoding
    assert result.headers["content-type"] == "application/json"
    # The client's negotiation reached the upstream unchanged.
    assert request.headers["accept-encoding"] == "gzip, deflate, br, zstd"


async def test_plain_unary_body_is_relayed_without_an_encoding_header(
    httpx_mock: HTTPXMock,
) -> None:
    result, _request = await _relay(httpx_mock, _VERDICT, {"content-type": "application/json"})

    assert result.body == _VERDICT
    assert "content-encoding" not in result.headers
    assert json.loads(result.body)["content"][0]["text"] == "<severity>0</severity>"
