"""Keep a translated SSE stream alive while nothing visible arrives.

The response converters emit an Anthropic event only when the upstream
sends something the client can see: text, a tool call, the terminal
events. A reasoning model streams nothing visible while it thinks (its
reasoning deltas are dropped), and a busy gateway can hold a request in
its queue for minutes; either way the client sees a silent stream, and
Claude Code aborts one that stays silent past its watchdogs (measured
2026-09-21: ~180 s) and then resends the whole conversation, which only
adds to the gateway's load. api.anthropic.com keeps its own streams alive
with ``ping`` events during thinking pauses, and the gateway protocol asks
a translating gateway to do the same; this module does it for the streams
the router produces.

The wrapper sits on the converter's output, so both sources of silence
look the same to it: no frame for ``interval_s``. The wait for the next
frame is never cancelled on a timeout -- cancelling it would tear down the
upstream iteration -- the pending step is kept and awaited again after the
ping goes out.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from collections.abc import AsyncGenerator, AsyncIterator

from const import STREAM_KEEPALIVE_INTERVAL_S, Constants
from log import get_logger

logger = get_logger(__name__)

# The frame the converters send at the start of every stream: valid at any
# point of an Anthropic stream, carries no content, counts as traffic.
PING_FRAME = (
    f"event: {Constants.EVENT_PING}\n"
    f"data: {json.dumps({'type': Constants.EVENT_PING})}\n\n"
)


async def with_keepalive(
    frames: AsyncIterator[str],
    *,
    interval_s: float = STREAM_KEEPALIVE_INTERVAL_S,
    provider: str = "",
) -> AsyncIterator[str]:
    """Relay SSE frames, inserting a ``ping`` whenever none arrived for ``interval_s``.

    Args:
        frames: the converter's SSE frames, one event per string.
        interval_s: the silence after which a ping goes out; the timer
            restarts after every frame and after every ping.
        provider: provider name for the log.

    Yields:
        The frames, unchanged and in order, with ping frames in the gaps.
        A stream that needed at least one ping is logged as
        ``stream_keepalive`` with the number of pings and the longest gap
        between two frames, so gateway stalls stay visible.
    """
    iterator = frames.__aiter__()
    pending: asyncio.Task[str] | None = None
    pings = 0
    longest_gap_s = 0.0
    waited_since = time.monotonic()
    try:
        while True:
            if pending is None:
                pending = asyncio.ensure_future(iterator.__anext__())
                waited_since = time.monotonic()
            done, _ = await asyncio.wait({pending}, timeout=interval_s)
            if not done:
                pings += 1
                yield PING_FRAME
                continue
            longest_gap_s = max(longest_gap_s, time.monotonic() - waited_since)
            try:
                frame = pending.result()
            except StopAsyncIteration:
                return
            finally:
                pending = None
            yield frame
    finally:
        if pending is not None and not pending.done():
            # Closed from the outside (the client went away) while a frame
            # was still awaited: stop the upstream iteration cleanly.
            pending.cancel()
            with contextlib.suppress(asyncio.CancelledError, StopAsyncIteration):
                await pending
        if isinstance(frames, AsyncGenerator):
            await frames.aclose()
        if pings:
            logger.info(
                "stream_keepalive",
                provider=provider,
                pings=pings,
                longest_gap_s=round(longest_gap_s, 1),
            )
