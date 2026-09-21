"""Unit tests for the stream keepalive: pings in the gaps, frames untouched.

The wrapper sits on the converter's output. A gap in that output -- the
upstream queued, or a reasoning model thinking with nothing visible to
forward -- must produce Anthropic ``ping`` frames on the client side, and
the frame that finally arrives must still be delivered: the wait for it is
never cancelled by a ping.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from typing import Any

import pytest

import services.keepalive as keepalive_module
from services.keepalive import PING_FRAME, with_keepalive

_INTERVAL = 0.05


class _LogRecorder:
    """Stand-in for the module logger that records structlog-style events.

    Used instead of ``structlog.testing.capture_logs`` for the reason given
    in ``test_openai_translate_context_window``: the lazy logger proxy keeps
    the processor chain an earlier test's ``setup_logging()`` gave it, so
    ``capture_logs`` would depend on test order.
    """

    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def __getattr__(self, level: str) -> Callable[..., None]:
        def record(event: str, **fields: Any) -> None:
            self.events.append({"event": event, "level": level, **fields})

        return record


@pytest.fixture
def keepalive_log(monkeypatch: pytest.MonkeyPatch) -> _LogRecorder:
    recorder = _LogRecorder()
    monkeypatch.setattr(keepalive_module, "logger", recorder)
    return recorder


async def _frames(*items: str | float) -> AsyncIterator[str]:
    """Yield strings; a float is a pause of that many seconds."""
    for item in items:
        if isinstance(item, float):
            await asyncio.sleep(item)
        else:
            yield item


async def test_frames_that_keep_coming_pass_through_untouched(
    keepalive_log: _LogRecorder,
) -> None:
    relayed = [f async for f in with_keepalive(_frames("a", "b", "c"), interval_s=_INTERVAL)]
    assert relayed == ["a", "b", "c"]
    assert keepalive_log.events == []  # nothing to report: no ping went out


async def test_a_gap_gets_pings_and_the_late_frame_still_arrives(
    keepalive_log: _LogRecorder,
) -> None:
    relayed = [
        f
        async for f in with_keepalive(
            _frames("a", 0.17, "b"), interval_s=_INTERVAL, provider="gateway"
        )
    ]

    pings = relayed.count(PING_FRAME)
    assert pings >= 2
    assert [f for f in relayed if f != PING_FRAME] == ["a", "b"]
    assert relayed[0] == "a"
    assert relayed[-1] == "b"

    (entry,) = keepalive_log.events
    assert entry["event"] == "stream_keepalive"
    assert entry["provider"] == "gateway"
    assert entry["pings"] == pings
    assert entry["longest_gap_s"] >= 0.1


async def test_a_silent_start_gets_pings_before_the_first_frame() -> None:
    relayed = [f async for f in with_keepalive(_frames(0.12, "a"), interval_s=_INTERVAL)]
    assert relayed[-1] == "a"
    assert relayed.count(PING_FRAME) >= 1


async def test_ping_frame_is_a_well_formed_anthropic_event() -> None:
    assert PING_FRAME.startswith("event: ping\n")
    assert PING_FRAME.endswith('data: {"type": "ping"}\n\n')


async def test_an_upstream_error_propagates_after_the_pings() -> None:
    async def failing() -> AsyncIterator[str]:
        yield "a"
        await asyncio.sleep(0.12)
        raise RuntimeError("upstream broke")

    seen: list[str] = []
    with pytest.raises(RuntimeError):
        async for f in with_keepalive(failing(), interval_s=_INTERVAL):
            seen.append(f)
    assert seen[0] == "a"
    assert PING_FRAME in seen


async def test_closing_the_wrapper_stops_the_upstream_iteration() -> None:
    closed = False

    async def slow() -> AsyncIterator[str]:
        nonlocal closed
        try:
            yield "a"
            await asyncio.sleep(10)
            yield "never"
        finally:
            closed = True

    wrapped = with_keepalive(slow(), interval_s=_INTERVAL)
    assert await wrapped.__anext__() == "a"
    assert await wrapped.__anext__() == PING_FRAME  # the upstream is mid-sleep
    await wrapped.aclose()

    assert closed is True
