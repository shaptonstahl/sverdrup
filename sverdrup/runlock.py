"""Single-instance guard for scheduled jobs."""

from __future__ import annotations

import fcntl
from collections.abc import Iterator
from contextlib import contextmanager


@contextmanager
def run_lock(path: str) -> Iterator[bool]:
    """Hold an exclusive advisory lock on ``path`` without waiting.

    Yields True when this process holds the lock, False when another run does,
    so a slow backfill and the next scheduled run never overlap.
    """
    with open(path, "a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)
