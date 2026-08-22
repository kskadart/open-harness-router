"""Unit tests for the shared connection-establishment retry helper."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from services.retry import exponential_delays, retry_connect


class _RetryableError(Exception):
    """Failure type the tests mark as retryable."""


class _FatalError(Exception):
    """Failure type the tests treat as non-retryable."""


@pytest.fixture
def recorded_pauses(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Record the pauses the helper would sleep instead of waiting them out."""
    recorded: list[float] = []

    async def _record(delay: float) -> None:
        recorded.append(delay)

    monkeypatch.setattr("services.retry.asyncio.sleep", _record)
    return recorded


async def test_retry_connect_first_success_returns_without_pause(
    recorded_pauses: list[float],
) -> None:
    """A successful first attempt neither sleeps nor reports a retry."""
    attempt = AsyncMock(return_value="ok")
    on_retry = MagicMock()

    result = await retry_connect(
        attempt, delays=[1.0, 2.0], retryable=(_RetryableError,), on_retry=on_retry
    )

    assert result == "ok"
    assert attempt.await_count == 1
    assert recorded_pauses == []
    on_retry.assert_not_called()


async def test_retry_connect_retryable_failures_retry_until_success(
    recorded_pauses: list[float],
) -> None:
    """Retryable failures burn one pause each and report attempt numbers."""
    attempt = AsyncMock(side_effect=[_RetryableError("one"), _RetryableError("two"), "ok"])
    on_retry = MagicMock()

    result = await retry_connect(
        attempt, delays=[1.0, 2.0, 3.0], retryable=(_RetryableError,), on_retry=on_retry
    )

    assert result == "ok"
    assert attempt.await_count == 3
    assert recorded_pauses == [1.0, 2.0]
    assert [call.args[1:] for call in on_retry.call_args_list] == [(1, 1.0), (2, 2.0)]


async def test_retry_connect_non_retryable_failure_raises_immediately(
    recorded_pauses: list[float],
) -> None:
    """A non-retryable failure propagates before any pause is taken."""
    attempt = AsyncMock(side_effect=_FatalError("boom"))

    with pytest.raises(_FatalError):
        await retry_connect(
            attempt, delays=[1.0, 2.0], retryable=(_RetryableError,), on_retry=MagicMock()
        )

    assert attempt.await_count == 1
    assert recorded_pauses == []


async def test_retry_connect_exhausted_delays_raise_final_failure(
    recorded_pauses: list[float],
) -> None:
    """After the last pause the final attempt's failure propagates as is."""
    attempt = AsyncMock(
        side_effect=[_RetryableError("one"), _RetryableError("two"), _RetryableError("final")]
    )

    with pytest.raises(_RetryableError, match="final"):
        await retry_connect(
            attempt, delays=[1.0, 2.0], retryable=(_RetryableError,), on_retry=MagicMock()
        )

    assert attempt.await_count == 3
    assert recorded_pauses == [1.0, 2.0]


async def test_retry_connect_empty_delays_run_single_attempt(
    recorded_pauses: list[float],
) -> None:
    """Empty pause list means exactly one attempt and no sleeping."""
    attempt = AsyncMock(side_effect=_RetryableError("only"))

    with pytest.raises(_RetryableError):
        await retry_connect(
            attempt, delays=[], retryable=(_RetryableError,), on_retry=MagicMock()
        )

    assert attempt.await_count == 1
    assert recorded_pauses == []


def test_exponential_delays_double_up_to_the_cap() -> None:
    """Pauses double from the base and clamp at the ceiling."""
    assert exponential_delays(0.25, 2.0, 5) == [0.25, 0.5, 1.0, 2.0, 2.0]


def test_exponential_delays_zero_count_produces_no_pauses() -> None:
    """Zero or negative pause count disables retries entirely."""
    assert exponential_delays(0.25, 2.0, 0) == []
    assert exponential_delays(0.25, 2.0, -1) == []
