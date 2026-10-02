"""Riding out an upstream's output-token quota window.

Some OpenAI-compatible gateways meter generated (completion) tokens per
key over a sliding window of a few minutes. Once the window is spent they
answer every request with a status the client treats as final -- observed:
``422`` with ``"Превышен лимит completion-токенов: использовано 12487,
лимит 10000. Повторите попытку через 3 мин."`` -- and Claude Code ends the
turn or the subagent on the spot, although the very same request would
succeed a few minutes later.

The window belongs to the key, not to the request, so the module keeps one
:class:`QuotaGate` per provider: the first refusal closes the gate until
the hinted moment, and every request to that provider -- the refused one
and the ones that arrive meanwhile -- waits for it instead of hitting the
upstream and spending another refusal. The gate reopens one request at a
time, a short stagger apart, so a pack of parallel subagents does not wake
up together and drain the fresh window in the same second.

Waiting is bounded by ``ProviderCfg.quota_wait_max_s``: a refusal whose
window ends later than that, or a repeated refusal after the wait, is
answered with ``429 rate_limit_error`` and a ``retry-after`` header -- a
status the client retries by itself, unlike the gateway's ``422``. The
header never exceeds :data:`CLIENT_RETRY_AFTER_MAX_S`: measured 2026-10-02,
Claude Code retries a 429 with ``retry-after: 60`` exactly 60 s later, but
gives up at once on ``retry-after: 180`` (its SDK, like every Stainless SDK,
honors the header only up to 60 s). A 60 s pause also brings the retry
back while the gate may still be closed, where it waits in the router.
"""

from __future__ import annotations

import asyncio
import math
import re
import time
from collections.abc import Awaitable, Callable

from log import get_logger

logger = get_logger(__name__)

# Statuses a quota refusal arrives with. 429 is the standard one; 422 is
# what the corporate gateway uses. Any other status is never a quota
# refusal, whatever its text says.
QUOTA_STATUSES = frozenset({422, 429})

# The retry hint is what tells a quota refusal from a genuine 422 (a
# malformed request has no "try again in N minutes"). Russian and English
# wordings, seconds/minutes/hours, an integer or decimal amount.
_RETRY_HINT = re.compile(
    r"\b(?:через|in)\s+(?P<amount>\d+(?:[.,]\d+)?)\s*"
    r"(?P<unit>сек|с\b|seconds?|secs?|s\b|мин|minutes?|mins?|m\b|час|ч\b|hours?|h\b)",
    re.IGNORECASE,
)
# A hint is only trusted next to quota wording: "in 5 seconds" alone could
# be anything.
_QUOTA_WORDING = re.compile(r"\b(?:лимит|квот|limit|quota|rate\b)", re.IGNORECASE)
_UNIT_SECONDS = {
    "сек": 1, "с": 1, "second": 1, "seconds": 1, "sec": 1, "secs": 1, "s": 1,
    "мин": 60, "minute": 60, "minutes": 60, "min": 60, "mins": 60, "m": 60,
    "час": 3600, "ч": 3600, "hour": 3600, "hours": 3600, "h": 3600,
}

# Pause between two requests leaving a reopened gate. Long enough that the
# first request's own completion tokens start counting before the next one
# goes, short enough to be invisible next to a minutes-long window.
RELEASE_STAGGER_S = 2.0
# Added on top of the hinted moment: the gateway rounds its hint down to
# whole minutes, so leaving exactly on time would mostly earn a second
# refusal.
WINDOW_MARGIN_S = 5.0
# Ceiling of the retry-after the client is given; see the module docstring.
CLIENT_RETRY_AFTER_MAX_S = 60.0
# How often a waiting request checks whether its client is still there.
_CANCEL_POLL_S = 1.0


def quota_retry_after(status_code: int, message: str) -> float | None:
    """Recognize a quota refusal and return how long the window stays closed.

    Args:
        status_code: HTTP status of the upstream error.
        message: upstream error text (the SDK's rendering of the body).

    Returns:
        Seconds until the upstream asks to retry, or None when this is not
        a quota refusal with a usable hint.
    """
    if status_code not in QUOTA_STATUSES or not _QUOTA_WORDING.search(message):
        return None
    match = _RETRY_HINT.search(message)
    if match is None:
        return None
    unit = match.group("unit").lower()
    seconds_per_unit = _UNIT_SECONDS.get(unit) or _UNIT_SECONDS.get(unit.rstrip("s"))
    if seconds_per_unit is None:  # pragma: no cover - the regex admits only known units
        return None
    return float(match.group("amount").replace(",", ".")) * seconds_per_unit


def retry_after_header(seconds: float) -> str:
    """Render a wait as a ``retry-after`` value the client will honor.

    Whole seconds, never zero, never above :data:`CLIENT_RETRY_AFTER_MAX_S`.
    """
    return str(max(1, math.ceil(min(seconds, CLIENT_RETRY_AFTER_MAX_S))))


class QuotaGate:
    """Closed-until moment of one provider's quota window, shared by its requests."""

    def __init__(
        self,
        provider: str,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        """Create an open gate.

        Args:
            provider: provider name, for the log.
            clock: monotonic clock; replaced in tests.
            sleep: sleep coroutine; replaced in tests.
        """
        self._provider = provider
        self._clock = clock
        self._sleep = sleep
        self._closed_until = 0.0
        self._next_release = 0.0

    def remaining_s(self) -> float:
        """Seconds until the gate reopens; zero when it is open."""
        return max(0.0, self._closed_until - self._clock())

    def _queue_clear_at(self) -> float:
        """Moment the last claimed departure slot has passed.

        Until then the gate counts as closed even after the window ended:
        a request arriving in between must queue behind the staggered ones
        instead of overtaking them.
        """
        return max(self._closed_until, self._next_release - RELEASE_STAGGER_S)

    def close(self, retry_after_s: float) -> None:
        """Close the gate for a refusal's window; a later moment wins.

        Args:
            retry_after_s: the upstream's hint, in seconds.
        """
        until = self._clock() + retry_after_s + WINDOW_MARGIN_S
        if until > self._closed_until:
            self._closed_until = until
            logger.warning(
                "upstream_quota_window_closed",
                provider=self._provider,
                retry_after_s=round(retry_after_s, 1),
            )

    def _departure(self) -> float:
        """Claim the next free departure slot after the gate reopens."""
        slot = max(self._closed_until, self._next_release)
        self._next_release = slot + RELEASE_STAGGER_S
        return slot

    async def wait_turn(
        self, max_wait_s: float, is_cancelled: Callable[[], Awaitable[bool]]
    ) -> bool:
        """Wait for this request's slot behind a closed gate.

        An open gate with no queue returns immediately and claims no slot.
        Otherwise departure slots are handed out in arrival order, a stagger
        apart. A refusal that re-closes the gate while this request waits
        (the first request out was refused again) moves its slot behind the
        new window instead of letting it walk into another refusal.

        Args:
            max_wait_s: the longest this request may wait.
            is_cancelled: reports whether the client has gone away.

        Returns:
            True when the request may go now; False when the slot lies
            beyond ``max_wait_s`` or the client left -- the caller answers
            with 429 (or drops the request) without touching the upstream.
        """
        now = self._clock()
        if self._queue_clear_at() <= now:
            return True
        deadline = now + max_wait_s
        if max(self._closed_until, self._next_release) > deadline:
            return False
        departure = self._departure()
        logger.info(
            "upstream_quota_wait",
            provider=self._provider,
            wait_s=round(departure - now, 1),
        )
        while (left := departure - self._clock()) > 0 or self._closed_until > departure:
            if self._closed_until > departure:
                if max(self._closed_until, self._next_release) > deadline:
                    return False
                departure = self._departure()
                continue
            if await is_cancelled():
                return False
            await self._sleep(min(left, _CANCEL_POLL_S))
        return True
