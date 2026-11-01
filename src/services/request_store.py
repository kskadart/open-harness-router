"""Persistent request log behind the dashboard's windowed statistics.

The monitor (:mod:`services.monitor`) keeps its counters in memory, so a
restart wipes them. This module writes one row per finished routed request
to SQLite and answers the same counter shape over a rolling window, so the
dashboard can show traffic that outlives the process.

Retention is a full year, not the window. A row of the table below --
sixteen values plus the row header and the timestamp index entry -- measures
165 bytes (probe: 200,000 rows -> 31.5 MiB, ``page_size`` 4096). At a
modest ~3,000 requests/day that is ~172 MiB/year (~14 MiB/month); a light
~300/day is ~17 MiB/year, heavy ~10,000/day ~574 MiB/year. Because a year
is cheap at real volumes, the right mechanism is pruning old rows, not
sampling requests away: sampling would bias the very series the dashboard
shows, while a year of history answers "how did this month compare".

Failure policy: the proxy must never break because statistics storage
failed. A read-only or full disk, a corrupt database, a busy lock -- every
``record`` and ``prune`` failure is swallowed and logged, and the last one
is carried into the window snapshot (:meth:`RequestStore.window_totals`)
for the dashboard to show. Turning a successful model response into a 500
because a counter could not be written is never acceptable.

The same rows answer the throughput question: a streamed answer carries the
time of its first content token, and the generation speed is the output
tokens over the generation window that follows it -- not over the whole
request, whose prompt processing would dominate the duration (see
:meth:`RequestStore.tps`).

Threading: one connection, guarded by a :class:`threading.Lock`.
``record`` runs on the event loop thread (it is a single fast INSERT);
the read queries run in worker threads through ``asyncio.to_thread`` so
they never block the loop. One SQLite object may not be touched from two
threads at once, so ``check_same_thread=False`` lets the connection cross
threads and the lock serialises every statement.
"""

from __future__ import annotations

import asyncio
import sqlite3
import threading
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

from const import TPS_MIN_GENERATION_MS
from log import get_logger

logger = get_logger(__name__)

# The columns of one finished routed request, in insert order. ``ts`` is the
# ISO-8601 UTC string ``Monitor.finish_request`` already stamps; every other
# field is nullable because a failed request carries no usage and a log-only
# entry carries almost nothing.
_SCHEMA = """
CREATE TABLE IF NOT EXISTS request_log (
    id INTEGER PRIMARY KEY,
    ts TEXT NOT NULL,
    request_id INTEGER,
    endpoint TEXT,
    model TEXT,
    provider TEXT,
    status INTEGER,
    error TEXT,
    stream INTEGER,
    rejected TEXT,
    duration_ms REAL,
    input_tokens INTEGER,
    output_tokens INTEGER,
    cache_write_tokens INTEGER,
    cache_read_tokens INTEGER,
    cost_usd REAL,
    ttft_ms REAL
)
"""

# All window queries are range scans on the timestamp; the index is what
# keeps them cheap as the table grows to a year.
_INDEX = "CREATE INDEX IF NOT EXISTS request_log_ts ON request_log (ts)"

_COLUMNS = (
    "ts",
    "request_id",
    "endpoint",
    "model",
    "provider",
    "status",
    "error",
    "stream",
    "rejected",
    "duration_ms",
    "input_tokens",
    "output_tokens",
    "cache_write_tokens",
    "cache_read_tokens",
    "cost_usd",
    "ttft_ms",
)

# What the monitor counts as an error: a failed request (an ``error`` name)
# or a 4xx/5xx status -- except a deliberate capability rejection, which is
# protocol with the client, not a failure (see ``Monitor.finish_request``).
_ERROR_CONDITION = "(error IS NOT NULL OR (status >= 400 AND rejected IS NULL))"

# What one window (a range of timestamps) aggregates to. Built as a
# function of its WHERE clause so a rolling window (``ts >= ?``) and an
# explicit range (``ts >= ? AND ts < ?``) share one statement: the columns
# of a window total must not drift between the two callers.
_TOTALS_TEMPLATE = f"""
SELECT
    COUNT(*) AS requests,
    COALESCE(SUM(CASE WHEN {_ERROR_CONDITION} THEN 1 ELSE 0 END), 0) AS errors,
    COALESCE(SUM(duration_ms), 0.0) AS duration_ms_total,
    COALESCE(SUM(input_tokens), 0) AS input_tokens,
    COALESCE(SUM(output_tokens), 0) AS output_tokens,
    COALESCE(SUM(cache_write_tokens), 0) AS cache_creation_input_tokens,
    COALESCE(SUM(cache_read_tokens), 0) AS cache_read_input_tokens,
    COUNT(cost_usd) AS priced_requests,
    COALESCE(SUM(cost_usd), 0.0) AS cost_usd
FROM request_log
WHERE {{where}}
"""

# ISO-8601 UTC strings sort lexicographically the same as they compare in
# time, so a prefix of ``ts`` is the bucket it falls into: the first ten
# characters are the UTC day, the first thirteen the UTC hour. A "last 24
# hours" window is read as hours -- a day-granular series over one day
# would be a single point, which is not a series.
_BUCKET_EXPR = {"hour": "substr(ts, 1, 13)", "day": "substr(ts, 1, 10)"}

# Per-provider and per-model breakdown over the same window, so the
# dashboard's tables can show the selected period instead of the process
# lifetime. ``dim`` is the grouping column ("provider" or "model"); it is
# not user input -- the caller passes one of the fixed literals below.
_GROUP_TEMPLATE = f"""
SELECT
    {{dim}} AS name,
    COUNT(*) AS requests,
    COALESCE(SUM(CASE WHEN {_ERROR_CONDITION} THEN 1 ELSE 0 END), 0) AS errors,
    COALESCE(SUM(duration_ms), 0.0) AS duration_ms_total,
    COALESCE(SUM(input_tokens), 0) AS input_tokens,
    COALESCE(SUM(output_tokens), 0) AS output_tokens,
    COALESCE(SUM(cache_write_tokens), 0) AS cache_creation_input_tokens,
    COALESCE(SUM(cache_read_tokens), 0) AS cache_read_input_tokens,
    COUNT(cost_usd) AS priced_requests,
    COALESCE(SUM(cost_usd), 0.0) AS cost_usd
FROM request_log
WHERE {{where}}
GROUP BY name
ORDER BY name ASC
"""

_SERIES_TEMPLATE = f"""
SELECT
    {{bucket}} AS bucket,
    COUNT(*) AS requests,
    COALESCE(SUM(CASE WHEN {_ERROR_CONDITION} THEN 1 ELSE 0 END), 0) AS errors,
    COALESCE(SUM(input_tokens), 0) AS input_tokens,
    COALESCE(SUM(output_tokens), 0) AS output_tokens,
    COALESCE(SUM(cache_write_tokens), 0) AS cache_creation_input_tokens,
    COALESCE(SUM(cache_read_tokens), 0) AS cache_read_input_tokens,
    COUNT(cost_usd) AS priced_requests,
    COALESCE(SUM(cost_usd), 0.0) AS cost_usd
FROM request_log
WHERE {{where}}
GROUP BY bucket
ORDER BY bucket ASC
"""

# What a request must look like to carry a throughput figure: a streamed
# ``/v1/messages`` answer with a known first-token time, some output tokens
# and a generation window (the total duration minus the time to the first
# token) long enough to mean something. A gateway that buffers the stream
# delivers every token in one burst at the end, which would read as tens of
# thousands of tok/s; a generation window under ``TPS_MIN_GENERATION_MS``
# carries no throughput signal at all.
_TPS_QUALIFY = (
    "endpoint = 'messages' AND stream = 1 AND ttft_ms IS NOT NULL "
    "AND output_tokens > 0 AND model IS NOT NULL "
    "AND (duration_ms - ttft_ms) >= ?"
)

# The throughput series: one row per time bucket and model, token-weighted
# (the sum of the output tokens over the sum of the generation windows, not
# the mean of the per-request ratios).
_TPS_SERIES_TEMPLATE = f"""
SELECT
    {{bucket}} AS bucket,
    model,
    COALESCE(SUM(output_tokens), 0) AS output_tokens,
    COALESCE(SUM(duration_ms - ttft_ms), 0.0) AS generation_ms,
    COUNT(*) AS requests
FROM request_log
WHERE {{where}} AND {_TPS_QUALIFY}
GROUP BY bucket, model
ORDER BY bucket ASC, model ASC
"""

# One row per (provider, model) over the window: the dashboard's single
# Providers table shows the provider rows and their model rows beneath
# them. Unlike the per-provider and per-model groupings, a NULL name is
# kept: a row recorded without a provider or model is still a request,
# and hiding it would make the table disagree with the tiles.
_BREAKDOWN_TEMPLATE = f"""
SELECT
    provider,
    model,
    COUNT(*) AS requests,
    COALESCE(SUM(CASE WHEN {_ERROR_CONDITION} THEN 1 ELSE 0 END), 0) AS errors,
    COALESCE(SUM(duration_ms), 0.0) AS duration_ms_total,
    COALESCE(SUM(input_tokens), 0) AS input_tokens,
    COALESCE(SUM(output_tokens), 0) AS output_tokens,
    COALESCE(SUM(cache_write_tokens), 0) AS cache_creation_input_tokens,
    COALESCE(SUM(cache_read_tokens), 0) AS cache_read_input_tokens,
    COUNT(cost_usd) AS priced_requests,
    COALESCE(SUM(cost_usd), 0.0) AS cost_usd
FROM request_log
WHERE {{where}}
GROUP BY provider, model
"""

# The throughput of the same groups: only the rows that qualify carry
# it, and the figure is token-weighted over them (the sum of the output
# tokens over the sum of the generation windows).
_BREAKDOWN_TPS_TEMPLATE = f"""
SELECT
    provider,
    model,
    COUNT(*) AS tps_requests,
    COALESCE(SUM(output_tokens), 0) AS tps_output_tokens,
    COALESCE(SUM(duration_ms - ttft_ms), 0.0) AS tps_generation_ms,
    AVG(ttft_ms) AS avg_ttft_ms
FROM request_log
WHERE {{where}} AND {_TPS_QUALIFY}
GROUP BY provider, model
"""

# The same aggregation over the whole window, per model.
_TPS_MODEL_TEMPLATE = f"""
SELECT
    model,
    COALESCE(SUM(output_tokens), 0) AS output_tokens,
    COALESCE(SUM(duration_ms - ttft_ms), 0.0) AS generation_ms,
    COUNT(*) AS requests,
    AVG(ttft_ms) AS avg_ttft_ms
FROM request_log
WHERE {{where}} AND {_TPS_QUALIFY}
GROUP BY model
ORDER BY model ASC
"""

# Every model that has throughput at all, by first appearance over the WHOLE
# table, not over the window: a model's slot (and with it its colour on the
# chart) must not move when the window changes. Only qualifying rows count,
# so a model the log saw only on count_tokens or unary calls does not take
# one of the eight slots from a model that is actually charted.
_TPS_MODELS_TEMPLATE = f"""
SELECT model
FROM request_log
WHERE {_TPS_QUALIFY}
GROUP BY model
ORDER BY MIN(ts) ASC, model ASC
"""


def _breakdown_sort_key(entry: dict[str, Any]) -> tuple[Any, ...]:
    """Order the breakdown rows the way the dashboard shows them.

    Provider ascending (a row without a provider last), then the busiest
    group first, then the model name. A missing model sorts last, like a
    missing provider: an unattributable row is an anomaly, not the first
    thing to read.

    Args:
        entry: one breakdown row.

    Returns:
        The sort key of the row.
    """
    provider = entry["provider"]
    model = entry["model"]
    return (
        provider is None,
        provider or "",
        -entry["requests"],
        model is None,
        model or "",
    )


def _now_iso() -> str:
    """Return the current UTC time in the same ISO form the monitor stamps."""
    return datetime.now(UTC).isoformat()


def _range_where(start: str, end: str | None) -> tuple[str, tuple[object, ...]]:
    """Build the WHERE clause and its bound parameters for a time range.

    Kept as one function so the totals query and the series query can never
    disagree about which rows a window covers. The range is half-open --
    ``ts >= start`` and, when given, ``ts < end`` -- so adjoining windows do
    not double-count the boundary second.

    Args:
        start: inclusive lower bound on ``ts`` (ISO-8601 UTC).
        end: exclusive upper bound, or ``None`` for an open end.

    Returns:
        The SQL fragment and the parameters to bind to it.
    """
    if end is None:
        return "ts >= ?", (start,)
    return "ts >= ? AND ts < ?", (start, end)


# The windows the dashboard offers, mapped to how far back they reach.
# "all" is the retention window: every row the store keeps, which is what
# a reader means by "everything we have".
_WINDOW_DAYS: dict[str, int | None] = {
    "24h": 1,
    "7d": 7,
    "30d": 30,
    "all": None,
}

# The named windows plus ``custom``, in the order the dashboard shows them.
WINDOW_LABELS: tuple[str, ...] = (*_WINDOW_DAYS, "custom")


@dataclass(frozen=True)
class ResolvedWindow:
    """A dashboard window turned into the range the store queries.

    Attributes:
        label: the window the caller asked for, echoed back.
        start: inclusive lower bound on ``ts`` (ISO-8601 UTC).
        end: exclusive upper bound, or ``None`` for "up to now".
        bucket: ``"hour"`` for a window short enough that a daily series
            would be a single point, else ``"day"``.
        clamped: whether the retention floor cut the range short, so the
            caller can say so instead of labelling a week of rows "30d".
    """

    label: str
    start: str
    end: str | None
    bucket: Literal["hour", "day"]
    clamped: bool = False


def resolve_window(
    *,
    window: str | None,
    start: str | None,
    end: str | None,
    retention_days: int,
    now: datetime | None = None,
) -> ResolvedWindow:
    """Turn the dashboard's window request into the range to query.

    ``window`` names one of :data:`_WINDOW_DAYS` (``24h``, ``7d``, ``30d``,
    ``all``); ``start``/``end`` are an explicit custom range in
    ``YYYY-MM-DD`` (or full ISO-8601) and take precedence when given.
    ``start`` is required for a custom range, ``end`` is optional and means
    "through now". Both the named windows and the custom range are clamped
    to the retention: reading rows that have already been pruned would show
    a silently truncated window as if it were whole.

    Args:
        window: a named window, or ``None`` to use the default (``30d``).
        start: custom range lower bound, or ``None``.
        end: custom range upper bound, or ``None``.
        retention_days: how many days the store keeps; the floor for every
            range.
        now: the reference instant, for tests; defaults to the wall clock.

    Returns:
        The resolved window: its ``label``, the ``start`` and ``end``
        bounds, and the ``bucket`` its series should use.

    Raises:
        ValueError: when the custom range is malformed, or ``start`` is not
            before ``end``.
    """
    now = now or datetime.now(UTC)
    floor = now - timedelta(days=retention_days)
    if start is not None:
        start_dt = _parse_bound(start, end_of_day=False)
        # An omitted end means "through now" -- a range that has not closed
        # yet is a normal thing to ask for.
        end_dt = _parse_bound(end, end_of_day=True) if end is not None else now
        if start_dt >= end_dt:
            raise ValueError("start must be before end")
        # Clamp the START only, and re-check: a range that ends before the
        # retention floor would otherwise come out inverted (start after
        # end) and quietly return nothing while looking like a valid
        # window. Nothing is kept from before the floor, so the honest
        # answer is that the range cannot be served.
        clamped_start = max(start_dt, floor)
        if end is not None and clamped_start >= end_dt:
            raise ValueError(
                f"range ends before the retention floor "
                f"({floor.date()}); nothing that old is kept"
            )
        return ResolvedWindow(
            label="custom",
            start=clamped_start.isoformat(),
            end=None if end is None else end_dt.isoformat(),
            bucket=_bucket_for(end_dt - clamped_start),
            clamped=clamped_start > start_dt,
        )
    if end is not None:
        raise ValueError("a custom range needs start (end alone cannot say when it begins)")
    label = window or "30d"
    if label == "custom":
        raise ValueError("a custom range needs start (YYYY-MM-DD or ISO-8601)")
    if label not in _WINDOW_DAYS:
        raise ValueError(f"unknown window {label!r}; expected one of {sorted(_WINDOW_DAYS)}")
    days = _WINDOW_DAYS[label]
    if days is None:
        return ResolvedWindow(label=label, start=floor.isoformat(), end=None, bucket="day")
    start_dt = now - timedelta(days=days)
    clamped_start = max(start_dt, floor)
    return ResolvedWindow(
        label=label,
        start=clamped_start.isoformat(),
        end=None,
        bucket=_bucket_for(now - clamped_start),
        clamped=clamped_start > start_dt,
    )


def _bucket_for(span: timedelta) -> Literal["hour", "day"]:
    """Pick the series granularity for a window span.

    A window of a day or less is read as hours: a daily series over it
    would be a single point, which shows nothing about how the day went.

    Args:
        span: how much time the window covers.

    Returns:
        ``"hour"`` for a span of at most a day, else ``"day"``.
    """
    return "hour" if span <= timedelta(days=1) else "day"


def _parse_bound(value: str, *, end_of_day: bool) -> datetime:
    """Parse one custom-range bound into an aware UTC datetime.

    A date alone (``2026-10-01``) is the natural thing to type into the
    dashboard's date fields; it means the start of that day, or -- for the
    upper bound -- its very end, so a single-day range covers the day
    rather than an empty interval.

    Args:
        value: ``YYYY-MM-DD`` or a full ISO-8601 timestamp.
        end_of_day: extend a date-only value to the end of that day.

    Returns:
        The bound as an aware UTC datetime.

    Raises:
        ValueError: when the value is not a date or a timestamp.
    """
    text = value.strip()
    try:
        if len(text) == 10:
            day = datetime.strptime(text, "%Y-%m-%d").replace(tzinfo=UTC)
            return day + timedelta(days=1) if end_of_day else day
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"invalid date {value!r}; expected YYYY-MM-DD or ISO-8601") from exc
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _as_int(value: object) -> int | None:
    """Return ``value`` as an int column value, or None when it is not one.

    A non-numeric value is None rather than an exception: the entry comes
    from the monitor's feed, and a malformed field there must degrade to a
    missing column, not break the record path.

    Args:
        value: the raw field from a feed entry.

    Returns:
        The value as an int, or None when it cannot be read as one.
    """
    if value is None or isinstance(value, bool):
        return None if value is None else int(value)
    if isinstance(value, int | float):
        return int(value)
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return None
    return None


def _optional(value: object) -> object:
    """Return ``value`` unchanged unless it is a type SQLite cannot bind."""
    if value is None or isinstance(value, bool | int | float | str):
        return value
    return str(value)


def _tps(output_tokens: int, generation_ms: float) -> float:
    """Return the token-weighted throughput of a group of requests.

    The sum of the output tokens over the sum of the generation windows --
    never the mean of the per-request ratios, which would let one slow
    answer weigh as much as a fast one that generated ten times the tokens.

    Args:
        output_tokens: the summed output tokens of the group.
        generation_ms: the summed generation time of the group, in ms.

    Returns:
        The tokens per second, to two decimals (0.0 without a generation
        time to divide by).
    """
    if generation_ms <= 0:
        return 0.0
    return round(output_tokens * 1000.0 / generation_ms, 2)


class RequestStore:
    """SQLite log of finished requests, queried over a rolling window.

    One instance per process. Construction never raises: a database it
    cannot open leaves the store inert (``record`` becomes a no-op) and the
    failure visible through :meth:`window_totals`, so a bad path cannot stop
    the router from starting.
    """

    def __init__(self, *, db_path: Path, retention_days: int, window_days: int) -> None:
        """Open the database and create the table if it is missing.

        Args:
            db_path: path to the SQLite file; its parent directory is
                created when missing.
            retention_days: how many days of rows to keep (see ``prune``);
                stored for reference.
            window_days: the default rolling window; stored for reference.
        """
        self.retention_days = retention_days
        self.window_days = window_days
        self.db_path = db_path
        self._lock = threading.Lock()
        self._last_error: str | None = None
        self._connection: sqlite3.Connection | None = None
        try:
            db_path.parent.mkdir(parents=True, exist_ok=True)
            # One connection shared across threads: the single fast INSERT
            # runs on the loop thread, the reads run in worker threads via
            # asyncio.to_thread. check_same_thread=False permits that, and
            # the lock serialises every use of the connection.
            connection = sqlite3.connect(db_path, check_same_thread=False)
            # WAL lets reads proceed while a write is in flight;
            # synchronous=NORMAL is the durable-enough pairing for WAL and
            # keeps the per-insert cost in the microseconds.
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=NORMAL")
            connection.execute(_SCHEMA)
            self._migrate(connection)
            connection.execute(_INDEX)
            connection.commit()
        except (OSError, sqlite3.Error) as exc:
            # OSError covers a path whose parent is a file or is not
            # writable; sqlite3.Error covers a corrupt or locked database.
            self._last_error = f"{type(exc).__name__}: {exc}"
            logger.warning(
                "request_store_unavailable", db_path=str(db_path), error=self._last_error
            )
            return
        self._connection = connection

    @staticmethod
    def _migrate(connection: sqlite3.Connection) -> None:
        """Bring a database written by an older version up to date.

        The table is created with ``CREATE TABLE IF NOT EXISTS``, so a file
        that already exists keeps the columns it has; a column added later
        (the first-token timing) is therefore missing there. It is added
        with an ``ALTER TABLE``: the rows already written stay as they are,
        with no timing in them, and only the new rows carry one.

        Args:
            connection: the open connection to migrate.
        """
        columns = {row[1] for row in connection.execute("PRAGMA table_info(request_log)")}
        if "ttft_ms" not in columns:
            connection.execute("ALTER TABLE request_log ADD COLUMN ttft_ms REAL")

    def record(self, entry: Mapping[str, Any]) -> None:
        """Insert one finished request from the monitor's feed entry.

        The entry is exactly the dict ``Monitor.finish_request`` already
        builds for its event feed -- no second shape is created. Missing
        keys and a nested ``usage`` object are tolerated, the token counts
        read out of ``usage``. Every exception is swallowed and logged: a
        storage failure must not turn a successful model response into an
        error.

        Args:
            entry: the monitor's feed entry for the finished request.
        """
        connection = self._connection
        if connection is None:
            return
        usage = entry.get("usage")
        if not isinstance(usage, Mapping):
            usage = {}
        ts = entry.get("ts")
        # The whole body is guarded, values included: the docstring promises
        # that no exception escapes, and building the tuple can raise on a
        # value SQLite would not take.
        try:
            values = (
                ts if isinstance(ts, str) else _now_iso(),
                _as_int(entry.get("request_id")),
                _optional(entry.get("endpoint")),
                _optional(entry.get("model")),
                _optional(entry.get("provider")),
                _as_int(entry.get("status")),
                _optional(entry.get("error")),
                _as_int(entry.get("stream")),
                _optional(entry.get("rejected")),
                _optional(entry.get("duration_ms")),
                _as_int(usage.get("input_tokens")),
                _as_int(usage.get("output_tokens")),
                _as_int(usage.get("cache_creation_input_tokens")),
                _as_int(usage.get("cache_read_input_tokens")),
                _optional(entry.get("cost_usd")),
                _optional(entry.get("ttft_ms")),
            )
            placeholders = ", ".join("?" for _ in _COLUMNS)
            sql = f"INSERT INTO request_log ({', '.join(_COLUMNS)}) VALUES ({placeholders})"
            with self._lock:
                connection.execute(sql, values)
                connection.commit()
        except Exception as exc:  # noqa: BLE001 - storage must never break a request
            self._last_error = f"{type(exc).__name__}: {exc}"
            logger.warning("request_store_record_failed", error=self._last_error)
        else:
            # A successful write clears a stale error, so the dashboard stops
            # reporting a failure that has already been recovered from.
            self._last_error = None

    async def window_totals(self, *, days: int) -> dict[str, Any]:
        """Aggregate the counters of the last ``days`` days.

        The shape mirrors ``_Stats.as_dict()`` exactly, plus a
        ``storage_error`` key carrying the last storage failure (or None)
        for the dashboard to show.

        Args:
            days: how many days back the window reaches, from now.

        Returns:
            The counters over the window: ``requests``, ``errors``,
            ``avg_duration_ms``, ``duration_ms_total``, ``usage`` (the four
            token keys), ``tokens``, ``priced_requests``, ``cost_usd`` and
            ``storage_error``.
        """
        cutoff = (datetime.now(UTC) - timedelta(days=days)).isoformat()
        return await self.range_totals(start=cutoff, end=None)

    async def range_totals(self, *, start: str, end: str | None) -> dict[str, Any]:
        """Aggregate the counters of the half-open range ``[start, end)``.

        The same shape :meth:`window_totals` returns. Both bounds are
        ISO-8601 UTC strings; ``end`` of ``None`` means "up to now", which
        is what the rolling windows use.

        Args:
            start: inclusive lower bound on ``ts``.
            end: exclusive upper bound on ``ts``, or ``None`` for no upper
                bound.

        Returns:
            The counters over the range, in the ``window_totals`` shape.
        """
        where, params = _range_where(start, end)
        rows = await asyncio.to_thread(
            self._query, _TOTALS_TEMPLATE.format(where=where), params
        )
        return self._totals_from_row(rows[0] if rows else None)

    async def grouped_totals(self, *, start: str, end: str | None) -> dict[str, dict[str, Any]]:
        """Aggregate the counters per provider and per model over a range.

        Feeds the dashboard's Providers and Models tables for the selected
        window, so those tables read the same period as the tiles above
        them rather than the process lifetime.

        Args:
            start: inclusive lower bound on ``ts`` (ISO-8601 UTC).
            end: exclusive upper bound, or ``None`` for no upper bound.

        Returns:
            ``{"providers": {name: counters}, "models": {name: counters}}``,
            each counters dict in the ``_Stats.as_dict()`` shape. Rows with
            a NULL name are skipped: an entry without a provider or model
            cannot be attributed to a table row.
        """
        where, params = _range_where(start, end)
        providers = await asyncio.to_thread(
            self._query, _GROUP_TEMPLATE.format(dim="provider", where=where), params
        )
        models = await asyncio.to_thread(
            self._query, _GROUP_TEMPLATE.format(dim="model", where=where), params
        )
        return {
            "providers": {
                row[0]: self._grouped_from_row(row) for row in providers if row[0] is not None
            },
            "models": {
                row[0]: self._grouped_from_row(row) for row in models if row[0] is not None
            },
        }

    async def breakdown(self, *, start: str, end: str | None) -> list[dict[str, Any]]:
        """Aggregate the counters per (provider, model) over a range.

        The dashboard's single Providers table: a provider row with its
        model rows beneath it. The counters are the ``grouped_totals``
        ones, and the throughput of the group comes along -- token-weighted
        over the rows that qualify, exactly as ``tps`` computes it.

        A row whose provider or model is missing is kept (as a NULL
        name): it is still a request, and dropping it would make the
        table disagree with the tiles above it.

        Args:
            start: inclusive lower bound on ``ts`` (ISO-8601 UTC).
            end: exclusive upper bound, or ``None`` for no upper bound.

        Returns:
            One entry per (provider, model), sorted by provider ascending
            (a missing provider last), then by requests descending, then
            by model. Each entry carries ``provider``, ``model``,
            ``requests``, ``errors``, ``avg_duration_ms``, ``usage``,
            ``tokens``, ``priced_requests`` and ``cost_usd``, plus the
            throughput of the group: ``tps`` (None when no row of the
            group qualifies), ``tps_requests``, ``tps_output_tokens``,
            ``tps_generation_ms`` and ``avg_ttft_ms`` (None when no row
            of the group qualifies).
        """
        where, params = _range_where(start, end)
        rows = await asyncio.to_thread(
            self._query, _BREAKDOWN_TEMPLATE.format(where=where), params
        )
        tps_rows = await asyncio.to_thread(
            self._query,
            _BREAKDOWN_TPS_TEMPLATE.format(where=where),
            (*params, TPS_MIN_GENERATION_MS),
        )
        throughput = {(row[0], row[1]): row for row in tps_rows}
        entries = []
        for row in rows:
            counters = self._totals_from_row(row[2:])
            entry: dict[str, Any] = {
                "provider": row[0],
                "model": row[1],
                "requests": counters["requests"],
                "errors": counters["errors"],
                "avg_duration_ms": counters["avg_duration_ms"],
                "usage": counters["usage"],
                "tokens": counters["tokens"],
                "priced_requests": counters["priced_requests"],
                "cost_usd": counters["cost_usd"],
                "tps": None,
                "tps_requests": 0,
                "tps_output_tokens": 0,
                "tps_generation_ms": 0.0,
                "avg_ttft_ms": None,
            }
            tps_row = throughput.get((row[0], row[1]))
            if tps_row is not None:
                output_tokens = int(tps_row[3] or 0)
                generation_ms = float(tps_row[4] or 0.0)
                entry["tps"] = _tps(output_tokens, generation_ms)
                entry["tps_requests"] = int(tps_row[2] or 0)
                entry["tps_output_tokens"] = output_tokens
                entry["tps_generation_ms"] = round(generation_ms, 1)
                entry["avg_ttft_ms"] = round(float(tps_row[5] or 0.0), 1)
            entries.append(entry)
        entries.sort(key=_breakdown_sort_key)
        return entries

    async def first_ts(self) -> str | None:
        """Return the timestamp of the oldest row in the store.

        The dashboard shows it as "data since": when the store is younger
        than the selected window, the window shows everything there is,
        and saying so is honest -- a range that covers no rows reads as a
        broken page otherwise.

        Returns:
            The oldest ``ts`` in the store (ISO-8601 UTC), or ``None``
            when the store holds no rows.
        """
        rows = await asyncio.to_thread(self._query, "SELECT MIN(ts) FROM request_log", ())
        if not rows or rows[0][0] is None:
            return None
        return str(rows[0][0])

    async def daily_series(self, *, days: int) -> list[dict[str, Any]]:
        """Sum the counters per UTC day over the last ``days`` days.

        Args:
            days: how many days back the series reaches, from now.

        Returns:
            One entry per UTC day that has rows, oldest first, each with
            ``day`` (``YYYY-MM-DD``), ``requests``, ``errors``,
            ``usage`` (the four token keys), ``cost_usd`` and
            ``priced_requests``.
        """
        cutoff = (datetime.now(UTC) - timedelta(days=days)).isoformat()
        return await self.range_series(start=cutoff, end=None, bucket="day")

    async def range_series(
        self, *, start: str, end: str | None, bucket: Literal["hour", "day"] = "day"
    ) -> list[dict[str, Any]]:
        """Sum the counters per time bucket over ``[start, end)``.

        ``bucket`` picks the granularity: ``"day"`` for anything spanning
        more than a couple of days, ``"hour"`` for a one-day window, where
        a day-granular series would collapse to a single point.

        Args:
            start: inclusive lower bound on ``ts`` (ISO-8601 UTC).
            end: exclusive upper bound, or ``None`` for no upper bound.
            bucket: ``"hour"`` or ``"day"``.

        Returns:
            One entry per bucket that has rows, oldest first, each with
            ``bucket`` (``YYYY-MM-DDTHH`` for hours, ``YYYY-MM-DD`` for
            days) and the same counters ``daily_series`` reports. The key
            is ``bucket`` rather than ``day`` so an hourly series is not
            misread as daily; ``daily_series`` keeps its ``day`` key.
        """
        where, params = _range_where(start, end)
        sql = _SERIES_TEMPLATE.format(bucket=_BUCKET_EXPR[bucket], where=where)
        rows = await asyncio.to_thread(self._query, sql, params)
        return [self._series_entry(row, key="day" if bucket == "day" else "bucket") for row in rows]

    async def tps(
        self, *, start: str, end: str | None, bucket: Literal["hour", "day"]
    ) -> dict[str, Any]:
        """The generation throughput of every model over a range.

        Throughput is the output tokens over the *generation* time -- the
        time from the first content token to the last response byte. It is
        deliberately not the output tokens over the whole request: the
        prompt processing of a two-million-token prompt would dominate the
        total duration and understate the generation speed badly.

        Every figure is token-weighted: the sum of the output tokens over
        the sum of the generation windows, never the mean of the per-request
        ratios.

        Args:
            start: inclusive lower bound on ``ts`` (ISO-8601 UTC).
            end: exclusive upper bound, or ``None`` for no upper bound.
            bucket: ``"hour"`` or ``"day"`` -- the granularity of the series.

        Returns:
            ``{"models": [...], "series": [...], "by_model": {...}}``:
            ``models`` lists every model the log knows, by first appearance
            over the whole table (a model's colour must not move when the
            window changes); ``series`` has one entry per bucket and model
            with the bucket's throughput, its output tokens, its generation
            time and its request count, oldest bucket first; ``by_model``
            aggregates the same over the whole window and adds the average
            time to the first token.
        """
        where, params = _range_where(start, end)
        qualify = (*params, TPS_MIN_GENERATION_MS)
        series_rows = await asyncio.to_thread(
            self._query,
            _TPS_SERIES_TEMPLATE.format(bucket=_BUCKET_EXPR[bucket], where=where),
            qualify,
        )
        model_rows = await asyncio.to_thread(
            self._query, _TPS_MODEL_TEMPLATE.format(where=where), qualify
        )
        model_names = await asyncio.to_thread(
            self._query, _TPS_MODELS_TEMPLATE, (TPS_MIN_GENERATION_MS,)
        )
        models = [str(row[0]) for row in model_names]
        by_model: dict[str, dict[str, Any]] = {}
        for row in model_rows:
            model = str(row[0])
            output_tokens = int(row[1] or 0)
            generation_ms = float(row[2] or 0.0)
            by_model[model] = {
                "tps": _tps(output_tokens, generation_ms),
                "output_tokens": output_tokens,
                "generation_ms": round(generation_ms, 1),
                "requests": int(row[3] or 0),
                "avg_ttft_ms": round(float(row[4] or 0.0), 1),
            }
        series = [
            {
                "bucket": str(row[0]),
                "model": str(row[1]),
                "tps": _tps(int(row[2] or 0), float(row[3] or 0.0)),
                "output_tokens": int(row[2] or 0),
                "generation_ms": round(float(row[3] or 0.0), 1),
                "requests": int(row[4] or 0),
            }
            for row in series_rows
        ]
        return {
            # The global order first, so the chart's colours follow the model
            # and not its rank in this window.
            "models": models,
            "series": series,
            "by_model": {model: by_model[model] for model in models if model in by_model},
        }

    async def prune(self, *, retention_days: int) -> int:
        """Delete the rows older than the retention cutoff.

        Called once at startup and once a day. A failure is logged and
        reported as zero deleted, never raised: pruning is housekeeping and
        must not break anything that is running.

        Args:
            retention_days: how many days of rows to keep, from now.

        Returns:
            The number of rows deleted (0 when the store is inert or the
            deletion failed).
        """
        connection = self._connection
        if connection is None:
            return 0
        cutoff = (datetime.now(UTC) - timedelta(days=retention_days)).isoformat()
        try:
            with self._lock:
                cursor = connection.execute("DELETE FROM request_log WHERE ts < ?", (cutoff,))
                connection.commit()
                deleted = cursor.rowcount
        except Exception as exc:  # noqa: BLE001 - pruning must never break a running proxy
            self._last_error = f"{type(exc).__name__}: {exc}"
            logger.warning("request_store_prune_failed", error=self._last_error)
            return 0
        return int(deleted)

    async def aclose(self) -> None:
        """Close the connection; safe to call more than once."""
        connection = self._connection
        if connection is None:
            return
        self._connection = None
        with self._lock:
            connection.close()

    def _query(self, sql: str, params: Sequence[object]) -> list[tuple[Any, ...]]:
        """Run ``sql`` under the lock; a failure logs, not raises.

        Args:
            sql: the statement to run.
            params: its bound parameters.

        Returns:
            The result rows, or an empty list when the store is inert or
            the query failed.
        """
        connection = self._connection
        if connection is None:
            return []
        try:
            with self._lock:
                return list(connection.execute(sql, params).fetchall())
        except Exception as exc:  # noqa: BLE001 - a read failure degrades, it does not raise
            self._last_error = f"{type(exc).__name__}: {exc}"
            logger.warning("request_store_query_failed", error=self._last_error)
            return []
    def _series_entry(
        self, row: tuple[Any, ...], *, key: Literal["day", "bucket"]
    ) -> dict[str, Any]:
        """Shape one series row (daily or hourly) into a dashboard entry.

        Args:
            row: one aggregate row from the series query.
            key: the name of the bucket field, so ``daily_series`` keeps
                its historical ``day`` and an hourly series reads as
                ``bucket``.

        Returns:
            The entry with the bucket key and the per-bucket counters.
        """
        return {
            key: row[0],
            "requests": int(row[1] or 0),
            "errors": int(row[2] or 0),
            "usage": {
                "input_tokens": int(row[3] or 0),
                "output_tokens": int(row[4] or 0),
                "cache_creation_input_tokens": int(row[5] or 0),
                "cache_read_input_tokens": int(row[6] or 0),
            },
            "priced_requests": int(row[7] or 0),
            "cost_usd": round(float(row[8] or 0.0), 6),
        }

    def _grouped_from_row(self, row: tuple[Any, ...]) -> dict[str, Any]:
        """Shape one grouped row (name + counters) like ``_Stats.as_dict()``.

        The grouping query puts the name in column 0 and the counters one
        column further right than the window query does, so it cannot share
        ``_totals_from_row``; the two must otherwise stay in step.

        Args:
            row: one row of the grouped aggregate.

        Returns:
            The counters for that provider or model.
        """
        return self._totals_from_row(row[1:])

    def _totals_from_row(self, row: tuple[Any, ...] | None) -> dict[str, Any]:
        """Shape one window aggregate row like ``_Stats.as_dict()``.

        Args:
            row: the aggregate row, or None when the table has no rows.

        Returns:
            The counters, with a ``storage_error`` key appended.
        """
        requests = int(row[0]) if row else 0
        duration_ms_total = float(row[2]) if row else 0.0
        usage = {
            "input_tokens": int(row[3]) if row else 0,
            "output_tokens": int(row[4]) if row else 0,
            "cache_creation_input_tokens": int(row[5]) if row else 0,
            "cache_read_input_tokens": int(row[6]) if row else 0,
        }
        average = duration_ms_total / requests if requests else 0.0
        return {
            "requests": requests,
            "errors": int(row[1]) if row else 0,
            "avg_duration_ms": round(average, 1),
            "duration_ms_total": round(duration_ms_total, 1),
            "usage": usage,
            "tokens": sum(usage.values()),
            "priced_requests": int(row[7]) if row else 0,
            "cost_usd": round(float(row[8]) if row else 0.0, 6),
            "storage_error": self._last_error,
        }
