"""Unit tests for raising the open-file limit at startup (``services.open_files``).

One test drives the real ``resource`` calls: it lowers the soft limit of
the test process and checks that the helper lifts it back to the target
(the original limits are restored afterwards). The remaining cases --
already high enough, capped by the hard limit, refused by the kernel --
would need a hard limit the test cannot choose, so ``getrlimit`` and
``setrlimit`` are replaced with recorders there.
"""

from __future__ import annotations

import resource
from collections.abc import Iterator

import pytest
import structlog.testing

from services import open_files

_LOWERED_SOFT = 1024
_TARGET = 2048
_INFINITY = resource.RLIM_INFINITY


@pytest.fixture
def restore_limits() -> Iterator[tuple[int, int]]:
    """Hand the test the current limits and put them back afterwards."""
    original = resource.getrlimit(resource.RLIMIT_NOFILE)
    try:
        yield original
    finally:
        resource.setrlimit(resource.RLIMIT_NOFILE, original)


def _fake_limits(
    monkeypatch: pytest.MonkeyPatch, soft: int, hard: int
) -> list[tuple[int, int]]:
    """Replace the resource calls: report ``(soft, hard)``, record every setrlimit."""
    calls: list[tuple[int, int]] = []
    monkeypatch.setattr(open_files.resource, "getrlimit", lambda _resource: (soft, hard))
    monkeypatch.setattr(
        open_files.resource, "setrlimit", lambda _resource, limits: calls.append(limits)
    )
    return calls


def test_raises_soft_limit_when_below_target(restore_limits: tuple[int, int]) -> None:
    """A soft limit below the target is lifted to it; the hard limit stays."""
    _soft, hard = restore_limits
    if hard != _INFINITY and hard < _TARGET:
        pytest.skip("hard limit too low to exercise the raise")
    resource.setrlimit(resource.RLIMIT_NOFILE, (_LOWERED_SOFT, hard))

    with structlog.testing.capture_logs() as logs:
        effective = open_files.raise_open_files_limit(_TARGET)

    assert effective == _TARGET
    assert resource.getrlimit(resource.RLIMIT_NOFILE) == (_TARGET, hard)
    events = [entry["event"] for entry in logs]
    assert events == ["open_files_limit_raised"]
    assert logs[0]["previous"] == _LOWERED_SOFT
    assert logs[0]["soft"] == _TARGET


def test_keeps_soft_limit_already_above_target(monkeypatch: pytest.MonkeyPatch) -> None:
    """A soft limit above the target is never lowered."""
    calls = _fake_limits(monkeypatch, soft=16384, hard=_INFINITY)

    assert open_files.raise_open_files_limit(8192) == 16384
    assert calls == []


def test_unlimited_soft_limit_is_left_alone(monkeypatch: pytest.MonkeyPatch) -> None:
    """RLIM_INFINITY as the soft limit is not mistaken for a small number."""
    calls = _fake_limits(monkeypatch, soft=_INFINITY, hard=_INFINITY)

    assert open_files.raise_open_files_limit(8192) == _INFINITY
    assert calls == []


def test_caps_request_at_hard_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    """A hard limit below the target bounds the raise instead of failing it."""
    calls = _fake_limits(monkeypatch, soft=256, hard=1000)

    assert open_files.raise_open_files_limit(8192) == 1000
    assert calls == [(1000, 1000)]


def test_hard_limit_already_reached_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    """Soft equal to a low hard limit: nothing to raise, the operator is told."""
    calls = _fake_limits(monkeypatch, soft=512, hard=512)

    with structlog.testing.capture_logs() as logs:
        assert open_files.raise_open_files_limit(8192) == 512

    assert calls == []
    assert [entry["event"] for entry in logs] == ["open_files_limit_capped"]
    assert logs[0]["hard"] == 512


def test_kernel_refusal_keeps_old_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    """A refused setrlimit is logged and the old ceiling is returned."""
    monkeypatch.setattr(open_files.resource, "getrlimit", lambda _resource: (256, _INFINITY))

    def refuse(_resource: int, _limits: tuple[int, int]) -> None:
        raise ValueError("not allowed to raise maximum limit")

    monkeypatch.setattr(open_files.resource, "setrlimit", refuse)

    with structlog.testing.capture_logs() as logs:
        assert open_files.raise_open_files_limit(8192) == 256

    assert [entry["event"] for entry in logs] == ["open_files_limit_unchanged"]
    assert logs[0]["hard"] is None
    assert logs[0]["requested"] == 8192


def test_current_limit_hides_infinity(monkeypatch: pytest.MonkeyPatch) -> None:
    """The startup log gets None for an unlimited soft limit, the number otherwise."""
    _fake_limits(monkeypatch, soft=_INFINITY, hard=_INFINITY)
    assert open_files.current_open_files_limit() is None

    _fake_limits(monkeypatch, soft=8192, hard=_INFINITY)
    assert open_files.current_open_files_limit() == 8192
