"""Raising the process's open-file limit at startup.

A forward-proxy spends two descriptors per tunnel (the client side and the
upstream side) and one more per connection attempt in flight, so a burst
of parallel clients -- a package manager fanning out through
``HTTPS_PROXY``, a fleet of subagents -- needs hundreds of descriptors at
once. launchd starts agents with the soft limit at 256 (``launchctl limit
maxfiles``); once it is reached, ``accept()`` fails with ``EMFILE``, every
outbound connect fails the same way, and everything behind the router goes
offline until the storm passes. The hard limit is far higher (unlimited on
macOS, 524288 for systemd services by default), and a process may raise its own
soft limit up to the hard one without privileges -- so the service does it
itself instead of relying on every plist or unit file to carry
``SoftResourceLimits``.
"""

from __future__ import annotations

import resource

from log import get_logger

logger = get_logger(__name__)

# Soft limit the service asks for: far above the 256 launchd hands out and
# above what one machine's worth of clients opens at once, yet far below
# the kernel's per-process ceiling (``kern.maxfilesperproc`` on macOS,
# ``fs.nr_open`` on Linux), so the request never fails for being too big.
OPEN_FILES_TARGET = 8192


def _limit_value(limit: int) -> int | None:
    """Map ``RLIM_INFINITY`` to ``None`` so the log does not carry 2**63.

    Args:
        limit: raw value from ``resource.getrlimit``.

    Returns:
        The limit, or None when it is unlimited.
    """
    return None if limit == resource.RLIM_INFINITY else limit


def current_open_files_limit() -> int | None:
    """Return the soft open-file limit in force, ``None`` when unlimited."""
    return _limit_value(resource.getrlimit(resource.RLIMIT_NOFILE)[0])


def raise_open_files_limit(target: int = OPEN_FILES_TARGET) -> int:
    """Raise the soft open-file limit to ``target`` when it is lower.

    Never lowers a soft limit that is already higher and never touches the
    hard limit; when the hard limit is below ``target`` the soft limit is
    raised to the hard limit instead, and a hard limit that is already the
    soft limit is reported so the operator knows the ceiling is set outside
    the process. A refusal from the kernel is logged and swallowed: the
    service must start either way, only with the old ceiling.

    Args:
        target: desired soft limit.

    Returns:
        The effective soft limit after the call.
    """
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    if soft == resource.RLIM_INFINITY or soft >= target:
        return soft
    wanted = target if hard == resource.RLIM_INFINITY else min(target, hard)
    if wanted <= soft:
        logger.warning(
            "open_files_limit_capped",
            soft=soft,
            hard=_limit_value(hard),
            requested=target,
        )
        return soft
    try:
        resource.setrlimit(resource.RLIMIT_NOFILE, (wanted, hard))
    except (ValueError, OSError) as exc:
        logger.warning(
            "open_files_limit_unchanged",
            soft=soft,
            hard=_limit_value(hard),
            requested=wanted,
            error=str(exc),
        )
        return soft
    logger.info(
        "open_files_limit_raised", previous=soft, soft=wanted, hard=_limit_value(hard)
    )
    return wanted
