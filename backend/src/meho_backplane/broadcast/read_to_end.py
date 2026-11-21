# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Run one short archive read to its end, also when its caller is cancelled.

A cancel that lands in the middle of a database statement can leave the
connection holding its lock. On SQLite the next write then waits and
fails with "database is locked". So each archive read runs in its own
task. A cancelled caller stops waiting at once, and the read still ends
and closes its database session.
"""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from typing import Any

__all__ = ["run_to_end"]

#: Reads whose caller stopped waiting. Kept here so a read is not
#: garbage-collected before it ends.
_LEFT_READS: set[asyncio.Task[Any]] = set()


async def run_to_end[T](read: Coroutine[Any, Any, T]) -> T:
    """Await *read* in its own task; a cancel of the caller does not stop it.

    When the caller stops waiting, :func:`asyncio.shield` has asyncio log
    an error of the read that is left, so the error is still seen.
    """
    task = asyncio.ensure_future(read)
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        if not task.done():
            _LEFT_READS.add(task)
            task.add_done_callback(_LEFT_READS.discard)
        raise
