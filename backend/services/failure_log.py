"""
Report a degraded detector once, then keep counting.

The storm-tracking and radar paths are full of handlers that catch an exception
and `return` or `continue` without a word. Individually each looks defensible --
a malformed sweep should not take down a scan. Collectively they are how this
system fails: the classifier ran for months loading nothing while logging a line
that read like a normal untrained state, and a feature vector the estimator
could not accept raised on roughly one cell in six with the error swallowed
entirely.

The naive fix is worse than the bug. A radar volume carries dozens of sweeps and
a scan carries hundreds of cells, so logging per occurrence turns a systemic
failure into thousands of identical lines nobody reads -- silence achieved by
volume rather than by omission.

So: log the FIRST occurrence at warning, with what stopped working and what that
costs, then stay quiet and count. The count is the useful part -- a detector that
failed once on a bad sweep and a detector that has failed 40,000 times look
identical in a log that only records the first, and completely different here.

`snapshot()` is what a health endpoint reads, so a degraded detector is visible
somewhere a person actually looks rather than only in a log file.
"""

from __future__ import annotations

import logging
import threading
from typing import Any

logger = logging.getLogger("tbf.degraded")

_lock = threading.Lock()
_counts: dict[str, int] = {}
_first: dict[str, str] = {}


def note_failure(key: str, what: str, exc: BaseException | None = None) -> int:
    """Record that `key` failed. Log the first one; count every one.

    `what` should say what STOPPED WORKING and what that costs, not what threw.
    "LLSD rotation detection is producing nothing for this volume" is actionable;
    "IndexError in _detect_llsd_rotation" needs someone to already know what that
    detector is for.

    Returns the running count, so a caller can escalate on its own terms.
    """
    with _lock:
        n = _counts.get(key, 0) + 1
        _counts[key] = n
        first = n == 1
        if first:
            _first[key] = what

    if first:
        detail = f" ({type(exc).__name__}: {exc})" if exc is not None else ""
        logger.warning(
            "%s%s -- logged once; further occurrences are counted, not logged. "
            "See /api/model/paths for the running totals.", what, detail,
        )
    return n


def snapshot() -> dict[str, Any]:
    """Current degradation state, for a health endpoint.

    Empty dict means nothing has failed, which is a meaningful answer rather
    than an absence of data -- that distinction is the whole point of this
    module.
    """
    with _lock:
        return {
            "total": sum(_counts.values()),
            "detectors": [
                {"key": k, "count": _counts[k], "what": _first.get(k, "")}
                for k in sorted(_counts, key=lambda x: -_counts[x])
            ],
        }


def reset() -> None:
    """Clear all counters. For tests, and for a deliberate 'start watching from
    now' after a fix has been deployed."""
    with _lock:
        _counts.clear()
        _first.clear()
