"""Unit tests for riding out an upstream quota window (``services.upstream_quota``).

The gate runs on a fake clock and a fake sleep that advances it, so the
minutes-long windows of the real gateway cost nothing; the recorded sleeps
also show when each waiting request would have left.
"""

from __future__ import annotations

import asyncio

import pytest
import structlog.testing

from services import upstream_quota
from services.upstream_quota import QuotaGate, quota_retry_after, retry_after_header

_GATEWAY_REFUSAL = (
    "Error code: 422 - {'error': 'Превышен лимит completion-токенов: "
    "использовано 12487, лимит 10000. Повторите попытку через 3 мин.'}"
)


class FakeClock:
    """Monotonic clock moved only by the fake sleep."""

    def __init__(self) -> None:
        self.now = 1000.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds
        await asyncio.sleep(0)


async def _never_cancelled() -> bool:
    return False


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def gate(clock: FakeClock) -> QuotaGate:
    return QuotaGate("gateway", clock=clock, sleep=clock.sleep)


@pytest.mark.parametrize(
    ("status", "message", "expected"),
    [
        (422, _GATEWAY_REFUSAL, 180.0),
        (422, "Превышен лимит completion-токенов. Повторите попытку через 1 мин.", 60.0),
        (429, "Rate limit reached. Please try again in 24.754s", 24.754),
        (429, "quota exceeded, retry in 2 minutes", 120.0),
        (429, "Лимит запросов, повторите через 30 сек", 30.0),
    ],
)
def test_recognizes_quota_refusals(status: int, message: str, expected: float) -> None:
    """A quota status with quota wording and a retry hint yields the hinted wait."""
    assert quota_retry_after(status, message) == pytest.approx(expected)


@pytest.mark.parametrize(
    ("status", "message"),
    [
        (400, _GATEWAY_REFUSAL),
        (500, _GATEWAY_REFUSAL),
        (422, "Unprocessable: 'messages' must be a list"),
        (422, "Превышен лимит completion-токенов"),
        (429, "try again in 5 seconds"),
        (422, "Failed to generate the answer, retry in 5 seconds"),
        (429, "quota hit, wait within 3 minutes"),
    ],
)
def test_ignores_everything_else(status: int, message: str) -> None:
    """Wrong status, no hint, or a hint without quota wording: not a quota refusal."""
    assert quota_retry_after(status, message) is None


def test_retry_after_header_rounds_up_never_zero_and_capped() -> None:
    """Claude Code gives up on retry-after above 60 s, so the header never exceeds it."""
    assert retry_after_header(41.2) == "42"
    assert retry_after_header(0.0) == "1"
    assert retry_after_header(179.2) == "60"


async def test_open_gate_does_not_wait(gate: QuotaGate, clock: FakeClock) -> None:
    assert await gate.wait_turn(0, _never_cancelled) is True
    assert clock.sleeps == []


async def test_closed_gate_waits_window_plus_margin(gate: QuotaGate, clock: FakeClock) -> None:
    """A request behind a closed gate leaves after the hint and the safety margin."""
    gate.close(60)

    assert await gate.wait_turn(300, _never_cancelled) is True
    assert sum(clock.sleeps) == pytest.approx(60 + upstream_quota.WINDOW_MARGIN_S)


async def test_waiting_requests_leave_a_stagger_apart(gate: QuotaGate, clock: FakeClock) -> None:
    """Parallel waiters get departure slots one stagger apart, not the same instant."""
    gate.close(60)
    with structlog.testing.capture_logs() as logs:
        results = await asyncio.gather(
            *(gate.wait_turn(300, _never_cancelled) for _ in range(3))
        )

    assert results == [True, True, True]
    # The fake sleep moves one shared clock, so the waits are compared as
    # departure moments: the moment each waiter logged plus its wait.
    opened = 1000.0 + 60 + upstream_quota.WINDOW_MARGIN_S
    waits = [entry["wait_s"] for entry in logs if entry["event"] == "upstream_quota_wait"]
    assert waits[0] == pytest.approx(opened - 1000.0)
    assert len(waits) == 3


def test_departure_slots_are_staggered(gate: QuotaGate, clock: FakeClock) -> None:
    gate.close(60)
    slots = [gate._departure() for _ in range(3)]

    assert [later - earlier for earlier, later in zip(slots, slots[1:], strict=False)] == [
        pytest.approx(upstream_quota.RELEASE_STAGGER_S)
    ] * 2


async def test_window_beyond_budget_refuses_without_waiting(
    gate: QuotaGate, clock: FakeClock
) -> None:
    gate.close(600)

    assert await gate.wait_turn(240, _never_cancelled) is False
    assert clock.sleeps == []
    assert gate.remaining_s() == pytest.approx(600 + upstream_quota.WINDOW_MARGIN_S)


async def test_later_window_wins_earlier_does_not_shorten(gate: QuotaGate) -> None:
    gate.close(120)
    gate.close(30)

    assert gate.remaining_s() == pytest.approx(120 + upstream_quota.WINDOW_MARGIN_S)


async def test_client_leaving_stops_the_wait(gate: QuotaGate, clock: FakeClock) -> None:
    """A cancelled client is noticed within a poll interval, not at the window's end."""
    gate.close(120)
    polls = 0

    async def leaves_after_three_polls() -> bool:
        nonlocal polls
        polls += 1
        return polls > 3

    assert await gate.wait_turn(300, leaves_after_three_polls) is False
    assert sum(clock.sleeps) < 10


async def test_late_arrival_queues_behind_staggered_waiters(
    gate: QuotaGate, clock: FakeClock
) -> None:
    """After the window ends, a newcomer does not overtake requests still leaving."""
    gate.close(60)
    first = gate._departure()
    second = gate._departure()
    clock.now = first + 0.5  # window over, second waiter not gone yet

    assert await gate.wait_turn(300, _never_cancelled) is True
    assert clock.now >= second + upstream_quota.RELEASE_STAGGER_S


async def test_open_gate_after_queue_drained_does_not_wait(
    gate: QuotaGate, clock: FakeClock
) -> None:
    gate.close(60)
    last = gate._departure()
    clock.now = last + 1

    assert await gate.wait_turn(0, _never_cancelled) is True
    assert clock.sleeps == []


async def test_reclosed_gate_moves_waiter_behind_new_window(
    gate: QuotaGate, clock: FakeClock
) -> None:
    """The first request out refused again: a waiter does not leave on the old schedule."""
    gate.close(60)
    reopened = clock.now + 60 + upstream_quota.WINDOW_MARGIN_S
    reclosed = False

    async def reclose_once_window_ends() -> bool:
        nonlocal reclosed
        if not reclosed and clock.now >= reopened - 1:
            # The request released first was refused again: the gate closes
            # for a new window, counted from (almost) the old reopening.
            gate.close(120)
            reclosed = True
        return False

    assert await gate.wait_turn(600, reclose_once_window_ends) is True
    assert reclosed
    assert clock.now >= reopened - 1 + 120


async def test_reclosed_gate_beyond_budget_gives_up(gate: QuotaGate, clock: FakeClock) -> None:
    gate.close(60)
    reclosed = False

    async def reclose_far() -> bool:
        nonlocal reclosed
        if not reclosed and gate.remaining_s() < 30:
            gate.close(600)
            reclosed = True
        return False

    assert await gate.wait_turn(120, reclose_far) is False
