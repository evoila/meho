# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Run one short archive read to its end, also when its caller is cancelled.

A cancel that lands in the middle of a database statement can leave the
connection holding its lock. On SQLite the next write then waits and
fails with "database is locked". So each archive read runs in its own
task with its own session. A cancelled caller stops waiting at once,
and the read still ends and closes its session.

A read whose caller stopped waiting runs on for at most
:data:`LEFT_READ_LIMIT_SECONDS`. After that it is stopped, and its
database connection is closed instead of given back to the pool, so a
read that never ends does not keep a pool connection.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any, Final

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

__all__ = ["LEFT_READ_LIMIT_SECONDS", "run_to_end"]

_log = structlog.get_logger(__name__)

#: How long a read may run on after its caller stopped waiting.
LEFT_READ_LIMIT_SECONDS: Final[float] = 30.0

#: The guards of reads whose caller stopped waiting. Each guard holds its
#: read, so neither is garbage-collected before the read ends.
_LEFT_READS: set[asyncio.Task[None]] = set()


async def run_to_end[T](
    open_session: Callable[[], AsyncSession],
    read: Callable[[AsyncSession], Awaitable[T]],
) -> T:
    """Run *read* with a new session in its own task; a cancel of the caller does not stop it.

    When the caller stops waiting, the read runs on for at most
    :data:`LEFT_READ_LIMIT_SECONDS` and is then stopped.
    """
    task = asyncio.ensure_future(_read_in_session(open_session, read))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        if not task.done():
            _log.info("broadcast_archive_read_left", limit_seconds=LEFT_READ_LIMIT_SECONDS)
            guard = asyncio.ensure_future(_stop_at_limit(task))
            _LEFT_READS.add(guard)
            guard.add_done_callback(_LEFT_READS.discard)
        raise


async def _read_in_session[T](
    open_session: Callable[[], AsyncSession],
    read: Callable[[AsyncSession], Awaitable[T]],
) -> T:
    session = open_session()
    try:
        result = await read(session)
    except asyncio.CancelledError:
        # Stopped at the limit, maybe in the middle of a statement: close
        # the connection, do not give it back to the pool.
        await session.invalidate()
        raise
    except BaseException:
        await session.close()
        raise
    await session.close()
    return result


async def _stop_at_limit(task: asyncio.Future[Any]) -> None:
    """Wait for a left read up to the limit; stop it after that."""
    done, _ = await asyncio.wait({task}, timeout=LEFT_READ_LIMIT_SECONDS)
    if not done:
        task.cancel()
        await asyncio.wait({task})
        _log.warning("broadcast_archive_read_stopped", limit_seconds=LEFT_READ_LIMIT_SECONDS)
        return
    if task.cancelled():
        _log.info("broadcast_archive_read_left_ended", outcome="cancelled")
        return
    error = task.exception()
    if error is not None:
        _log.warning(
            "broadcast_archive_read_left_ended", outcome="error", error_class=type(error).__name__
        )
        return
    _log.info("broadcast_archive_read_left_ended", outcome="done")
