"""Message threads of Claude Code v2.1.278+, held for a translated provider.

On a first-party host -- which the forward-proxy is, as far as the client
can tell -- Claude Code sends the first request of a turn with ``thread:
{"type": "create"}`` and the whole conversation, and every tool-result step
after it as a delta with ``thread: {"type": "continue",
"previous_message_id": ...}``: the attribution block as the entire
``system``, the new messages only, no tools. The upstream is expected to
hold the rest under the id of the previous response. Anthropic does; an
OpenAI-format upstream cannot, so the router holds it instead. Every
response handed out under a ``thread`` field is recorded together with the
request that produced it, and a continuation is rebuilt into the full
request the client would have sent without threads: the held system prompt
with the attribution block refreshed from the delta, the held messages, the
recorded assistant turn, the delta's messages, the held tools.

The store is bounded by entry count and by age (see ``const``). A miss --
process restart, eviction, expired TTL -- is raised to the caller, which
answers the client with the 400 that makes it resend the full conversation.
"""

from __future__ import annotations

import json
import time
from collections import OrderedDict
from collections.abc import AsyncGenerator, AsyncIterator
from dataclasses import dataclass
from typing import Any

from const import THREAD_STORE_MAX_ENTRIES, THREAD_STORE_TTL_S, Constants
from log import get_logger

logger = get_logger(__name__)

# Marks the attribution block Claude Code puts first in ``system``: the only
# system content a continuation carries, and one that changes per request.
_ATTRIBUTION_PREFIX = "x-anthropic-billing-header:"

# Request fields a continuation does not restate -- the held request keeps
# them -- plus ``thread`` itself, which never reaches the upstream.
_HELD_FIELDS = frozenset({"system", "messages", "tools", "thread"})


class ThreadMissError(Exception):
    """A continuation names a response the store does not hold.

    Attributes:
        reason: ``unknown_id`` -- never recorded here, evicted, or no id
            given at all; ``expired`` -- older than the TTL.
    """

    def __init__(self, reason: str) -> None:
        """Keep the reason for the log."""
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True, slots=True)
class _Entry:
    """One recorded response: when, the request behind it, what it said."""

    stored_at: float
    request: dict[str, Any]
    assistant_content: list[dict[str, Any]]


class ThreadStore:
    """Conversations keyed by the id of the response that closed them.

    Concurrency assumption: the same as ``ReasoningCache`` -- the router is
    single-process and async, the methods that touch the entries are
    synchronous, so no lock is needed. ``record_stream`` awaits between
    frames, but records in one synchronous step at the end.
    """

    def __init__(
        self,
        name: str,
        *,
        max_entries: int = THREAD_STORE_MAX_ENTRIES,
        ttl_s: float = THREAD_STORE_TTL_S,
    ) -> None:
        """Create an empty store with the given bounds.

        Args:
            name: the owning provider's name, for the log.
            max_entries: cap on the number of entries; beyond it, the oldest
                by storage time are evicted.
            ttl_s: entry lifetime in seconds.
        """
        self._name = name
        self._max_entries = max_entries
        self._ttl_s = ttl_s
        self._entries: OrderedDict[str, _Entry] = OrderedDict()

    def record(
        self,
        message_id: str,
        request: dict[str, Any],
        assistant_content: list[dict[str, Any]],
    ) -> None:
        """Remember the conversation a response closes.

        Args:
            message_id: the ``id`` the client received for the response.
            request: the full Anthropic-format request that produced it, as
                the client sent it or as :meth:`resume` rebuilt it.
            assistant_content: the response's content blocks as the client
                received them -- what it echoes back as the assistant turn.
                The converters always open a text block, so an empty list
                stands in for one, keeping the turn well-formed.
        """
        held = {key: value for key, value in request.items() if key != "thread"}
        blocks = assistant_content or [{"type": Constants.CONTENT_TEXT, "text": ""}]
        # Overwriting moves the key to the end, otherwise dict order would
        # stop matching storage time and the cheap eviction would break.
        self._entries.pop(message_id, None)
        self._entries[message_id] = _Entry(time.monotonic(), held, blocks)
        self._evict()
        logger.debug(
            "thread_recorded",
            provider=self._name,
            message_id=message_id,
            messages=len(held.get("messages") or []),
            blocks=len(blocks),
        )

    def resume(self, previous_message_id: str | None, delta: dict[str, Any]) -> dict[str, Any]:
        """Rebuild the full request a continuation stands for.

        The held request is not modified: the result is a new mapping whose
        ``system`` and ``messages`` are new lists, so the same entry can be
        resumed again (a client retry) with the same outcome.

        Args:
            previous_message_id: the response the continuation follows.
            delta: the continuation's own payload.

        Returns:
            The request with the held fields restored: the delta's scalar
            fields over the held ones, the held ``system`` with its
            attribution block refreshed from the delta, the held
            ``messages`` plus the recorded assistant turn plus the delta's,
            the delta's ``tools`` when it restates them and the held ones
            otherwise, and no ``thread``.

        Raises:
            ThreadMissError: nothing is held under that id, or it has expired.
        """
        if previous_message_id is None:
            raise ThreadMissError("unknown_id")
        entry = self._entries.get(previous_message_id)
        if entry is None:
            raise ThreadMissError("unknown_id")
        if time.monotonic() - entry.stored_at > self._ttl_s:
            del self._entries[previous_message_id]
            raise ThreadMissError("expired")

        request = dict(entry.request)
        request.update({key: value for key, value in delta.items() if key not in _HELD_FIELDS})
        system = _merge_system(entry.request.get("system"), delta.get("system"))
        if system is None:
            request.pop("system", None)
        else:
            request["system"] = system
        request["messages"] = [
            *(entry.request.get("messages") or []),
            {"role": Constants.ROLE_ASSISTANT, "content": entry.assistant_content},
            *(delta.get("messages") or []),
        ]
        if delta.get("tools"):
            request["tools"] = delta["tools"]
        return request

    async def record_stream(
        self, request: dict[str, Any], frames: AsyncIterator[str]
    ) -> AsyncIterator[str]:
        """Relay an Anthropic SSE stream unchanged and record the message it carries.

        A stream that ends any other way than with ``message_stop`` (an
        ``error`` event, a break, a client that went away) records nothing:
        the client will not continue from it.

        Args:
            request: the full request the response answers (see
                :meth:`record`).
            frames: the converter's SSE frames, one event per string.

        Yields:
            The frames, unchanged.
        """
        assembler = _MessageAssembler()
        try:
            async for frame in frames:
                if assembler.feed(frame) and assembler.message_id is not None:
                    self.record(assembler.message_id, request, assembler.content())
                yield frame
        finally:
            # The wrapper is closed from the outside when the client goes
            # away (see ``services.monitor``); the converter underneath must
            # not be left suspended with its upstream connection open.
            if isinstance(frames, AsyncGenerator):
                await frames.aclose()

    def _evict(self) -> None:
        """Remove expired entries and any excess beyond the cap.

        Entries are ordered by storage time, so the expired ones always sit
        at the front and go without a scan of the whole dict.
        """
        now = time.monotonic()
        while self._entries:
            message_id, entry = next(iter(self._entries.items()))
            if now - entry.stored_at <= self._ttl_s:
                break
            del self._entries[message_id]

        while len(self._entries) > self._max_entries:
            self._entries.popitem(last=False)


class _MessageAssembler:
    """Rebuild the assistant message the client sees from the converter's frames.

    The inverse of the response converters, which both emit the same
    Anthropic events: the id comes from ``message_start``, a block opens on
    ``content_block_start``, text grows by ``text_delta`` and a tool input
    by ``input_json_delta``, and ``message_stop`` closes the message. Frames
    that are not events, or not well-formed ones, are passed over: the
    client gets them unchanged either way, and only a complete message is
    worth recording.

    Attributes:
        message_id: the id announced by ``message_start``, if seen.
    """

    def __init__(self) -> None:
        """Start with no message."""
        self.message_id: str | None = None
        self._blocks: dict[int, dict[str, Any]] = {}

    def feed(self, frame: str) -> bool:
        """Apply one frame.

        Args:
            frame: an SSE frame as the converter emitted it.

        Returns:
            True when this frame was ``message_stop``: the message is complete.
        """
        payload = _frame_payload(frame)
        if payload is None:
            return False
        kind = payload.get("type")
        if kind == Constants.EVENT_MESSAGE_START:
            message = payload.get("message")
            if isinstance(message, dict) and isinstance(message.get("id"), str):
                self.message_id = message["id"]
        elif kind == Constants.EVENT_CONTENT_BLOCK_START:
            index = _index(payload)
            opened = payload.get("content_block")
            if index is not None and isinstance(opened, dict):
                self._blocks[index] = _open_block(opened)
        elif kind == Constants.EVENT_CONTENT_BLOCK_DELTA:
            index = _index(payload)
            delta = payload.get("delta")
            block = self._blocks.get(index) if index is not None else None
            if block is not None and isinstance(delta, dict):
                _apply_delta(block, delta)
        elif kind == Constants.EVENT_MESSAGE_STOP:
            return True
        return False

    def content(self) -> list[dict[str, Any]]:
        """Return the content blocks assembled so far, in stream order."""
        return [_close_block(self._blocks[index]) for index in sorted(self._blocks)]


def _merge_system(held: object, delta: object) -> object:
    """Return the held system prompt with the attribution block refreshed.

    A continuation's ``system`` is the attribution block alone, and it
    changes per request (a hash of the conversation); a full request would
    have carried the current one first, so it replaces the held one. Any
    other shape -- a string prompt, no attribution on either side -- keeps
    the held prompt as is.

    Args:
        held: the ``system`` of the held request.
        delta: the ``system`` of the continuation.

    Returns:
        The ``system`` for the rebuilt request; a new list when merged.
    """
    if not (isinstance(held, list) and held and isinstance(delta, list) and delta):
        return held
    if _is_attribution(held[0]) and _is_attribution(delta[0]):
        return [delta[0], *held[1:]]
    return held


def _is_attribution(block: object) -> bool:
    """Whether a system block is Claude Code's attribution block."""
    return (
        isinstance(block, dict)
        and block.get("type") == Constants.CONTENT_TEXT
        and str(block.get("text", "")).startswith(_ATTRIBUTION_PREFIX)
    )


def _frame_payload(frame: str) -> dict[str, Any] | None:
    """Return the JSON object of an SSE frame's ``data:`` line, if it has one."""
    for line in frame.split("\n"):
        if not line.startswith("data:"):
            continue
        try:
            payload = json.loads(line[5:].strip())
        except ValueError:
            return None
        return payload if isinstance(payload, dict) else None
    return None


def _index(payload: dict[str, Any]) -> int | None:
    """The block index of a content event, when it is a proper integer."""
    index = payload.get("index")
    return index if isinstance(index, int) and not isinstance(index, bool) else None


def _open_block(content_block: dict[str, Any]) -> dict[str, Any]:
    """Start assembling a content block from its ``content_block_start`` event."""
    if content_block.get("type") == Constants.CONTENT_TOOL_USE:
        return {
            "type": Constants.CONTENT_TOOL_USE,
            "id": content_block.get("id", ""),
            "name": content_block.get("name", ""),
            "partial_json": "",
        }
    return {"type": Constants.CONTENT_TEXT, "text": str(content_block.get("text", ""))}


def _apply_delta(block: dict[str, Any], delta: dict[str, Any]) -> None:
    """Fold a ``content_block_delta`` into the block being assembled."""
    kind = delta.get("type")
    if kind == Constants.DELTA_TEXT and block["type"] == Constants.CONTENT_TEXT:
        block["text"] += str(delta.get("text", ""))
    elif kind == Constants.DELTA_INPUT_JSON and block["type"] == Constants.CONTENT_TOOL_USE:
        block["partial_json"] += str(delta.get("partial_json", ""))


def _close_block(block: dict[str, Any]) -> dict[str, Any]:
    """Finish a block: parse the tool input the client parsed from the same bytes."""
    if block["type"] != Constants.CONTENT_TOOL_USE:
        return block
    partial_json = block["partial_json"]
    try:
        tool_input = json.loads(partial_json) if partial_json.strip() else {}
    except ValueError:
        tool_input = {}
    return {
        "type": Constants.CONTENT_TOOL_USE,
        "id": block["id"],
        "name": block["name"],
        "input": tool_input if isinstance(tool_input, dict) else {},
    }
