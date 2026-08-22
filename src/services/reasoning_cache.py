"""Cache of Responses API reasoning items across tool-call steps.

The Responses API returns ``reasoning`` items with an ``encrypted_content``
field -- the model's derived chain of reasoning, encrypted. If they're sent
back together with the tool result when continuing the conversation, the
model doesn't re-derive what it already computed; otherwise every following
step of an agentic chain pays for reasoning again.

The Anthropic protocol has no way to carry such items, so the router keeps
them itself and substitutes them by ``call_id`` rather than relying on
``previous_response_id``: the latter is tied to the upstream's server-side
storage and breaks when the client edits history (Claude Code does this
during compaction).

The store is bounded both by entry count and by age: the router process is
long-lived, and each entry holds an encrypted blob of roughly a kilobyte.
"""

from __future__ import annotations

import time
from collections import OrderedDict
from typing import Any

from const import (
    REASONING_CACHE_MAX_ENTRIES,
    REASONING_CACHE_TTL_S,
    REASONING_INPUT_FIELDS,
)

# The entry's storage time (monotonic seconds) and the items themselves.
type _Entry = tuple[float, list[dict[str, Any]]]


class ReasoningCache:
    """Store of reasoning items keyed by tool-call ``call_id``.

    Concurrency assumption: the router is single-process and async (uvicorn
    without ``--workers``). The methods are synchronous and contain no
    ``await``, so from the event loop's perspective each one is atomic and
    no lock is needed. Run with multiple workers, the cache stops being
    shared -- some requests will miss, which degrades gracefully (see
    :meth:`get`), but the benefit becomes partial.
    """

    def __init__(
        self,
        *,
        max_entries: int = REASONING_CACHE_MAX_ENTRIES,
        ttl_s: float = REASONING_CACHE_TTL_S,
    ) -> None:
        """Create an empty cache with the given bounds.

        Args:
            max_entries: cap on the number of entries; beyond it, the oldest
                entries by storage time are evicted.
            ttl_s: entry lifetime in seconds.
        """
        self._max_entries = max_entries
        self._ttl_s = ttl_s
        self._entries: OrderedDict[str, _Entry] = OrderedDict()

    def store(self, call_id: str, items: list[dict[str, Any]]) -> None:
        """Store reasoning items under a tool-call identifier.

        Items are normalized to the form allowed in the ``input`` array:
        returning them as-is is rejected by the upstream (see
        ``REASONING_INPUT_FIELDS``). Normalization happens on write rather
        than on substitution, so the shape is checked in one place and
        unneeded fields don't sit around in memory.

        Args:
            call_id: tool-call identifier (the Responses API ``call_id``,
                same as the ``id`` of a ``tool_use`` block in the Anthropic
                protocol).
            items: ``reasoning`` items from the upstream's output.
        """
        if not items:
            return

        sanitized = [
            {key: value for key, value in item.items() if key in REASONING_INPUT_FIELDS}
            for item in items
        ]
        # Overwriting moves the key to the end, otherwise dict order would
        # stop matching storage time and the cheap eviction would break.
        self._entries.pop(call_id, None)
        self._entries[call_id] = (time.monotonic(), sanitized)
        self._evict()

    def get(self, call_id: str) -> list[dict[str, Any]]:
        """Return the stored reasoning items for a tool call.

        A miss (process restart, eviction, expired TTL) yields an empty
        list: the caller substitutes nothing, behavior falls back to the
        previous one, and the client sees no error.

        Args:
            call_id: tool-call identifier.

        Returns:
            The ``reasoning`` items in ``input``-ready form, or an empty list.
        """
        entry = self._entries.get(call_id)
        if entry is None:
            return []

        stored_at, items = entry
        if time.monotonic() - stored_at > self._ttl_s:
            del self._entries[call_id]
            return []
        return items

    def _evict(self) -> None:
        """Remove expired entries and any excess beyond the cap.

        Entries are ordered by storage time, so expired ones always sit at
        the front and are removed without scanning the whole dict.
        """
        now = time.monotonic()
        while self._entries:
            call_id, (stored_at, _) = next(iter(self._entries.items()))
            if now - stored_at <= self._ttl_s:
                break
            del self._entries[call_id]

        while len(self._entries) > self._max_entries:
            self._entries.popitem(last=False)
