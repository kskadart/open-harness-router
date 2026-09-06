"""Process-wide activity monitor behind ``GET /dashboard``.

The monitor answers "what is the router doing right now" without a log
file: which requests are in flight, how many went to each provider and
model, how many tokens they used, what that costs at the configured prices,
and a ring buffer of the last events (tunnels, upstream errors, retries).

It is a single object per process on purpose -- uptime and pid are process
facts, and both listeners (the ASGI app and the forward-proxy) feed the
same picture -- reached through :meth:`Monitor.current`. Tests call
:meth:`Monitor.reset`.

Two feeds fill it:

- every routed ``/v1/messages`` and ``/v1/messages/count_tokens`` request
  is tracked from the routing decision to the last response byte
  (:meth:`Monitor.start_request` -> :class:`RequestTracker`), in both
  entrances; token usage is read off the Anthropic-format response on the
  way out (JSON body or SSE ``message_start``/``message_delta``), without
  buffering or changing a byte -- a gzip/deflate stream (what
  ``api.anthropic.com`` sends to a client that accepts it; passthrough
  relays it compressed) is inflated for the reading only;
- the structlog processor :func:`capture_for_monitor` copies the log events
  worth seeing on the dashboard (tunnels, protocol switches, retries,
  warnings and errors) into the same ring buffer, so the paths the
  forward-proxy relays without routing are visible too.
"""

from __future__ import annotations

import json
import os
import time
import zlib
from collections import deque
from collections.abc import AsyncGenerator, AsyncIterator, Mapping, MutableMapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Any

from const import MONITOR_EVENT_BUFFER, MONITOR_EVENT_TEXT_LIMIT, MONITOR_SSE_LINE_LIMIT
from providers.base import ProviderResult
from routing.schema import PricingCfg

# Paths the request tracker covers itself: their ``proxy_request`` log
# events would duplicate the tracked entries in the feed.
_TRACKED_PATHS = frozenset({"/v1/messages", "/v1/messages/count_tokens"})

# Informational events that belong in the feed even though they are not
# warnings: what the forward-proxy did with traffic the tracker never sees.
_FEED_EVENTS = frozenset({
    "startup",
    "proxy_startup",
    "shutdown",
    "entrypoint_shutdown",
    "proxy_request",
    "proxy_tunnel_closed",
    "proxy_protocol_switch",
    "passthrough_connect_retry",
    "context_window_clamp",
    "context_window_reject",
})

# Log fields never copied into the feed: bulky (the routing table) or
# already carried by the entry itself.
_SKIPPED_FIELDS = frozenset({"routes", "timestamp", "level", "event", "logger", "_record"})

_USAGE_FIELDS = (
    "input_tokens",
    "output_tokens",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
)

# Content encodings the stream scanner can inflate with the stdlib. A
# stream in any other encoding (``br``, ``zstd``) is relayed untouched and
# its usage stays unknown (zero).
_ZLIB_ENCODINGS = frozenset({"gzip", "x-gzip", "deflate"})
_PLAIN_ENCODINGS = frozenset({"", "identity"})


def _now_iso() -> str:
    """Return the current UTC time in the same ISO form the logs use."""
    return datetime.now(UTC).isoformat()


@dataclass(slots=True)
class Usage:
    """Token counts of one or many Anthropic-format responses.

    Attributes:
        input_tokens: uncached prompt tokens.
        output_tokens: completion tokens.
        cache_creation_input_tokens: prompt tokens written to the cache.
        cache_read_input_tokens: prompt tokens served from the cache.
    """

    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0

    @classmethod
    def from_payload(cls, payload: object) -> Usage:
        """Read a ``usage`` object; anything that is not an int counts as 0.

        Args:
            payload: the ``usage`` value of a response or SSE event.

        Returns:
            The counts found in it.
        """
        usage = cls()
        if isinstance(payload, dict):
            usage.update(payload)
        return usage

    def update(self, payload: dict[str, Any]) -> None:
        """Overwrite the counts present in ``payload``.

        SSE reports cumulative numbers (``message_start`` carries the prompt
        side, every ``message_delta`` the completion so far), so a field is
        assigned rather than added.

        Args:
            payload: a ``usage`` object of one SSE event.
        """
        for name in _USAGE_FIELDS:
            value = payload.get(name)
            if isinstance(value, int) and not isinstance(value, bool):
                setattr(self, name, value)

    def add(self, other: Usage) -> None:
        """Accumulate another response's counts into this one."""
        for name in _USAGE_FIELDS:
            setattr(self, name, getattr(self, name) + getattr(other, name))

    def total(self) -> int:
        """Return the sum of all four counts."""
        return sum(getattr(self, name) for name in _USAGE_FIELDS)

    def as_dict(self) -> dict[str, int]:
        """Return the counts as a JSON-ready mapping."""
        return {name: getattr(self, name) for name in _USAGE_FIELDS}


class _Inflater:
    """Incremental gzip/deflate decoder for the scanner's private copy.

    ``zlib.MAX_WBITS | 32`` lets zlib detect the gzip or zlib header
    itself. After the first decoding error the rest of the stream is
    ignored: the counts stay at what was read so far.
    """

    def __init__(self) -> None:
        """Prepare a fresh decoder."""
        self._decoder = zlib.decompressobj(zlib.MAX_WBITS | 32)
        self._broken = False

    def feed(self, chunk: bytes) -> bytes:
        """Return the plain bytes this compressed chunk yields."""
        if self._broken:
            return b""
        try:
            return self._decoder.decompress(chunk)
        except zlib.error:
            self._broken = True
            return b""


class UsageScanner:
    """Read the ``usage`` of one Anthropic-format response off its bytes.

    Handles both shapes the client can receive: a JSON body (unary) and an
    SSE stream, fed chunk by chunk with lines split anywhere. Neither path
    keeps the response: only the current incomplete line is buffered, and a
    line longer than ``MONITOR_SSE_LINE_LIMIT`` is dropped. A stream in a
    ``content-encoding`` the scanner cannot inflate is skipped altogether.
    """

    def __init__(self, content_encoding: str = "") -> None:
        """Start with empty counts and no pending line.

        Args:
            content_encoding: the stream's ``content-encoding`` header value.
        """
        self.usage = Usage()
        self._pending = b""
        encoding = content_encoding.strip().lower()
        self._inflater = _Inflater() if encoding in _ZLIB_ENCODINGS else None
        self.readable = encoding in _PLAIN_ENCODINGS or self._inflater is not None

    def feed_json(self, body: bytes) -> None:
        """Read the ``usage`` object of a complete JSON response body."""
        try:
            payload = json.loads(body)
        except ValueError:
            return
        if isinstance(payload, dict):
            self.usage = Usage.from_payload(payload.get("usage"))

    def feed_sse(self, chunk: bytes) -> None:
        """Consume the next chunk of an SSE stream, as sent to the client."""
        if not self.readable:
            return
        if self._inflater is not None:
            chunk = self._inflater.feed(chunk)
        data = self._pending + chunk
        *lines, self._pending = data.split(b"\n")
        if len(self._pending) > MONITOR_SSE_LINE_LIMIT:
            self._pending = b""
        for line in lines:
            self._line(line.rstrip(b"\r"))

    def _line(self, line: bytes) -> None:
        """Apply one complete SSE line."""
        if not line.startswith(b"data:"):
            return
        try:
            payload = json.loads(line[5:].strip())
        except ValueError:
            return
        if not isinstance(payload, dict):
            return
        event_type = payload.get("type")
        if event_type == "message_start":
            message = payload.get("message")
            usage = message.get("usage") if isinstance(message, dict) else None
        elif event_type == "message_delta":
            usage = payload.get("usage")
        else:
            return
        if isinstance(usage, dict):
            self.usage.update(usage)


@dataclass(slots=True)
class _Stats:
    """Counters of one bucket: the whole router, one provider, or one model."""

    requests: int = 0
    errors: int = 0
    priced_requests: int = 0
    duration_ms_total: float = 0.0
    usage: Usage = field(default_factory=Usage)
    cost_usd: float = 0.0

    def record(
        self, *, error: bool, duration_ms: float, usage: Usage, cost_usd: float | None
    ) -> None:
        """Fold one finished request into the counters."""
        self.requests += 1
        if error:
            self.errors += 1
        self.duration_ms_total += duration_ms
        self.usage.add(usage)
        if cost_usd is not None:
            self.priced_requests += 1
            self.cost_usd += cost_usd

    def as_dict(self) -> dict[str, Any]:
        """Return the counters as a JSON-ready mapping."""
        average = self.duration_ms_total / self.requests if self.requests else 0.0
        return {
            "requests": self.requests,
            "errors": self.errors,
            "avg_duration_ms": round(average, 1),
            "duration_ms_total": round(self.duration_ms_total, 1),
            "usage": self.usage.as_dict(),
            "tokens": self.usage.total(),
            "priced_requests": self.priced_requests,
            "cost_usd": round(self.cost_usd, 6),
        }


@dataclass(slots=True)
class _Active:
    """One request in flight."""

    request_id: int
    model: str
    provider: str
    endpoint: str
    started_monotonic: float
    started_at: str
    stream: bool = False


class RequestTracker:
    """Follows one routed request from the routing decision to the last byte.

    Created by :meth:`Monitor.start_request`; the handler calls
    :meth:`attach` with the provider's result (a streaming body is wrapped,
    a bytes body is read for its usage on the spot) or :meth:`fail` when
    the provider raised. Both finish the request exactly once.
    """

    def __init__(
        self,
        monitor: Monitor,
        active: _Active,
        pricing: PricingCfg | None,
    ) -> None:
        """Bind the tracker to its monitor and in-flight record."""
        self._monitor = monitor
        self._active = active
        self._pricing = pricing
        self._scanner: UsageScanner | None = None
        self._done = False

    @property
    def request_id(self) -> int:
        """Return the id of the tracked request."""
        return self._active.request_id

    def attach(self, result: ProviderResult) -> ProviderResult:
        """Account for the provider's result; wrap a streaming body.

        Args:
            result: what the provider returned.

        Returns:
            The same result, with a streaming body replaced by a wrapper
            that yields the identical chunks and finishes the request when
            the stream ends or breaks.
        """
        if isinstance(result.body, bytes):
            # A unary passthrough body is already decompressed by httpx (its
            # content-encoding header is dropped there) and a translated one
            # is built in the router: plain JSON either way.
            self._scanner = UsageScanner()
            if self._active.endpoint == "messages":
                self._scanner.feed_json(result.body)
            self._finish(status=result.status_code, error=None)
            return result
        self._active.stream = True
        self._scanner = UsageScanner(_content_encoding(result.headers))
        return replace(result, body=self._wrap(result.body, result.status_code))

    def fail(self, exc: BaseException) -> None:
        """Finish the request after the provider raised."""
        self._finish(status=None, error=type(exc).__name__)

    async def _wrap(self, body: AsyncIterator[bytes], status: int) -> AsyncGenerator[bytes]:
        """Yield the stream unchanged while reading its usage."""
        scanner = self._scanner
        assert scanner is not None  # noqa: S101 - set by attach() before wrapping
        try:
            async for chunk in body:
                scanner.feed_sse(chunk)
                yield chunk
        except BaseException as exc:
            self._finish(status=status, error=type(exc).__name__)
            raise
        else:
            self._finish(status=status, error=None)
        finally:
            if isinstance(body, AsyncGenerator):
                await body.aclose()

    def _finish(self, *, status: int | None, error: str | None) -> None:
        """Hand the outcome to the monitor once."""
        if self._done:
            return
        self._done = True
        usage = self._scanner.usage if self._scanner is not None else Usage()
        cost = (
            self._pricing.cost_usd(
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
                cache_write_tokens=usage.cache_creation_input_tokens,
                cache_read_tokens=usage.cache_read_input_tokens,
            )
            if self._pricing is not None
            else None
        )
        self._monitor.finish_request(
            self._active, status=status, error=error, usage=usage, cost_usd=cost
        )


class Monitor:
    """The router's live picture: in-flight requests, counters, last events."""

    _current: Monitor | None = None

    def __init__(self, *, event_buffer: int = MONITOR_EVENT_BUFFER) -> None:
        """Start an empty monitor for this process.

        Args:
            event_buffer: how many feed entries to keep.
        """
        self.pid = os.getpid()
        self.started_at = _now_iso()
        self._started_monotonic = time.monotonic()
        self._events: deque[dict[str, Any]] = deque(maxlen=event_buffer)
        self._totals = _Stats()
        self._providers: dict[str, _Stats] = {}
        self._models: dict[str, _Stats] = {}
        self._model_provider: dict[str, str] = {}
        self._active: dict[int, _Active] = {}
        self._next_id = 1

    @classmethod
    def current(cls) -> Monitor:
        """Return the process-wide monitor, creating it on first use."""
        if cls._current is None:
            cls._current = cls()
        return cls._current

    @classmethod
    def reset(cls) -> Monitor:
        """Replace the process-wide monitor with a fresh one (tests)."""
        cls._current = cls()
        return cls._current

    def start_request(
        self, *, model: str, provider: str, endpoint: str, pricing: PricingCfg | None
    ) -> RequestTracker:
        """Register a routed request as in flight.

        Args:
            model: the model id the client sent.
            provider: the name of the provider the route resolved to.
            endpoint: ``messages`` or ``count_tokens``.
            pricing: the route's effective prices, or ``None`` for tokens only.

        Returns:
            The tracker the handler finishes the request through.
        """
        active = _Active(
            request_id=self._next_id,
            model=model,
            provider=provider,
            endpoint=endpoint,
            started_monotonic=time.monotonic(),
            started_at=_now_iso(),
        )
        self._next_id += 1
        self._active[active.request_id] = active
        return RequestTracker(self, active, pricing)

    def finish_request(
        self,
        active: _Active,
        *,
        status: int | None,
        error: str | None,
        usage: Usage,
        cost_usd: float | None,
    ) -> None:
        """Move a request from in-flight to the counters and the feed."""
        self._active.pop(active.request_id, None)
        duration_ms = (time.monotonic() - active.started_monotonic) * 1000.0
        failed = error is not None or (status is not None and status >= 400)
        for bucket in (
            self._totals,
            self._providers.setdefault(active.provider, _Stats()),
            self._models.setdefault(active.model, _Stats()),
        ):
            bucket.record(error=failed, duration_ms=duration_ms, usage=usage, cost_usd=cost_usd)
        self._model_provider[active.model] = active.provider
        entry: dict[str, Any] = {
            "ts": _now_iso(),
            "kind": "request",
            "level": "error" if failed else "info",
            "endpoint": active.endpoint,
            "model": active.model,
            "provider": active.provider,
            "status": status,
            "stream": active.stream,
            "duration_ms": round(duration_ms, 1),
            "usage": usage.as_dict(),
        }
        if error is not None:
            entry["error"] = error
        if cost_usd is not None:
            entry["cost_usd"] = round(cost_usd, 6)
        self._events.append(entry)

    def record_log_event(self, event_dict: Mapping[str, Any]) -> None:
        """Copy a log event into the feed when it belongs there.

        Warnings and errors always do; of the informational events only the
        ones in ``_FEED_EVENTS``, and ``proxy_request`` only for paths the
        request tracker does not already cover.

        Args:
            event_dict: the structlog event dict, after the level and
                timestamp processors.
        """
        name = event_dict.get("event")
        level = str(event_dict.get("level", "info"))
        if not isinstance(name, str):
            return
        if level not in ("warning", "error", "critical") and name not in _FEED_EVENTS:
            return
        if name == "proxy_request" and event_dict.get("path") in _TRACKED_PATHS:
            return
        fields = {
            key: self._short(value)
            for key, value in event_dict.items()
            if key not in _SKIPPED_FIELDS and not key.startswith("_")
        }
        timestamp = event_dict.get("timestamp")
        self._events.append({
            "ts": timestamp if isinstance(timestamp, str) else _now_iso(),
            "kind": "log",
            "level": level,
            "event": name,
            "fields": fields,
        })

    @staticmethod
    def _short(value: object) -> object:
        """Keep a field JSON-ready and bounded."""
        if isinstance(value, bool | int | float) or value is None:
            return value
        text = value if isinstance(value, str) else repr(value)
        if len(text) > MONITOR_EVENT_TEXT_LIMIT:
            return text[: MONITOR_EVENT_TEXT_LIMIT - 1] + "…"
        return text

    def snapshot(self) -> dict[str, Any]:
        """Return the whole picture as a JSON-ready mapping.

        Returns:
            ``pid``, ``started_at``, ``uptime_s``, the ``totals`` counters,
            per-``providers`` and per-``models`` counters (each model with
            the provider it last resolved to), the ``active`` requests with
            their age, and the ``events`` feed, newest first.
        """
        now = time.monotonic()
        return {
            "pid": self.pid,
            "started_at": self.started_at,
            "uptime_s": round(now - self._started_monotonic, 1),
            "totals": self._totals.as_dict(),
            "providers": {name: stats.as_dict() for name, stats in self._providers.items()},
            "models": {
                model: {"provider": self._model_provider.get(model), **stats.as_dict()}
                for model, stats in self._models.items()
            },
            "active": [
                {
                    "id": item.request_id,
                    "model": item.model,
                    "provider": item.provider,
                    "endpoint": item.endpoint,
                    "stream": item.stream,
                    "started_at": item.started_at,
                    "age_s": round(now - item.started_monotonic, 1),
                }
                for item in self._active.values()
            ],
            "events": list(reversed(self._events)),
        }


def _content_encoding(headers: Mapping[str, str]) -> str:
    """Return the ``content-encoding`` header value, whatever its case."""
    for name, value in headers.items():
        if name.lower() == "content-encoding":
            return value
    return ""


def capture_for_monitor(
    _logger: Any, _method_name: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    """structlog processor: mirror feed-worthy events into the monitor.

    Placed after the level and timestamp processors in ``log._SHARED``, so
    it sees both; the event dict itself is returned untouched.
    """
    Monitor.current().record_log_event(event_dict)
    return event_dict
