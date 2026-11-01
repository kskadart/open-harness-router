"""Unit tests for ``services.request_store``: persistence and window queries.

No network: the store only touches its own SQLite file, and the monitor's
plain ``finish_request`` path (a unary result) feeds it the same entry it
builds for its feed.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from providers.base import ProviderResult
from services.monitor import Monitor, _Stats
from services.request_store import RequestStore, resolve_window
from settings import StorageSettings

_JSON_HEADERS = {"content-type": "application/json"}

# The keys ``_Stats.as_dict()`` returns -- the shape the window must mirror.
_STATS_KEYS = {
    "requests",
    "errors",
    "avg_duration_ms",
    "duration_ms_total",
    "usage",
    "tokens",
    "priced_requests",
    "cost_usd",
}

_USAGE_KEYS = {
    "input_tokens",
    "output_tokens",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
}


def _entry(ts: str, *, usage: dict[str, int] | None = None, **extra: object) -> dict[str, object]:
    """Build a feed entry with a fixed timestamp and whatever the test needs."""
    entry: dict[str, object] = {"ts": ts, "kind": "request", "level": "info"}
    if usage is not None:
        entry["usage"] = usage
    entry.update(extra)
    return entry


def _iso(days_ago: float = 0.0) -> str:
    """Return an ISO-8601 UTC timestamp ``days_ago`` days in the past."""
    return (datetime.now(UTC) - timedelta(days=days_ago)).isoformat()


def _streamed(
    ts: str,
    *,
    model: str = "m",
    endpoint: str = "messages",
    stream: int = 1,
    output_tokens: int = 100,
    duration_ms: float = 1100.0,
    ttft_ms: float | None = 100.0,
) -> dict[str, object]:
    """Build the feed entry of a streamed answer with a known first token."""
    entry = _entry(
        ts,
        endpoint=endpoint,
        model=model,
        provider="p",
        status=200,
        stream=stream,
        duration_ms=duration_ms,
        usage={"input_tokens": 10, "output_tokens": output_tokens},
    )
    if ttft_ms is not None:
        entry["ttft_ms"] = ttft_ms
    return entry


@pytest.fixture
def store(tmp_path: Path) -> RequestStore:
    """An enabled store in a fresh temporary directory."""
    return RequestStore(db_path=tmp_path / "monitor.db", retention_days=365, window_days=30)


async def test_record_then_window_totals_mirrors_stats_shape(store: RequestStore) -> None:
    """The window returns the same keys as ``_Stats.as_dict()`` and the right sums."""
    store.record(_entry(
        _iso(),
        usage={"input_tokens": 100, "output_tokens": 20, "cache_read_input_tokens": 5},
        status=200,
        duration_ms=40.0,
        cost_usd=0.001,
    ))
    totals = await store.window_totals(days=30)

    assert set(totals) >= _STATS_KEYS
    assert set(totals["usage"]) == _USAGE_KEYS
    assert totals["requests"] == 1
    assert totals["errors"] == 0
    assert totals["usage"]["input_tokens"] == 100
    assert totals["usage"]["output_tokens"] == 20
    assert totals["usage"]["cache_read_input_tokens"] == 5
    assert totals["tokens"] == 125
    assert totals["duration_ms_total"] == 40.0
    assert totals["avg_duration_ms"] == 40.0
    assert totals["priced_requests"] == 1
    assert totals["cost_usd"] == 0.001
    assert totals["storage_error"] is None

    # The same shape the monitor's own buckets produce.
    assert set(_Stats().as_dict()) >= _STATS_KEYS


async def test_window_totals_excludes_rows_older_than_the_window(store: RequestStore) -> None:
    store.record(_entry(_iso(), usage={"input_tokens": 10}))
    store.record(_entry(_iso(40), usage={"input_tokens": 999}))
    totals = await store.window_totals(days=30)
    assert totals["requests"] == 1
    assert totals["usage"]["input_tokens"] == 10


async def test_a_rejected_row_is_not_an_error(store: RequestStore) -> None:
    """A deliberate capability rejection is protocol, like the monitor treats it."""
    store.record(_entry(_iso(), status=400, rejected="thread_continue"))
    store.record(_entry(_iso(), status=429))
    store.record(_entry(_iso(), error="RuntimeError", status=None))
    totals = await store.window_totals(days=30)
    assert totals["requests"] == 3
    assert totals["errors"] == 2


async def test_daily_series_groups_by_utc_day_oldest_first(store: RequestStore) -> None:
    day_one = (datetime.now(UTC) - timedelta(days=2)).strftime("%Y-%m-%dT%H:%M:%S+00:00")
    day_two = (datetime.now(UTC) - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%S+00:00")
    store.record(_entry(day_two, usage={"input_tokens": 5}, cost_usd=0.002))
    store.record(_entry(day_one, usage={"input_tokens": 1, "output_tokens": 2}))
    store.record(_entry(day_one, usage={"input_tokens": 3}, status=500))

    series = await store.daily_series(days=30)

    assert [row["day"] for row in series] == [day_one[:10], day_two[:10]]
    first, second = series
    assert first["requests"] == 2
    assert first["errors"] == 1
    assert first["usage"]["input_tokens"] == 4
    assert first["usage"]["output_tokens"] == 2
    assert first["priced_requests"] == 0
    assert second["requests"] == 1
    assert second["usage"]["input_tokens"] == 5
    assert second["cost_usd"] == 0.002
    assert second["priced_requests"] == 1


async def test_prune_deletes_only_rows_past_the_cutoff(store: RequestStore) -> None:
    store.record(_entry(_iso(), usage={"input_tokens": 1}))
    store.record(_entry(_iso(10), usage={"input_tokens": 2}))
    store.record(_entry(_iso(400), usage={"input_tokens": 3}))
    store.record(_entry(_iso(800), usage={"input_tokens": 4}))

    deleted = await store.prune(retention_days=365)

    assert deleted == 2
    remaining = await store.window_totals(days=365)
    assert remaining["requests"] == 2


async def test_a_fresh_store_prunes_nothing(store: RequestStore) -> None:
    assert await store.prune(retention_days=365) == 0


async def test_window_totals_of_an_empty_store_is_the_zero_shape(store: RequestStore) -> None:
    totals = await store.window_totals(days=30)
    assert totals["requests"] == 0
    assert totals["avg_duration_ms"] == 0.0
    assert totals["usage"]["input_tokens"] == 0
    assert totals["tokens"] == 0
    assert await store.daily_series(days=30) == []


async def test_record_swallows_a_failure_and_surfaces_it_in_the_snapshot(
    tmp_path: Path,
) -> None:
    """A store whose every write fails must not raise out of ``record``."""
    store = RequestStore(db_path=tmp_path / "monitor.db", retention_days=365, window_days=30)
    store.record(_entry(_iso(), usage={"input_tokens": 5}))
    # Read-only on the connection: the next write fails the way a read-only
    # or full disk would.
    assert store._connection is not None  # noqa: SLF001
    store._connection.execute("PRAGMA query_only=ON")  # noqa: SLF001
    store.record(_entry(_iso(), usage={"input_tokens": 7}))  # must not raise

    totals = await store.window_totals(days=30)
    assert totals["storage_error"] is not None
    # The window query itself still reads the rows written before.
    assert totals["usage"]["input_tokens"] == 5


async def test_record_never_raises_on_a_malformed_entry(store: RequestStore) -> None:
    """A field of the wrong type degrades to a missing column, not an exception."""
    store.record(
        _entry(_iso(), usage={"input_tokens": "not-an-int"}, status="oops", request_id="x")
    )
    totals = await store.window_totals(days=30)
    assert totals["requests"] == 1
    assert totals["usage"]["input_tokens"] == 0


async def test_a_recovered_store_clears_its_stale_error(tmp_path: Path) -> None:
    """`storage_error` reports the current state, not a failure already recovered from."""
    store = RequestStore(db_path=tmp_path / "m.db", retention_days=365, window_days=30)
    assert store._connection is not None  # noqa: SLF001
    store._connection.execute("PRAGMA query_only=ON")  # noqa: SLF001
    store.record(_entry(_iso()))
    assert (await store.window_totals(days=30))["storage_error"] is not None

    store._connection.execute("PRAGMA query_only=OFF")  # noqa: SLF001
    store.record(_entry(_iso(), usage={"input_tokens": 1}))
    totals = await store.window_totals(days=30)
    assert totals["storage_error"] is None
    assert totals["requests"] == 1


async def test_an_unopenable_path_leaves_the_store_inert(tmp_path: Path) -> None:
    """A bad path must not stop construction; ``record`` becomes a no-op."""
    blocked = tmp_path / "a-file"
    blocked.write_text("not a directory")
    store = RequestStore(
        db_path=blocked / "monitor.db", retention_days=365, window_days=30
    )
    store.record(_entry(_iso(), usage={"input_tokens": 5}))
    totals = await store.window_totals(days=30)
    assert totals["storage_error"] is not None
    await store.aclose()


async def test_monitor_feeds_the_attached_store_once(tmp_path: Path) -> None:
    """A finished request reaches the store exactly once (no double-record)."""
    store = RequestStore(db_path=tmp_path / "m.db", retention_days=365, window_days=30)
    monitor = Monitor(store=store)
    tracker = monitor.start_request(model="m", provider="p", endpoint="messages", pricing=None)
    body = b'{"usage": {"input_tokens": 11, "output_tokens": 4}}'
    tracker.attach(ProviderResult(200, _JSON_HEADERS, body))

    totals = await store.window_totals(days=30)
    assert totals["requests"] == 1
    assert totals["usage"]["input_tokens"] == 11
    assert totals["usage"]["output_tokens"] == 4
    await store.aclose()


def test_enabled_false_at_the_settings_level_leaves_the_monitor_storeless() -> None:
    """With storage disabled no store is attached and the monitor still works."""
    settings = StorageSettings(enabled=False)
    assert settings.enabled is False
    monitor = Monitor(store=None)
    tracker = monitor.start_request(model="m", provider="p", endpoint="messages", pricing=None)
    tracker.attach(ProviderResult(200, _JSON_HEADERS, b'{"usage": {"input_tokens": 1}}'))
    assert monitor.store is None
    assert monitor.snapshot()["totals"]["requests"] == 1


def test_storage_settings_defaults() -> None:
    settings = StorageSettings()
    assert settings.enabled is True
    assert settings.db_path == Path("./state/monitor.db")
    assert settings.retention_days == 365
    assert settings.window_days == 30


@pytest.mark.parametrize("field", ["retention_days", "window_days"])
def test_storage_settings_reject_non_positive_days(field: str) -> None:
    with pytest.raises(ValueError, match="at least 1 day"):
        StorageSettings(**{field: 0})


def test_storage_settings_reject_a_window_wider_than_retention() -> None:
    with pytest.raises(ValueError, match="must not exceed retention_days"):
        StorageSettings(retention_days=30, window_days=60)


def _now() -> datetime:
    """A fixed reference instant for the window tests."""
    return datetime(2026, 10, 30, 12, 0, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    ("window", "days", "bucket"),
    [("24h", 1, "hour"), ("7d", 7, "day"), ("30d", 30, "day"), ("all", 365, "day")],
)
def test_resolve_window_maps_each_name_to_its_range(
    window: str, days: int, bucket: str
) -> None:
    resolved = resolve_window(window=window, start=None, end=None, retention_days=365, now=_now())
    assert resolved.label == window
    assert resolved.bucket == bucket
    assert resolved.start == (_now() - timedelta(days=days)).isoformat()
    assert resolved.end is None


def test_resolve_window_defaults_to_a_month() -> None:
    resolved = resolve_window(window=None, start=None, end=None, retention_days=365, now=_now())
    assert resolved.label == "30d"


def test_a_day_window_is_read_hourly_because_a_daily_series_would_be_one_point() -> None:
    resolved = resolve_window(window="24h", start=None, end=None, retention_days=365, now=_now())
    assert resolved.bucket == "hour"


def test_resolve_window_custom_range_covers_the_whole_end_day() -> None:
    resolved = resolve_window(
        window="custom", start="2026-10-01", end="2026-10-03", retention_days=365, now=_now()
    )
    assert resolved.label == "custom"
    assert resolved.start == datetime(2026, 10, 1, tzinfo=UTC).isoformat()
    # A date-only end is the END of that day, so the 3rd is inside the range.
    assert resolved.end == datetime(2026, 10, 4, tzinfo=UTC).isoformat()


def test_resolve_window_clamps_an_older_start_to_the_retention_floor() -> None:
    """Asking beyond retention must not read a truncated window as whole."""
    resolved = resolve_window(
        window="custom", start="2020-01-01", end="2026-10-30", retention_days=30, now=_now()
    )
    assert resolved.start == (_now() - timedelta(days=30)).isoformat()


def test_resolve_window_rejects_a_malformed_range() -> None:
    with pytest.raises(ValueError, match="start must be before end"):
        resolve_window(
            window="custom", start="2026-10-10", end="2026-10-01", retention_days=365, now=_now()
        )
    with pytest.raises(ValueError, match="needs start"):
        resolve_window(
            window="custom", start=None, end="2026-10-01", retention_days=365, now=_now()
        )
    with pytest.raises(ValueError, match="invalid date"):
        resolve_window(
            window="custom", start="not-a-date", end="2026-10-01", retention_days=365, now=_now()
        )


def test_an_open_ended_custom_range_runs_through_now() -> None:
    """Start without an end is "from that day until now" -- a normal ask."""
    resolved = resolve_window(
        window="custom", start="2026-10-25", end=None, retention_days=365, now=_now()
    )
    assert resolved.start == datetime(2026, 10, 25, tzinfo=UTC).isoformat()
    assert resolved.end is None


def test_a_named_window_clamped_by_retention_says_so() -> None:
    """The label must not claim 30d over a week of rows."""
    resolved = resolve_window(window="30d", start=None, end=None, retention_days=7, now=_now())
    assert resolved.clamped is True
    assert resolved.start == (_now() - timedelta(days=7)).isoformat()

    roomy = resolve_window(window="30d", start=None, end=None, retention_days=365, now=_now())
    assert roomy.clamped is False


def test_a_custom_range_entirely_before_the_floor_is_refused() -> None:
    """Clamping the start must not silently invert the range."""
    with pytest.raises(ValueError, match="before the retention floor"):
        resolve_window(
            window="custom",
            start="2020-01-01",
            end="2021-01-01",
            retention_days=30,
            now=_now(),
        )


def test_custom_window_without_dates_says_start_is_missing() -> None:
    with pytest.raises(ValueError, match="custom range needs start"):
        resolve_window(window="custom", start=None, end=None, retention_days=365, now=_now())


def test_resolve_window_rejects_an_unknown_name() -> None:
    with pytest.raises(ValueError, match="unknown window"):
        resolve_window(window="90d", start=None, end=None, retention_days=365, now=_now())


async def test_grouped_totals_splits_the_window_by_provider_and_model(
    store: RequestStore,
) -> None:
    """The provider/model tables read the window, so the tiles and they agree."""
    store.record(
        _entry(_iso(), provider="gateway", model="m1", usage={"input_tokens": 10}, cost_usd=0.01)
    )
    store.record(
        _entry(_iso(), provider="gateway", model="m1", usage={"input_tokens": 5}, status=500)
    )
    store.record(_entry(_iso(), provider="opencode", model="m2", usage={"input_tokens": 7}))
    store.record(_entry(_iso(40), provider="gateway", model="m1", usage={"input_tokens": 999}))

    grouped = await store.grouped_totals(start=_iso(30), end=None)

    assert set(grouped) == {"providers", "models"}
    assert grouped["providers"]["gateway"]["requests"] == 2
    assert grouped["providers"]["gateway"]["errors"] == 1
    assert grouped["providers"]["gateway"]["usage"]["input_tokens"] == 15
    assert grouped["providers"]["gateway"]["cost_usd"] == 0.01
    assert grouped["providers"]["opencode"]["requests"] == 1
    assert grouped["models"]["m1"]["requests"] == 2
    assert grouped["models"]["m2"]["requests"] == 1

    # The per-provider counters must add up to the window totals.
    totals = await store.range_totals(start=_iso(30), end=None)
    assert sum(p["requests"] for p in grouped["providers"].values()) == totals["requests"]


async def test_grouped_totals_skips_rows_without_a_provider_or_model(
    store: RequestStore,
) -> None:
    """An unattributable row cannot become a table row."""
    store.record(_entry(_iso(), usage={"input_tokens": 1}))
    grouped = await store.grouped_totals(start=_iso(30), end=None)
    assert grouped["providers"] == {}
    assert grouped["models"] == {}


async def test_breakdown_splits_the_window_by_provider_and_model(store: RequestStore) -> None:
    """One row per (provider, model), with the counters of the window."""
    store.record(
        _entry(_iso(), provider="gateway", model="m1", usage={"input_tokens": 10}, cost_usd=0.01)
    )
    store.record(
        _entry(_iso(), provider="gateway", model="m1", usage={"input_tokens": 5}, status=500)
    )
    store.record(
        _entry(_iso(), provider="opencode", model="m2", usage={"input_tokens": 7})
    )
    store.record(
        _entry(_iso(40), provider="gateway", model="m1", usage={"input_tokens": 999})
    )

    rows = await store.breakdown(start=_iso(30), end=None)

    assert [(row["provider"], row["model"]) for row in rows] == [
        ("gateway", "m1"),
        ("opencode", "m2"),
    ]
    first = rows[0]
    assert first["requests"] == 2
    assert first["errors"] == 1
    assert first["avg_duration_ms"] == 0.0
    assert first["usage"]["input_tokens"] == 15
    assert first["tokens"] == 15
    assert first["priced_requests"] == 1
    assert first["cost_usd"] == 0.01
    assert rows[1]["requests"] == 1
    assert rows[1]["errors"] == 0


async def test_breakdown_keeps_a_row_without_a_provider_or_model(store: RequestStore) -> None:
    """An unattributable row is still a request: hiding it breaks the totals."""
    store.record(_entry(_iso(), usage={"input_tokens": 3}))

    rows = await store.breakdown(start=_iso(30), end=None)

    assert len(rows) == 1
    assert rows[0]["provider"] is None
    assert rows[0]["model"] is None
    assert rows[0]["requests"] == 1


async def test_breakdown_weights_the_throughput_by_tokens(store: RequestStore) -> None:
    """The group's TPS is the sum of the tokens over the sum of the windows."""
    # 100 tokens in 1 s (100 tok/s) and 100 tokens in 9 s (11.1 tok/s).
    store.record(
        _streamed(
            _iso(),
            model="m",
            output_tokens=100,
            duration_ms=1100.0,
            ttft_ms=100.0,
        )
    )
    store.record(
        _streamed(
            _iso(),
            model="m",
            output_tokens=100,
            duration_ms=9200.0,
            ttft_ms=200.0,
        )
    )

    rows = await store.breakdown(start=_iso(1), end=None)

    assert len(rows) == 1
    row = rows[0]
    assert row["tps"] == 20.0
    assert row["tps_requests"] == 2
    assert row["tps_output_tokens"] == 200
    assert row["tps_generation_ms"] == 10000.0
    assert row["avg_ttft_ms"] == 150.0


async def test_breakdown_has_no_throughput_without_a_qualifying_row(store: RequestStore) -> None:
    """A unary answer carries no throughput, and the row says so."""
    store.record(_streamed(_iso(), model="m", stream=0))

    rows = await store.breakdown(start=_iso(1), end=None)

    assert len(rows) == 1
    row = rows[0]
    assert row["tps"] is None
    assert row["tps_requests"] == 0
    assert row["tps_output_tokens"] == 0
    assert row["tps_generation_ms"] == 0.0
    assert row["avg_ttft_ms"] is None


async def test_breakdown_sorts_by_provider_then_by_requests(store: RequestStore) -> None:
    """Provider ascending, the busiest group first, a missing provider last."""
    store.record(_entry(_iso(), provider="opencode", model="m2"))
    store.record(_entry(_iso(), provider="gateway", model="m2"))
    store.record(_entry(_iso(), provider="gateway", model="m1"))
    store.record(_entry(_iso(), provider="gateway", model="m1"))
    store.record(_entry(_iso(), provider="gateway", model="m0"))
    store.record(_entry(_iso()))

    rows = await store.breakdown(start=_iso(30), end=None)

    assert [(row["provider"], row["model"]) for row in rows] == [
        ("gateway", "m1"),
        ("gateway", "m0"),
        ("gateway", "m2"),
        ("opencode", "m2"),
        (None, None),
    ]


async def test_first_ts_is_the_oldest_row_and_none_when_empty(store: RequestStore) -> None:
    """The page says "data since" -- so the store must say when it starts."""
    assert await store.first_ts() is None

    oldest = _iso(5)
    store.record(_entry(oldest))
    store.record(_entry(_iso(1)))

    assert await store.first_ts() == oldest


async def test_range_totals_and_series_honour_an_explicit_range(store: RequestStore) -> None:
    """A custom range reads exactly the rows inside it, endpoints excluded."""
    in_range = datetime(2026, 6, 15, 10, 0, tzinfo=UTC).isoformat()
    before = datetime(2026, 6, 14, 10, 0, tzinfo=UTC).isoformat()
    store.record(_entry(in_range, usage={"input_tokens": 7}))
    store.record(_entry(before, usage={"input_tokens": 999}))

    totals = await store.range_totals(
        start=datetime(2026, 6, 15, tzinfo=UTC).isoformat(),
        end=datetime(2026, 6, 16, tzinfo=UTC).isoformat(),
    )
    assert totals["requests"] == 1
    assert totals["usage"]["input_tokens"] == 7

    series = await store.range_series(
        start=datetime(2026, 6, 15, tzinfo=UTC).isoformat(),
        end=datetime(2026, 6, 16, tzinfo=UTC).isoformat(),
        bucket="day",
    )
    assert [row["day"] for row in series] == ["2026-06-15"]


async def test_range_series_can_bucket_by_hour(store: RequestStore) -> None:
    """A one-day window is read hourly; the bucket key says so."""
    at = lambda h, m: datetime(2026, 6, 15, h, m, tzinfo=UTC).isoformat()  # noqa: E731
    store.record(_entry(at(9, 30), usage={"input_tokens": 1}))
    store.record(_entry(at(9, 45), usage={"input_tokens": 2}))
    store.record(_entry(at(11, 0), usage={"input_tokens": 4}))

    series = await store.range_series(
        start=datetime(2026, 6, 15, tzinfo=UTC).isoformat(),
        end=datetime(2026, 6, 16, tzinfo=UTC).isoformat(),
        bucket="hour",
    )

    assert [row["bucket"] for row in series] == ["2026-06-15T09", "2026-06-15T11"]
    assert series[0]["requests"] == 2
    assert series[0]["usage"]["input_tokens"] == 3


async def test_tps_is_token_weighted_and_not_the_mean_of_the_ratios(store: RequestStore) -> None:
    """The sum of the tokens over the sum of the generation windows."""
    # 100 tokens in 1 s (100 tok/s) and 100 tokens in 9 s (11.1 tok/s).
    store.record(_streamed(_iso(), output_tokens=100, duration_ms=1100.0, ttft_ms=100.0))
    store.record(_streamed(_iso(), output_tokens=100, duration_ms=9200.0, ttft_ms=200.0))

    result = await store.tps(start=_iso(1), end=None, bucket="hour")

    model = result["by_model"]["m"]
    assert model["output_tokens"] == 200
    assert model["generation_ms"] == 10000.0
    assert model["requests"] == 2
    # 200 tokens over 10 s of generation: the mean of the ratios would say
    # 55.6 instead.
    assert model["tps"] == 20.0
    row = result["series"][0]
    assert row["tps"] == 20.0
    assert row["output_tokens"] == 200
    assert row["generation_ms"] == 10000.0
    assert row["requests"] == 2


async def test_tps_counts_only_streamed_requests_with_a_first_token(store: RequestStore) -> None:
    """A unary answer, a count, a row without timing and a burst add up to nothing."""
    store.record(_streamed(_iso()))  # the one that counts
    store.record(_streamed(_iso(), stream=0))  # a unary answer
    store.record(_streamed(_iso(), endpoint="count_tokens"))  # not a message
    store.record(_streamed(_iso(), ttft_ms=None))  # no first-token timing
    store.record(_streamed(_iso(), output_tokens=0))  # nothing was generated
    store.record(_streamed(_iso(), duration_ms=300.0, ttft_ms=100.0))  # 200 ms generated

    result = await store.tps(start=_iso(1), end=None, bucket="hour")

    assert result["by_model"]["m"]["requests"] == 1
    assert result["by_model"]["m"]["output_tokens"] == 100
    assert len(result["series"]) == 1


async def test_tps_lists_the_models_by_first_appearance_over_the_whole_log(
    store: RequestStore,
) -> None:
    """The order is the whole table's, not the window's: the colour must not move."""
    store.record(_streamed(_iso(40), model="old"))
    store.record(_streamed(_iso(), model="new"))

    result = await store.tps(start=_iso(1), end=None, bucket="day")

    assert result["models"] == ["old", "new"]
    assert list(result["by_model"]) == ["new"]


async def test_tps_models_skip_a_model_that_never_had_throughput(store: RequestStore) -> None:
    """A model seen only on unary or count_tokens calls must not take a colour slot."""
    store.record(_streamed(_iso(5), model="unary-only", stream=0))
    store.record(_streamed(_iso(4), model="counted-only", endpoint="count_tokens"))
    store.record(_streamed(_iso(), model="charted"))

    result = await store.tps(start=_iso(30), end=None, bucket="day")

    assert result["models"] == ["charted"]


async def test_tps_buckets_by_hour_or_by_day(store: RequestStore) -> None:
    """The bucket key says which granularity the series was read at."""
    at = lambda h, m: datetime(2026, 6, 15, h, m, tzinfo=UTC).isoformat()  # noqa: E731
    store.record(_streamed(at(9, 30)))
    store.record(_streamed(at(11, 0)))
    start = datetime(2026, 6, 15, tzinfo=UTC).isoformat()
    end = datetime(2026, 6, 16, tzinfo=UTC).isoformat()

    hourly = await store.tps(start=start, end=end, bucket="hour")
    daily = await store.tps(start=start, end=end, bucket="day")

    assert [row["bucket"] for row in hourly["series"]] == ["2026-06-15T09", "2026-06-15T11"]
    assert [row["bucket"] for row in daily["series"]] == ["2026-06-15"]


async def test_an_old_database_gains_the_first_token_column(tmp_path: Path) -> None:
    """A log written before the timing existed is migrated in place."""
    path = tmp_path / "old.db"
    connection = sqlite3.connect(path)
    connection.execute(
        "CREATE TABLE request_log ("
        "id INTEGER PRIMARY KEY, ts TEXT NOT NULL, request_id INTEGER, endpoint TEXT, "
        "model TEXT, provider TEXT, status INTEGER, error TEXT, stream INTEGER, "
        "rejected TEXT, duration_ms REAL, input_tokens INTEGER, output_tokens INTEGER, "
        "cache_write_tokens INTEGER, cache_read_tokens INTEGER, cost_usd REAL)"
    )
    connection.execute(
        "INSERT INTO request_log (ts, endpoint, model, provider, status, stream, "
        "duration_ms, output_tokens) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (_iso(), "messages", "old-model", "p", 200, 1, 1100.0, 100),
    )
    connection.commit()
    connection.close()

    store = RequestStore(db_path=path, retention_days=365, window_days=30)
    assert store._connection is not None  # noqa: SLF001
    columns = [row[1] for row in store._connection.execute("PRAGMA table_info(request_log)")]  # noqa: SLF001
    assert "ttft_ms" in columns

    # The row written before the migration is still counted ...
    totals = await store.range_totals(start=_iso(1), end=None)
    assert totals["requests"] == 1
    assert totals["usage"]["output_tokens"] == 100
    # ... and it has no timing, so it is no throughput and takes no colour slot.
    assert await store.tps(start=_iso(1), end=None, bucket="hour") == {
        "models": [],
        "series": [],
        "by_model": {},
    }

    store.record(_streamed(_iso(), model="new-model"))
    result = await store.tps(start=_iso(1), end=None, bucket="hour")
    assert result["by_model"]["new-model"]["requests"] == 1
    await store.aclose()
