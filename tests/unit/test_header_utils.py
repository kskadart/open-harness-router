"""Unit tests for header filtering used in proxying."""

from __future__ import annotations

from services.header_utils import forward_headers, response_headers


def test_forward_headers_strips_hop_by_hop() -> None:
    incoming = {
        "host": "test",
        "content-length": "123",
        "connection": "keep-alive",
        "transfer-encoding": "chunked",
        "keep-alive": "timeout=5",
        "authorization": "Bearer sk-xxx",
        "x-api-key": "test-key",
    }
    fwd = forward_headers(incoming)
    assert "host" not in fwd
    assert "content-length" not in fwd
    assert "connection" not in fwd
    assert "transfer-encoding" not in fwd
    assert "keep-alive" not in fwd


def test_forward_headers_preserves_anthropic_headers() -> None:
    incoming = {
        "authorization": "Bearer sk-xxx",
        "x-api-key": "test-key",
        "anthropic-version": "2023-06-01",
        "anthropic-beta": "prompt-caching-2024-07-31",
        "content-type": "application/json",
        "accept": "application/json",
    }
    fwd = forward_headers(incoming)
    assert fwd["authorization"] == "Bearer sk-xxx"
    assert fwd["x-api-key"] == "test-key"
    assert fwd["anthropic-version"] == "2023-06-01"
    assert fwd["anthropic-beta"] == "prompt-caching-2024-07-31"
    assert fwd["content-type"] == "application/json"
    assert fwd["accept"] == "application/json"


def test_forward_headers_is_case_insensitive_for_hop_by_hop() -> None:
    incoming = {"Host": "test", "Content-Length": "10", "X-Custom": "keep-me"}
    fwd = forward_headers(incoming)
    assert "Host" not in fwd
    assert "Content-Length" not in fwd
    assert fwd["X-Custom"] == "keep-me"


def test_response_headers_strips_hop_by_hop_from_upstream() -> None:
    upstream = {
        "content-type": "application/json",
        "content-length": "42",
        "transfer-encoding": "chunked",
        "x-request-id": "req_abc",
    }
    hdrs = response_headers(upstream)
    assert hdrs["content-type"] == "application/json"
    assert hdrs["x-request-id"] == "req_abc"
    assert "content-length" not in hdrs
    assert "transfer-encoding" not in hdrs
