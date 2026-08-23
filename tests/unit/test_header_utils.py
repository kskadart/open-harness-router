"""Unit tests for header filtering used in proxying."""

from __future__ import annotations

from starlette.datastructures import Headers

from services.header_utils import (
    forward_headers,
    merge_extra_headers,
    own_key_headers,
    response_headers,
)


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


def test_forward_headers_joins_duplicate_header_lines_with_a_comma() -> None:
    """A client sending a header as two field lines must not lose the first one.

    ``starlette.datastructures.Headers.items()`` (what FastAPI hands the
    router for the incoming request) yields one tuple PER RAW LINE, not
    merged -- building a plain dict directly from it keeps only the last
    line. Per RFC 9110 SS5.3, combining field lines with the same name is
    equivalent to joining their values with a comma.
    """
    incoming = Headers(
        raw=[
            (b"anthropic-beta", b"prompt-caching-2024-07-31"),
            (b"anthropic-beta", b"interleaved-thinking-2025-05-14"),
            (b"anthropic-version", b"2023-06-01"),
        ]
    )
    fwd = forward_headers(incoming)
    assert fwd["anthropic-beta"] == "prompt-caching-2024-07-31, interleaved-thinking-2025-05-14"
    assert fwd["anthropic-version"] == "2023-06-01"


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


def test_own_key_headers_replaces_both_client_credentials_with_provider_key() -> None:
    """Regression test for the actual vulnerability: REPLACE, don't supplement.

    A client carrying both authorization and x-api-key (the Anthropic SDK is
    known to emit both at once, anthropics/anthropic-sdk-csharp#47) must end
    up with only the provider's own key on the outgoing request -- neither
    of the client's credentials may leak through.
    """
    client_headers = {
        "authorization": "Bearer client-oauth-token",
        "x-api-key": "client-api-key",
        "anthropic-version": "2023-06-01",
    }
    outgoing_headers = own_key_headers(client_headers, "authorization", "Bearer provider-own-key")
    assert outgoing_headers["authorization"] == "Bearer provider-own-key"
    assert "x-api-key" not in outgoing_headers
    # Substring scan, not exact-equality membership: "client-oauth-token" is
    # a SUBSTRING of the full header value "Bearer client-oauth-token", and
    # `x not in dict.values()` checks exact equality per element -- it would
    # stay vacuously true even if the whole client value leaked verbatim
    # under some other header name.
    assert not any("client-oauth-token" in value for value in outgoing_headers.values())
    assert not any("client-api-key" in value for value in outgoing_headers.values())
    # Allowlisted protocol headers pass through untouched.
    assert outgoing_headers["anthropic-version"] == "2023-06-01"


def test_own_key_headers_drops_credentials_outside_the_known_pair() -> None:
    """Own-key mode is an ALLOWLIST, not a denylist over authorization/x-api-key.

    A denylist covering only the two known credential headers would still
    leak e.g. a cookie session token to a third-party upstream the client
    did not choose. own_key_headers must drop it, along with any other
    header this router doesn't explicitly recognize.
    """
    client_headers = {
        "authorization": "Bearer client-oauth-token",
        "cookie": "sessionKey=sk-ant-sid01-client-session-token",
        "x-unknown-custom-header": "some-other-credential-or-tracker",
        "anthropic-version": "2023-06-01",
    }
    outgoing_headers = own_key_headers(client_headers, "authorization", "Bearer provider-own-key")
    assert "cookie" not in outgoing_headers
    assert "x-unknown-custom-header" not in outgoing_headers
    # Substring scan (see the comment in the previous test): the cookie
    # value is "sessionKey=sk-ant-sid01-...", not exactly the bare token.
    assert not any(
        "sk-ant-sid01-client-session-token" in value for value in outgoing_headers.values()
    )
    assert outgoing_headers["anthropic-version"] == "2023-06-01"


def test_own_key_headers_keeps_user_agent_and_accept_encoding() -> None:
    """user-agent MUST travel unmodified (Kimi ToS: no client-identifier tampering);

    accept-encoding matters because the streaming path forwards the
    upstream's content-encoding to the client as-is.
    """
    client_headers = {
        "authorization": "Bearer client-oauth-token",
        "user-agent": "claude-cli/2.1.196 (external, cli)",
        "accept-encoding": "gzip, deflate",
    }
    outgoing_headers = own_key_headers(client_headers, "authorization", "Bearer provider-own-key")
    assert outgoing_headers["user-agent"] == "claude-cli/2.1.196 (external, cli)"
    assert outgoing_headers["accept-encoding"] == "gzip, deflate"


def test_own_key_headers_is_case_insensitive_for_the_allowlist_match() -> None:
    """HTTP header names are not case-sensitive; clients vary in casing."""
    client_headers = {
        "Authorization": "Bearer client-oauth-token",
        "X-Api-Key": "client-api-key",
        "Cookie": "sessionKey=sk-ant-sid01-client-session-token",
        "Anthropic-Version": "2023-06-01",
    }
    outgoing_headers = own_key_headers(client_headers, "x-api-key", "provider-own-key")
    assert "Cookie" not in outgoing_headers
    assert "X-Api-Key" not in outgoing_headers
    assert "Authorization" not in outgoing_headers
    assert outgoing_headers["Anthropic-Version"] == "2023-06-01"
    assert outgoing_headers["x-api-key"] == "provider-own-key"


def test_own_key_headers_bearer_style_sets_authorization_only() -> None:
    client_headers = {"authorization": "Bearer client-oauth-token", "x-api-key": "client-api-key"}
    outgoing_headers = own_key_headers(client_headers, "authorization", "Bearer provider-own-key")
    assert outgoing_headers == {"authorization": "Bearer provider-own-key"}


def test_own_key_headers_x_api_key_style_sets_x_api_key_only() -> None:
    client_headers = {"authorization": "Bearer client-oauth-token", "x-api-key": "client-api-key"}
    outgoing_headers = own_key_headers(client_headers, "x-api-key", "provider-own-key")
    assert outgoing_headers == {"x-api-key": "provider-own-key"}


def test_own_key_headers_joins_duplicate_allowlisted_header_lines() -> None:
    """The same RFC 9110 join behavior applies to own_key_headers, not just forward_headers."""
    client_headers = Headers(
        raw=[
            (b"anthropic-beta", b"prompt-caching-2024-07-31"),
            (b"anthropic-beta", b"interleaved-thinking-2025-05-14"),
            (b"authorization", b"Bearer client-oauth-token"),
        ]
    )
    outgoing_headers = own_key_headers(client_headers, "authorization", "Bearer provider-own-key")
    assert (
        outgoing_headers["anthropic-beta"]
        == "prompt-caching-2024-07-31, interleaved-thinking-2025-05-14"
    )
    assert outgoing_headers["authorization"] == "Bearer provider-own-key"


def test_merge_extra_headers_overrides_case_insensitively() -> None:
    """A client header and a canonically-cased extra_headers entry must not both survive.

    Client headers arrive lowercased (Starlette/h11); routing.yaml naturally
    uses canonical casing (e.g. User-Agent). A case-sensitive dict.update
    would leave BOTH "user-agent" and "User-Agent" in the result, and httpx
    would send two conflicting raw header lines to the upstream.
    """
    headers = {"user-agent": "claude-cli/2.1.196 (external, cli)", "accept": "application/json"}
    merged = merge_extra_headers(headers, {"User-Agent": "custom-router/1.0"})
    assert merged == {"User-Agent": "custom-router/1.0", "accept": "application/json"}
    assert "user-agent" not in merged


def test_merge_extra_headers_adds_non_colliding_headers_untouched() -> None:
    headers = {"accept": "application/json"}
    merged = merge_extra_headers(headers, {"HTTP-Referer": "https://example.com"})
    assert merged == {"accept": "application/json", "HTTP-Referer": "https://example.com"}


def test_merge_extra_headers_with_no_extra_headers_returns_an_equal_copy() -> None:
    headers = {"accept": "application/json"}
    merged = merge_extra_headers(headers, {})
    assert merged == headers
    assert merged is not headers
