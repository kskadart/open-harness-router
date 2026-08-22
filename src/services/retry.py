"""Shared retry of connection establishment with growing pauses.

A retry is safe only while the connection is being established: no request
byte has reached the upstream and no response byte has reached the client,
so a repeat cannot duplicate work or splice two generations together.
Callers name the exception types that mark such a failure; everything else
propagates unchanged, as does the failure of the final attempt.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence


def exponential_delays(base_s: float, cap_s: float, count: int) -> list[float]:
    """Build a capped, exponentially doubling pause sequence.

    Args:
        base_s: pause before the first repeat; each next pause doubles.
        cap_s: ceiling a single pause never exceeds.
        count: number of pauses; zero or negative produces no pauses.

    Returns:
        Pauses in seconds in application order: base_s, base_s*2, ... cap_s.
    """
    return [min(base_s * (2**exponent), cap_s) for exponent in range(max(0, count))]


async def retry_connect[AttemptResult](
    attempt: Callable[[], Awaitable[AttemptResult]],
    delays: Sequence[float],
    retryable: tuple[type[BaseException], ...],
    on_retry: Callable[[BaseException, int, float], None],
) -> AttemptResult:
    """Run an attempt, repeating retryable failures over the given pauses.

    The number of pauses sets the number of repeats: the final attempt runs
    after the last pause and its failure propagates to the caller as is, as
    does any non-retryable failure.

    Args:
        attempt: coroutine factory performing a single attempt.
        delays: pauses in seconds between attempts; empty disables retries.
        retryable: exception types that mark a retryable failure.
        on_retry: called before each pause with the failure, the 1-based
            attempt number and the pause in seconds; the place for logging.

    Returns:
        The result of the first successful attempt.
    """
    for attempt_no, delay in enumerate(delays, start=1):
        try:
            return await attempt()
        except retryable as exc:
            on_retry(exc, attempt_no, delay)
            await asyncio.sleep(delay)
    return await attempt()
