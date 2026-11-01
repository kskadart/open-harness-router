"""Unit tests for ``services.monitor``: usage scanning, tracking, the feed."""

from __future__ import annotations

import gzip
import json
import zlib
from collections.abc import AsyncIterator

import pytest

from const import CAPABILITY_REJECTED_HEADER
from providers.base import ProviderResult
from routing.schema import PricingCfg
from services.monitor import Monitor, Usage, UsageScanner, capture_for_monitor

_PRICING = PricingCfg(input=3, output=15, cache_write=3.75, cache_read=0.3)
_JSON_HEADERS = {"content-type": "application/json"}
_SSE_HEADERS = {"content-type": "text/event-stream"}


def _sse(event: str, payload: dict[str, object]) -> bytes:
    """Frame one SSE event the way Anthropic sends it."""
    return f"event: {event}\ndata: {json.dumps(payload)}\n\n".encode()


_STREAM = (
    _sse(
        "message_start",
        {
            "type": "message_start",
            "message": {
                "usage": {
                    "input_tokens": 100,
                    "cache_creation_input_tokens": 20,
                    "cache_read_input_tokens": 300,
                    "output_tokens": 1,
                }
            },
        },
    )
    + _sse("content_block_delta", {"type": "content_block_delta", "delta": {"text": "hi"}})
    + _sse("message_delta", {"type": "message_delta", "usage": {"output_tokens": 7}})
    + _sse("message_delta", {"type": "message_delta", "usage": {"output_tokens": 42}})
    + _sse("message_stop", {"type": "message_stop"})
)


def test_usage_from_payload_reads_ints_only() -> None:
    """Missing, non-int and boolean fields count as zero."""
    payload = {"input_tokens": 5, "output_tokens": "7", "cache_read_input_tokens": True}
    usage = Usage.from_payload(payload)
    assert usage.as_dict() == {
        "input_tokens": 5,
        "output_tokens": 0,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 0,
    }
    assert Usage.from_payload(None).total() == 0


def test_scanner_reads_unary_json_usage() -> None:
    scanner = UsageScanner()
    scanner.feed_json(json.dumps({"usage": {"input_tokens": 3, "output_tokens": 5}}).encode())
    assert scanner.usage.input_tokens == 3
    assert scanner.usage.output_tokens == 5


def test_scanner_ignores_unparseable_bodies() -> None:
    scanner = UsageScanner()
    scanner.feed_json(b"not json")
    scanner.feed_json(b"[1, 2]")
    assert scanner.usage.total() == 0


@pytest.mark.parametrize("chunk_size", [1, 7, 64, len(_STREAM)])
def test_scanner_reads_sse_usage_across_any_chunking(chunk_size: int) -> None:
    """Lines split anywhere still yield the prompt side and the LAST completion count."""
    scanner = UsageScanner()
    for start in range(0, len(_STREAM), chunk_size):
        scanner.feed_sse(_STREAM[start : start + chunk_size])
    assert scanner.usage.as_dict() == {
        "input_tokens": 100,
        "output_tokens": 42,
        "cache_creation_input_tokens": 20,
        "cache_read_input_tokens": 300,
    }


@pytest.mark.parametrize(
    ("encoding", "compress"),
    [
        ("gzip", gzip.compress),
        ("x-gzip", gzip.compress),
        ("deflate", zlib.compress),
        ("GZIP", gzip.compress),
    ],
)
def test_scanner_inflates_a_compressed_stream_for_reading(
    encoding: str, compress: object
) -> None:
    """api.anthropic.com gzips SSE for a client that accepts it; passthrough relays it as is."""
    compressed = compress(_STREAM)  # type: ignore[operator]
    scanner = UsageScanner(encoding)
    assert scanner.readable is True
    for start in range(0, len(compressed), 5):
        scanner.feed_sse(compressed[start : start + 5])
    assert scanner.usage.input_tokens == 100
    assert scanner.usage.output_tokens == 42


def test_scanner_gives_up_on_a_corrupt_compressed_stream() -> None:
    scanner = UsageScanner("gzip")
    scanner.feed_sse(b"definitely not gzip")
    scanner.feed_sse(gzip.compress(_STREAM))
    assert scanner.usage.total() == 0


@pytest.mark.parametrize("encoding", ["br", "zstd", "something-new"])
def test_scanner_skips_encodings_it_cannot_inflate(encoding: str) -> None:
    scanner = UsageScanner(encoding)
    assert scanner.readable is False
    scanner.feed_sse(_STREAM)
    assert scanner.usage.total() == 0


def test_scanner_drops_an_oversized_pending_line() -> None:
    """A data line that never ends must not grow the buffer without bound."""
    scanner = UsageScanner()
    scanner.feed_sse(b"data: " + b"x" * (2 << 20))
    assert scanner._pending == b""  # noqa: SLF001


def test_tracker_records_a_unary_result_with_cost() -> None:
    monitor = Monitor()
    tracker = monitor.start_request(
        model="claude-opus-4-8", provider="anthropic", endpoint="messages", pricing=_PRICING
    )
    assert len(monitor.snapshot()["active"]) == 1

    body = json.dumps({"usage": {"input_tokens": 1_000_000, "output_tokens": 0}}).encode()
    result = tracker.attach(ProviderResult(200, _JSON_HEADERS, body))

    assert result.body is body
    snapshot = monitor.snapshot()
    assert snapshot["active"] == []
    assert snapshot["totals"]["requests"] == 1
    assert snapshot["totals"]["errors"] == 0
    assert snapshot["totals"]["usage"]["input_tokens"] == 1_000_000
    assert snapshot["totals"]["cost_usd"] == 3.0
    assert snapshot["totals"]["priced_requests"] == 1
    assert snapshot["providers"]["anthropic"]["requests"] == 1
    assert snapshot["models"]["claude-opus-4-8"]["provider"] == "anthropic"
    entry = snapshot["events"][0]
    assert entry["kind"] == "request"
    assert entry["status"] == 200
    assert entry["cost_usd"] == 3.0
    assert entry["stream"] is False


def test_tracker_without_pricing_counts_tokens_only() -> None:
    monitor = Monitor()
    tracker = monitor.start_request(model="m", provider="p", endpoint="messages", pricing=None)
    tracker.attach(ProviderResult(200, _JSON_HEADERS, b'{"usage": {"output_tokens": 9}}'))
    totals = monitor.snapshot()["totals"]
    assert totals["usage"]["output_tokens"] == 9
    assert totals["priced_requests"] == 0
    assert totals["cost_usd"] == 0.0
    assert "cost_usd" not in monitor.snapshot()["events"][0]


def test_tracker_inflates_a_compressed_unary_body_for_reading() -> None:
    """A passthrough JSON body keeps the upstream's gzip; the usage is still read."""
    monitor = Monitor()
    tracker = monitor.start_request(model="m", provider="p", endpoint="messages", pricing=None)
    usage = {"usage": {"input_tokens": 28154, "output_tokens": 9}}
    body = gzip.compress(json.dumps(usage).encode())
    headers = {"Content-Type": "application/json", "Content-Encoding": "gzip"}

    result = tracker.attach(ProviderResult(200, headers, body))

    assert result.body is body  # relayed compressed, as received
    assert monitor.snapshot()["totals"]["usage"]["input_tokens"] == 28154
    assert monitor.snapshot()["totals"]["usage"]["output_tokens"] == 9


def test_tracker_leaves_usage_unknown_for_a_unary_body_it_cannot_inflate() -> None:
    monitor = Monitor()
    tracker = monitor.start_request(model="m", provider="p", endpoint="messages", pricing=None)
    headers = {"content-type": "application/json", "content-encoding": "br"}

    tracker.attach(ProviderResult(200, headers, b"\x83#\x01\x00opaque brotli bytes"))

    snapshot = monitor.snapshot()
    assert snapshot["totals"]["requests"] == 1
    assert snapshot["totals"]["errors"] == 0
    assert snapshot["totals"]["usage"]["input_tokens"] == 0


def test_tracker_does_not_read_count_tokens_bodies_as_usage() -> None:
    monitor = Monitor()
    tracker = monitor.start_request(model="m", provider="p", endpoint="count_tokens", pricing=None)
    tracker.attach(ProviderResult(200, _JSON_HEADERS, b'{"input_tokens": 12345}'))
    assert monitor.snapshot()["totals"]["usage"]["input_tokens"] == 0
    assert monitor.snapshot()["totals"]["requests"] == 1


def test_tracker_counts_a_4xx_status_as_an_error() -> None:
    monitor = Monitor()
    tracker = monitor.start_request(model="m", provider="p", endpoint="messages", pricing=None)
    tracker.attach(ProviderResult(429, _JSON_HEADERS, b'{"type": "error"}'))
    assert monitor.snapshot()["totals"]["errors"] == 1
    assert monitor.snapshot()["events"][0]["level"] == "error"


def test_tracker_keeps_a_deliberate_rejection_out_of_the_error_count() -> None:
    """A 400 the provider answers on purpose (marker header) is protocol, not a failure."""
    monitor = Monitor()
    tracker = monitor.start_request(model="m", provider="p", endpoint="messages", pricing=None)
    headers = {**_JSON_HEADERS, CAPABILITY_REJECTED_HEADER.upper(): "thread_continue"}
    tracker.attach(ProviderResult(400, headers, b'{"type": "error"}'))
    snapshot = monitor.snapshot()
    assert snapshot["totals"]["requests"] == 1
    assert snapshot["totals"]["errors"] == 0
    entry = snapshot["events"][0]
    assert entry["level"] == "info"
    assert entry["status"] == 400
    assert entry["rejected"] == "thread_continue"


def test_tracker_fail_records_the_exception_type() -> None:
    monitor = Monitor()
    tracker = monitor.start_request(model="m", provider="p", endpoint="messages", pricing=None)
    tracker.fail(RuntimeError("boom"))
    tracker.fail(RuntimeError("again"))  # finishing twice is a no-op
    snapshot = monitor.snapshot()
    assert snapshot["totals"]["requests"] == 1
    assert snapshot["totals"]["errors"] == 1
    assert snapshot["events"][0]["error"] == "RuntimeError"
    assert snapshot["events"][0]["status"] is None


async def test_tracker_wraps_a_stream_unchanged_and_finishes_at_the_end() -> None:
    monitor = Monitor()
    tracker = monitor.start_request(
        model="claude-opus-4-8", provider="anthropic", endpoint="messages", pricing=_PRICING
    )
    chunks = [_STREAM[:50], _STREAM[50:200], _STREAM[200:]]

    async def upstream() -> AsyncIterator[bytes]:
        for chunk in chunks:
            yield chunk

    result = tracker.attach(ProviderResult(200, _SSE_HEADERS, upstream()))
    assert not isinstance(result.body, bytes)

    seen: list[bytes] = []
    async for chunk in result.body:
        # Still in flight while the stream is being read.
        assert len(monitor.snapshot()["active"]) == 1
        assert monitor.snapshot()["active"][0]["stream"] is True
        seen.append(chunk)

    assert seen == chunks
    snapshot = monitor.snapshot()
    assert snapshot["active"] == []
    assert snapshot["totals"]["usage"] == {
        "input_tokens": 100,
        "output_tokens": 42,
        "cache_creation_input_tokens": 20,
        "cache_read_input_tokens": 300,
    }
    expected_cost = (100 * 3 + 42 * 15 + 20 * 3.75 + 300 * 0.3) / 1_000_000
    assert snapshot["totals"]["cost_usd"] == pytest.approx(expected_cost)
    assert snapshot["events"][0]["stream"] is True


class _Clock:
    """A monotonic clock the test moves by hand."""

    def __init__(self, value: float = 1000.0) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


async def test_tracker_stamps_the_first_content_token_and_not_the_ping(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The first-token time is the first delta; a ping before it is not a token."""
    clock = _Clock()
    monkeypatch.setattr("services.monitor.time.monotonic", clock)
    monitor = Monitor()
    tracker = monitor.start_request(model="m", provider="p", endpoint="messages", pricing=None)
    before = _sse(
        "message_start", {"type": "message_start", "message": {"usage": {"input_tokens": 5}}}
    )
    before += _sse("ping", {"type": "ping"})
    after = _sse("content_block_delta", {"type": "content_block_delta", "delta": {"text": "hi"}})

    async def upstream() -> AsyncIterator[bytes]:
        clock.advance(0.5)
        yield before
        clock.advance(0.5)
        yield after

    result = tracker.attach(ProviderResult(200, _SSE_HEADERS, upstream()))
    async for _chunk in result.body:
        pass

    entry = monitor.snapshot()["events"][0]
    # The request started at 1000.0, the ping arrived at 1000.5 and the
    # first content token at 1001.0.
    assert entry["ttft_ms"] == 1000.0


def test_a_unary_result_records_no_first_token_timing() -> None:
    """A unary answer has no token to time, so the entry carries no timing."""
    monitor = Monitor()
    tracker = monitor.start_request(model="m", provider="p", endpoint="messages", pricing=None)
    tracker.attach(ProviderResult(200, _JSON_HEADERS, b'{"usage": {"output_tokens": 9}}'))
    assert "ttft_ms" not in monitor.snapshot()["events"][0]


def test_a_count_tokens_request_records_no_first_token_timing() -> None:
    """``count_tokens`` answers with a count, not with tokens."""
    monitor = Monitor()
    tracker = monitor.start_request(model="m", provider="p", endpoint="count_tokens", pricing=None)
    tracker.attach(ProviderResult(200, _JSON_HEADERS, b'{"input_tokens": 12}'))
    assert "ttft_ms" not in monitor.snapshot()["events"][0]


async def test_tracker_reads_usage_off_a_gzipped_stream_and_relays_it_compressed() -> None:
    monitor = Monitor()
    tracker = monitor.start_request(model="m", provider="p", endpoint="messages", pricing=None)
    compressed = gzip.compress(_STREAM)
    chunks = [compressed[:20], compressed[20:]]

    async def upstream() -> AsyncIterator[bytes]:
        for chunk in chunks:
            yield chunk

    headers = {"Content-Type": "text/event-stream", "Content-Encoding": "gzip"}
    result = tracker.attach(ProviderResult(200, headers, upstream()))
    assert not isinstance(result.body, bytes)
    seen = [chunk async for chunk in result.body]

    assert seen == chunks  # the client still gets the compressed bytes
    assert monitor.snapshot()["totals"]["usage"]["output_tokens"] == 42


async def test_tracker_records_a_stream_that_breaks_and_closes_the_upstream() -> None:
    monitor = Monitor()
    tracker = monitor.start_request(model="m", provider="p", endpoint="messages", pricing=None)
    closed = False

    first_event_end = _STREAM.index(b"\n\n") + 2

    async def upstream() -> AsyncIterator[bytes]:
        nonlocal closed
        try:
            yield _STREAM[:first_event_end]
            raise ConnectionResetError("upstream went away")
        finally:
            closed = True

    result = tracker.attach(ProviderResult(200, _SSE_HEADERS, upstream()))
    assert not isinstance(result.body, bytes)
    with pytest.raises(ConnectionResetError):
        async for _chunk in result.body:
            pass

    assert closed is True
    snapshot = monitor.snapshot()
    assert snapshot["active"] == []
    assert snapshot["totals"]["errors"] == 1
    assert snapshot["events"][0]["error"] == "ConnectionResetError"
    # The prompt side was read before the break.
    assert snapshot["totals"]["usage"]["input_tokens"] == 100


async def test_tracker_releases_a_stream_never_read_and_finishes_the_request() -> None:
    """A stream dropped before its first byte still reaches the provider's release.

    Closing the never-started wrapper alone runs none of its cleanup: the
    provider's upstream response stayed open and the request stayed in
    flight on the dashboard for good.
    """
    monitor = Monitor()
    tracker = monitor.start_request(model="m", provider="p", endpoint="messages", pricing=None)
    released: list[bool] = []

    async def upstream() -> AsyncIterator[bytes]:
        yield _STREAM

    async def release() -> None:
        released.append(True)

    result = tracker.attach(ProviderResult(200, _SSE_HEADERS, upstream(), release))
    await result.aclose()

    assert released == [True]
    snapshot = monitor.snapshot()
    assert snapshot["active"] == []
    assert snapshot["totals"]["errors"] == 1
    assert snapshot["events"][0]["error"] == "StreamNotStarted"


async def test_tracker_release_after_a_full_stream_keeps_it_a_success() -> None:
    monitor = Monitor()
    tracker = monitor.start_request(model="m", provider="p", endpoint="messages", pricing=None)

    async def upstream() -> AsyncIterator[bytes]:
        yield _STREAM

    result = tracker.attach(ProviderResult(200, _SSE_HEADERS, upstream()))
    assert not isinstance(result.body, bytes)
    _seen = [chunk async for chunk in result.body]
    await result.aclose()

    snapshot = monitor.snapshot()
    assert snapshot["totals"]["errors"] == 0
    assert "error" not in snapshot["events"][0]


def test_feed_keeps_warnings_and_selected_info_events_only() -> None:
    monitor = Monitor()
    monitor.record_log_event({"event": "route", "level": "info", "model": "x"})
    monitor.record_log_event({
        "event": "proxy_request", "level": "info", "path": "/v1/messages", "status": 200
    })
    monitor.record_log_event({
        "event": "proxy_request", "level": "info", "path": "/api/oauth/x", "status": 200,
        "timestamp": "2026-09-07T00:00:00Z",
    })
    monitor.record_log_event({
        "event": "proxy_tunnel_closed", "level": "info", "host": "claude.ai", "port": 443
    })
    monitor.record_log_event({
        "event": "anything", "level": "warning", "routes": [{"big": "table"}], "_private": 1,
        "detail": "x" * 1000,
    })
    monitor.record_log_event({"event": 42, "level": "error"})  # no event name: dropped

    events = monitor.snapshot()["events"]
    assert [e["event"] for e in events] == ["anything", "proxy_tunnel_closed", "proxy_request"]
    assert events[2]["ts"] == "2026-09-07T00:00:00Z"
    assert events[2]["fields"] == {"path": "/api/oauth/x", "status": 200}
    assert "routes" not in events[0]["fields"]
    assert "_private" not in events[0]["fields"]
    assert len(events[0]["fields"]["detail"]) == 300


def test_feed_is_a_ring_buffer() -> None:
    monitor = Monitor(event_buffer=3)
    for number in range(5):
        monitor.record_log_event({"event": "proxy_tunnel_closed", "level": "info", "n": number})
    assert [e["fields"]["n"] for e in monitor.snapshot()["events"]] == [4, 3, 2]


def test_processor_mirrors_into_the_current_monitor_and_returns_the_dict() -> None:
    monitor = Monitor.reset()
    event = {"event": "proxy_tunnel_closed", "level": "info", "host": "h"}
    assert capture_for_monitor(None, "info", event) is event
    assert monitor.snapshot()["events"][0]["fields"] == {"host": "h"}


def test_snapshot_has_the_process_facts() -> None:
    snapshot = Monitor().snapshot()
    assert snapshot["pid"] > 0
    assert snapshot["uptime_s"] >= 0
    assert snapshot["started_at"].endswith("+00:00")
    assert snapshot["totals"]["requests"] == 0
    assert snapshot["providers"] == {}
    assert snapshot["models"] == {}


class _SpyStore:
    """A stand-in store that counts records and can be made to raise."""

    def __init__(self, *, raises: bool = False) -> None:
        self.entries: list[dict[str, object]] = []
        self.raises = raises

    def record(self, entry: dict[str, object]) -> None:
        """Record the entry once, or raise when configured to."""
        self.entries.append(entry)
        if self.raises:
            raise RuntimeError("storage is down")


def test_finish_request_feeds_an_attached_store_once() -> None:
    """The finished request reaches the store exactly once, with the feed entry."""
    store = _SpyStore()
    monitor = Monitor()
    monitor.attach_store(store)  # type: ignore[arg-type]
    tracker = monitor.start_request(model="m", provider="p", endpoint="messages", pricing=None)
    tracker.attach(ProviderResult(200, _JSON_HEADERS, b'{"usage": {"input_tokens": 3}}'))

    assert monitor.store is store
    assert len(store.entries) == 1
    assert store.entries[0] is monitor.snapshot()["events"][0]


def test_finish_request_survives_a_store_that_raises() -> None:
    """A raising store must not break the request path; the monitor still counts."""
    store = _SpyStore(raises=True)
    monitor = Monitor(store=store)  # type: ignore[arg-type]
    tracker = monitor.start_request(model="m", provider="p", endpoint="messages", pricing=None)
    tracker.attach(ProviderResult(200, _JSON_HEADERS, b'{"usage": {"input_tokens": 3}}'))

    assert monitor.snapshot()["totals"]["requests"] == 1
    assert len(store.entries) == 1


def test_detaching_the_store_stops_recording() -> None:
    store = _SpyStore()
    monitor = Monitor(store=store)  # type: ignore[arg-type]
    monitor.attach_store(None)
    tracker = monitor.start_request(model="m", provider="p", endpoint="messages", pricing=None)
    tracker.attach(ProviderResult(200, _JSON_HEADERS, b'{"usage": {"input_tokens": 3}}'))
    assert monitor.store is None
    assert store.entries == []
