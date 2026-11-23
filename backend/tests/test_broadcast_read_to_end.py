# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""A left archive read is stopped at its limit, also in the middle of a statement.

``run_to_end`` lets a read run on after its caller stopped waiting, for
at most ``LEFT_READ_LIMIT_SECONDS``. These tests cancel the caller while
the read runs a real SQL statement on the per-test SQLite file, and
check that the stop ends in bounded time, closes the connection (not
given back to the pool) and leaves no lock behind.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import Awaitable, Callable, Iterator

import pytest
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession
from structlog.testing import capture_logs

from meho_backplane.broadcast import read_to_end
from meho_backplane.db.engine import get_sessionmaker, reset_engine_for_testing
from meho_backplane.settings import get_settings

#: A statement that never ends by itself; SQLite can interrupt it.
_ENDLESS = text(
    "WITH RECURSIVE n(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM n) SELECT count(*) FROM n"
)


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    reset_engine_for_testing()
    yield
    get_settings.cache_clear()
    reset_engine_for_testing()


def _engine() -> AsyncEngine:
    engine: AsyncEngine = get_sessionmaker().kw["bind"]
    return engine


async def _leave_read(
    engine: AsyncEngine, read: Callable[[AsyncSession], Awaitable[object]]
) -> None:
    """Start *read*, and cancel its caller once the read runs a statement."""
    started = asyncio.Event()

    def _mark(*_args: object) -> None:
        started.set()

    event.listen(engine.sync_engine, "before_cursor_execute", _mark)
    caller = asyncio.ensure_future(read_to_end.run_to_end(get_sessionmaker(), read))
    async with asyncio.timeout(2.0):
        await started.wait()
    await asyncio.sleep(0.1)
    caller.cancel()
    with pytest.raises(asyncio.CancelledError):
        await caller
    assert read_to_end._LEFT_READS


async def _wait_until_no_left_read(seconds: float) -> None:
    async with asyncio.timeout(seconds):
        while read_to_end._LEFT_READS:
            await asyncio.sleep(0.02)


async def _write_soon(engine: AsyncEngine) -> None:
    """A write that would wait the SQLite busy time (5 s) on a kept lock."""
    async with asyncio.timeout(2.0), engine.begin() as conn:
        await conn.execute(text("CREATE TABLE written_after_the_stop (a INTEGER)"))


async def test_left_read_in_a_running_statement_is_interrupted_at_the_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The statement is interrupted; the read ends well inside the stop wait."""
    monkeypatch.setattr(read_to_end, "LEFT_READ_LIMIT_SECONDS", 0.2)
    engine = _engine()
    closed: list[object] = []
    event.listen(engine.sync_engine, "invalidate", lambda *args: closed.append(args[0]))

    async def _endless(session: AsyncSession) -> object:
        return (await session.execute(_ENDLESS)).all()

    with capture_logs() as logs:
        await _leave_read(engine, _endless)
        started = time.monotonic()
        await _wait_until_no_left_read(2.0)
    assert time.monotonic() - started < read_to_end.STOP_WAIT_SECONDS

    stopped = [entry for entry in logs if entry["event"] == "broadcast_archive_read_stopped"]
    assert [entry["read_ended"] for entry in stopped] == [True]
    assert len(closed) == 1
    assert engine.pool.checkedout() == 0
    await _write_soon(engine)


async def test_left_read_that_ignores_the_stop_has_its_connection_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A read that ignores interrupt and cancel: the stop still ends in bounded time.

    The connection is then closed without waiting for the read, so it is
    not given back to the pool.
    """
    monkeypatch.setattr(read_to_end, "LEFT_READ_LIMIT_SECONDS", 0.1)
    monkeypatch.setattr(read_to_end, "STOP_WAIT_SECONDS", 0.2)
    release = asyncio.Event()

    async def _stubborn(session: AsyncSession) -> object:
        await session.execute(text("SELECT 1"))
        while not release.is_set():
            with contextlib.suppress(asyncio.CancelledError):
                await release.wait()
        return None

    engine = _engine()
    closed: list[object] = []
    event.listen(engine.sync_engine, "invalidate", lambda *args: closed.append(args[0]))

    try:
        with capture_logs() as logs:
            await _leave_read(engine, _stubborn)
            await _wait_until_no_left_read(2.0)
        stopped = [entry for entry in logs if entry["event"] == "broadcast_archive_read_stopped"]
        assert [entry["read_ended"] for entry in stopped] == [False]
        assert len(closed) == 1
        assert engine.pool.checkedout() == 0
        await _write_soon(engine)
    finally:
        release.set()
