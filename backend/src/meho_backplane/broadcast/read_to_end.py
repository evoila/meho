# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Run one short archive read to its end, also when its caller is cancelled.

A cancel that lands in the middle of a database statement can leave the
connection holding its lock. On SQLite the next write then waits and
fails with "database is locked". So each archive read runs in its own
task with its own session. A cancelled caller stops waiting at once,
and the read still ends and closes its session.

A read whose caller stopped waiting runs on for at most
:data:`LEFT_READ_LIMIT_SECONDS`. Then it is stopped in steps, each with
a wait of at most :data:`STOP_WAIT_SECONDS`:

1. The running statement is interrupted in the database driver (SQLite),
   so the read ends with a database error and cleans up normally.
2. The read task is cancelled.
3. The database connection is closed at once, without waiting for it.

A stopped read's connection is closed, not given back to the pool. So a
read that never ends keeps neither a pool connection nor a lock, and the
wait for it is bounded.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Final

import structlog
from sqlalchemy import Connection, event
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session, SessionTransaction

__all__ = ["LEFT_READ_LIMIT_SECONDS", "STOP_WAIT_SECONDS", "run_to_end"]

_log = structlog.get_logger(__name__)

#: How long a read may run on after its caller stopped waiting.
LEFT_READ_LIMIT_SECONDS: Final[float] = 30.0

#: How long each step of stopping a read waits for the read to end.
STOP_WAIT_SECONDS: Final[float] = 5.0

#: The guards of reads whose caller stopped waiting. Each guard holds its
#: read, so neither is garbage-collected before the read ends.
_LEFT_READS: set[asyncio.Task[None]] = set()


@dataclass
class _ReadState:
    """What the guard needs to stop a read: its current connection."""

    connection: Connection | None = None
    stopping: bool = False


async def run_to_end[T](
    open_session: Callable[[], AsyncSession],
    read: Callable[[AsyncSession], Awaitable[T]],
) -> T:
    """Run *read* with a new session in its own task; a cancel of the caller does not stop it.

    When the caller stops waiting, the read runs on for at most
    :data:`LEFT_READ_LIMIT_SECONDS` and is then stopped.
    """
    state = _ReadState()
    task = asyncio.ensure_future(_read_in_session(open_session, read, state))
    try:
        # ``asyncio.wait`` does not cancel the read when the caller is
        # cancelled; the guard below takes over the read and its result.
        await asyncio.wait({task})
    except asyncio.CancelledError:
        if not task.done():
            _log.info("broadcast_archive_read_left", limit_seconds=LEFT_READ_LIMIT_SECONDS)
            guard = asyncio.ensure_future(_stop_at_limit(task, state))
            _LEFT_READS.add(guard)
            guard.add_done_callback(_LEFT_READS.discard)
        elif not task.cancelled():
            task.exception()
        raise
    return task.result()


async def _read_in_session[T](
    open_session: Callable[[], AsyncSession],
    read: Callable[[AsyncSession], Awaitable[T]],
    state: _ReadState,
) -> T:
    session = open_session()

    def _remember(_s: Session, _t: SessionTransaction, connection: Connection) -> None:
        state.connection = connection

    event.listen(session.sync_session, "after_begin", _remember)
    try:
        result = await read(session)
    except BaseException:
        if state.stopping:
            # Stopped at the limit: close the connection, do not give it
            # back to the pool.
            await session.invalidate()
        else:
            await session.close()
        raise
    await session.close()
    return result


async def _stop_at_limit(task: asyncio.Future[Any], state: _ReadState) -> None:
    """Wait for a left read up to the limit; stop it after that."""
    done, _ = await asyncio.wait({task}, timeout=LEFT_READ_LIMIT_SECONDS)
    if done:
        _log_left_end(task)
        return
    state.stopping = True
    if await _interrupt_statement(state.connection):
        done, _ = await asyncio.wait({task}, timeout=STOP_WAIT_SECONDS)
    if not done:
        task.cancel()
        done, _ = await asyncio.wait({task}, timeout=STOP_WAIT_SECONDS)
    if done and not task.cancelled():
        # The stop makes the read fail; the guard takes that error.
        task.exception()
    elif not done and state.connection is not None:
        # The read did not end: close its connection without waiting.
        state.connection.invalidate()
    _log.warning(
        "broadcast_archive_read_stopped",
        limit_seconds=LEFT_READ_LIMIT_SECONDS,
        read_ended=bool(done),
    )


async def _interrupt_statement(connection: Connection | None) -> bool:
    """Interrupt the statement running on *connection*, when the driver can.

    The SQLite driver can; there the statement then fails, and the read
    cleans up as after any database error. Returns whether it was sent.
    """
    if connection is None or connection.closed or connection.invalidated:
        return False
    interrupt = getattr(connection.connection.driver_connection, "interrupt", None)
    if interrupt is None:
        return False
    await interrupt()
    return True


def _log_left_end(task: asyncio.Future[Any]) -> None:
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
