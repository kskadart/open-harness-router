"""Unit tests for the Responses API reasoning-item cache.

The item shape and upstream's requirements for it were captured from the
live /v1/responses: an item returned in ``input`` as-is is rejected because
of ``status``, and without ``summary`` upstream responds "Missing required
parameter". The tests pin down the conversion to a valid shape, storage
bounds, and graceful degradation on a cache miss.
"""

from __future__ import annotations

from typing import Any

import pytest

from services.reasoning_cache import ReasoningCache

_CALL_ID = "call_oiAhWSjbBKCPqmjzySa6gZmu"
_REASONING_ID = "rs_0226e3ff2e40c123006a63d584cd1c81929878ad4d01848b01"
_ENCRYPTED = "gAAAAABo" + "x" * 1024


def _reasoning_item(**overrides: Any) -> dict[str, Any]:
    """A ``reasoning`` item in the shape returned by the SDK (``model_dump``)."""
    item: dict[str, Any] = {
        "id": _REASONING_ID,
        "type": "reasoning",
        "summary": [],
        "content": [],
        "encrypted_content": _ENCRYPTED,
        "status": None,
    }
    item.update(overrides)
    return item


def test_stored_items_are_returned_for_the_same_call_id() -> None:
    """Stored items are returned for the same ``call_id``."""
    cache = ReasoningCache()
    cache.store(_CALL_ID, [_reasoning_item()])

    stored = cache.get(_CALL_ID)

    assert len(stored) == 1
    assert stored[0]["id"] == _REASONING_ID
    assert stored[0]["encrypted_content"] == _ENCRYPTED


def test_unknown_call_id_returns_empty_list_without_error() -> None:
    """A miss on an unknown ``call_id`` returns an empty list, not an exception."""
    cache = ReasoningCache()

    assert cache.get("call_never_seen") == []


def test_stored_item_drops_fields_rejected_by_upstream_input() -> None:
    """The ``status`` field is stripped: upstream rejects it in ``input``."""
    cache = ReasoningCache()
    cache.store(_CALL_ID, [_reasoning_item(status="completed")])

    stored = cache.get(_CALL_ID)

    assert "status" not in stored[0]


def test_stored_item_keeps_fields_required_by_upstream_input() -> None:
    """Fields required for ``input`` are preserved in full."""
    cache = ReasoningCache()
    cache.store(_CALL_ID, [_reasoning_item()])

    stored = cache.get(_CALL_ID)

    assert set(stored[0]) == {"id", "type", "summary", "content", "encrypted_content"}


def test_empty_item_list_is_not_stored() -> None:
    """An empty item list does not create an entry."""
    cache = ReasoningCache()
    cache.store(_CALL_ID, [])

    assert cache.get(_CALL_ID) == []


def test_repeated_store_for_same_call_id_replaces_previous_items() -> None:
    """Storing again under the same key replaces the previous items."""
    cache = ReasoningCache()
    cache.store(_CALL_ID, [_reasoning_item(encrypted_content="first")])
    cache.store(_CALL_ID, [_reasoning_item(encrypted_content="second")])

    stored = cache.get(_CALL_ID)

    assert len(stored) == 1
    assert stored[0]["encrypted_content"] == "second"


def test_entries_beyond_max_size_evict_oldest_first() -> None:
    """Exceeding the cap evicts the oldest entries first."""
    cache = ReasoningCache(max_entries=2)
    cache.store("call_1", [_reasoning_item()])
    cache.store("call_2", [_reasoning_item()])
    cache.store("call_3", [_reasoning_item()])

    assert cache.get("call_1") == []
    assert cache.get("call_2") != []
    assert cache.get("call_3") != []


def test_entry_older_than_ttl_is_not_returned(monkeypatch: pytest.MonkeyPatch) -> None:
    """An entry older than the TTL counts as a miss."""
    clock = {"now": 1000.0}
    monkeypatch.setattr(
        "services.reasoning_cache.time.monotonic", lambda: clock["now"]
    )

    cache = ReasoningCache(ttl_s=60.0)
    cache.store(_CALL_ID, [_reasoning_item()])
    assert cache.get(_CALL_ID) != []

    clock["now"] = 1061.0

    assert cache.get(_CALL_ID) == []


def test_expired_entries_are_purged_on_store(monkeypatch: pytest.MonkeyPatch) -> None:
    """Expired entries are purged on the next store, without waiting for a read."""
    clock = {"now": 1000.0}
    monkeypatch.setattr(
        "services.reasoning_cache.time.monotonic", lambda: clock["now"]
    )

    cache = ReasoningCache(max_entries=100, ttl_s=60.0)
    cache.store("call_old", [_reasoning_item()])

    clock["now"] = 1061.0
    cache.store("call_new", [_reasoning_item()])

    assert cache.get("call_old") == []
    assert cache.get("call_new") != []


def test_entry_within_ttl_survives_unrelated_stores(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A fresh entry is not affected by cleanup triggered by other entries."""
    clock = {"now": 1000.0}
    monkeypatch.setattr(
        "services.reasoning_cache.time.monotonic", lambda: clock["now"]
    )

    cache = ReasoningCache(max_entries=100, ttl_s=600.0)
    cache.store("call_first", [_reasoning_item()])

    clock["now"] = 1300.0
    cache.store("call_second", [_reasoning_item()])

    assert cache.get("call_first") != []
    assert cache.get("call_second") != []
