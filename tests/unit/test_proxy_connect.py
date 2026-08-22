"""Unit tests for parsing the ``CONNECT`` request (``proxy.connect``)."""

from __future__ import annotations

import asyncio

import pytest

from proxy.connect import ConnectRequestError, parse_connect_request, read_connect_request

_TIMEOUT_S = 2.0


def _head(request_line: str, *header_lines: str) -> bytes:
    """Build the request prefix from a request line and headers.

    Args:
        request_line: the first line of the request.
        *header_lines: header lines without a trailing newline.

    Returns:
        The prefix bytes, ending with a blank line.
    """
    return ("\r\n".join([request_line, *header_lines]) + "\r\n\r\n").encode("latin-1")


def test_connect_request_line_yields_host_and_port() -> None:
    """The ``CONNECT host:port`` line is parsed into host and port."""
    request = parse_connect_request(_head("CONNECT api.anthropic.com:443 HTTP/1.1"))

    assert request.host == "api.anthropic.com"
    assert request.port == 443


def test_connect_target_without_port_defaults_to_https() -> None:
    """A target without an explicit port is treated as HTTPS."""
    request = parse_connect_request(_head("CONNECT api.anthropic.com HTTP/1.1"))

    assert request.port == 443


def test_connect_target_accepts_ipv6_literal_in_brackets() -> None:
    """An IPv6 literal in square brackets is parsed without brackets in the host."""
    request = parse_connect_request(_head("CONNECT [::1]:8443 HTTP/1.1"))

    assert request.host == "::1"
    assert request.port == 8443


def test_connect_headers_are_parsed_with_lowercase_names() -> None:
    """Request headers are available with lowercase names."""
    request = parse_connect_request(
        _head(
            "CONNECT api.anthropic.com:443 HTTP/1.1",
            "Host: api.anthropic.com:443",
            "Proxy-Connection: Keep-Alive",
        )
    )

    assert request.headers == {
        "host": "api.anthropic.com:443",
        "proxy-connection": "Keep-Alive",
    }


def test_non_connect_method_is_rejected() -> None:
    """A regular request on the proxy port is rejected as an unsupported method."""
    with pytest.raises(ConnectRequestError):
        parse_connect_request(_head("GET /v1/messages HTTP/1.1", "Host: example.com"))


def test_malformed_request_line_is_rejected() -> None:
    """A request line that is not three parts is rejected."""
    with pytest.raises(ConnectRequestError):
        parse_connect_request(_head("CONNECT api.anthropic.com:443"))


def test_non_numeric_port_is_rejected() -> None:
    """A non-numeric port in the target is rejected."""
    with pytest.raises(ConnectRequestError):
        parse_connect_request(_head("CONNECT api.anthropic.com:https HTTP/1.1"))


def test_port_out_of_range_is_rejected() -> None:
    """A port outside the 1-65535 range is rejected."""
    with pytest.raises(ConnectRequestError):
        parse_connect_request(_head("CONNECT api.anthropic.com:70000 HTTP/1.1"))


def test_empty_host_is_rejected() -> None:
    """A target without a host is rejected."""
    with pytest.raises(ConnectRequestError):
        parse_connect_request(_head("CONNECT :443 HTTP/1.1"))


def test_unterminated_ipv6_literal_is_rejected() -> None:
    """An unclosed bracket in an IPv6 literal is rejected."""
    with pytest.raises(ConnectRequestError):
        parse_connect_request(_head("CONNECT [::1:443 HTTP/1.1"))


async def test_read_connect_request_reads_head_from_stream() -> None:
    """Reading from the stream stops at the blank line and yields the parsed request."""
    reader = asyncio.StreamReader()
    reader.feed_data(_head("CONNECT api.anthropic.com:443 HTTP/1.1", "Host: api.anthropic.com"))

    request = await read_connect_request(reader, _TIMEOUT_S)

    assert (request.host, request.port) == ("api.anthropic.com", 443)


async def test_read_connect_request_reports_early_disconnect() -> None:
    """A connection dropped before the end of the prefix becomes a parse error."""
    reader = asyncio.StreamReader()
    reader.feed_data(b"CONNECT api.anthropic.com:443 HTTP/1.1\r\n")
    reader.feed_eof()

    with pytest.raises(ConnectRequestError):
        await read_connect_request(reader, _TIMEOUT_S)


async def test_read_connect_request_times_out_on_a_silent_client() -> None:
    """A client that opens a connection and sends no bytes does not hold the read forever."""
    reader = asyncio.StreamReader()

    with pytest.raises(ConnectRequestError, match="time"):
        await read_connect_request(reader, 0.05)
