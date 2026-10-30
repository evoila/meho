# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The migration runner waits for the database and holds a lock (#5233).

With the built-in PostgreSQL the chart runs ``python -m
meho_backplane.db.migrate`` as an init container of every backplane pod.
The database starts at the same time, and two replicas can start at once.
These tests pin the four behaviours that makes safe:

* the runner waits for a database that is not up yet, and fails with the
  ``migration_failed`` line after its time limit;
* a login the server refuses fails at once, without waiting;
* two runs at the same time do not both migrate: one waits for the
  advisory lock, then finds the schema at head;
* a database that is ahead of the image is left alone (rollback contract).

The lock tests start PostgreSQL with testcontainers and run the runner as
real processes, as two pods would. They skip where Docker is absent.
"""

from __future__ import annotations

import asyncio
import os
import socket
import sqlite3
import sys
import time
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import asyncpg
import pytest
from alembic.script import ScriptDirectory

from meho_backplane.db import migrate
from meho_backplane.db.migrate import MIGRATION_LOCK_KEY, main
from meho_backplane.db.migrations import alembic_config
from tests._pgvector_image import pgvector_test_image

_BACKEND_DIR = Path(__file__).resolve().parents[2]


def _head() -> str:
    head = ScriptDirectory.from_config(alembic_config()).get_current_head()
    assert head is not None
    return head


def _closed_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _sqlite_path(url: str) -> str:
    return url.split(":///", 1)[1]


def _stamp(url: str, revision: str) -> None:
    with sqlite3.connect(_sqlite_path(url)) as db:
        db.execute("UPDATE alembic_version SET version_num = ?", (revision,))


def _version(url: str) -> list[str]:
    with sqlite3.connect(_sqlite_path(url)) as db:
        return [row[0] for row in db.execute("SELECT version_num FROM alembic_version")]


# ---------------------------------------------------------------------------
# Waiting for the database
# ---------------------------------------------------------------------------


def test_runner_waits_for_a_database_that_is_down_then_fails_at_its_limit(
    monkeypatch: pytest.MonkeyPatch, capfd: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv(
        "DATABASE_URL", f"postgresql+asyncpg://meho:x@127.0.0.1:{_closed_port()}/meho"
    )
    monkeypatch.setenv("MIGRATION_DB_WAIT_SECONDS", "1")

    started = time.monotonic()
    code = main()
    elapsed = time.monotonic() - started

    err = capfd.readouterr().err.splitlines()
    assert code == 1
    assert elapsed >= 1.0
    assert err[0].startswith("migration_db_waiting: ConnectionRefusedError; waited 0 s of 1 s")
    assert err[-1].startswith(
        "migration_failed: DatabaseNotReadyError: the database did not accept a connection "
        "within 1 s (MIGRATION_DB_WAIT_SECONDS); last error: ConnectionRefusedError"
    )


class _FlakyEngine:
    """Stands in for ``AsyncEngine``: ``connect()`` fails, then succeeds."""

    def __init__(self, failures: list[BaseException]) -> None:
        self.failures = failures
        self.calls = 0
        self.connection = object()

    async def connect(self) -> Any:
        self.calls += 1
        if self.failures:
            raise self.failures.pop(0)
        return self.connection


class _ServerError(Exception):
    def __init__(self, sqlstate: str) -> None:
        super().__init__(f"server said {sqlstate}")
        self.sqlstate = sqlstate


async def test_runner_connects_once_the_database_answers(
    monkeypatch: pytest.MonkeyPatch, capfd: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(migrate, "_DB_RETRY_SECONDS", 0.01)
    starting_up = _ServerError("57P03")  # "the database system is starting up"
    engine = _FlakyEngine([ConnectionRefusedError(), starting_up, ConnectionResetError()])

    conn = await migrate._connect(engine, limit_seconds=30)

    err = capfd.readouterr().err
    assert conn is engine.connection
    assert engine.calls == 4
    assert "migration_db_waiting: ConnectionRefusedError" in err
    assert "migration_db_ready: after 0 s" in err


@pytest.mark.parametrize(
    ("sqlstate", "meaning"),
    [
        ("28P01", "invalid password"),
        ("28000", "no pg_hba.conf entry"),
        ("3D000", "database does not exist"),
    ],
)
async def test_runner_does_not_wait_when_the_server_refuses_the_login(
    sqlstate: str, meaning: str, capfd: pytest.CaptureFixture[str]
) -> None:
    engine = _FlakyEngine([_ServerError(sqlstate)])

    with pytest.raises(_ServerError):
        await migrate._connect(engine, limit_seconds=30)

    assert engine.calls == 1, meaning
    assert "migration_db_waiting" not in capfd.readouterr().err


@pytest.mark.parametrize("raw", ["abc", "-1", "nan", "inf"])
def test_a_wrong_wait_setting_fails_the_run(
    raw: str, monkeypatch: pytest.MonkeyPatch, capfd: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("MIGRATION_DB_WAIT_SECONDS", raw)

    assert main() == 1
    assert capfd.readouterr().err.startswith(
        "migration_failed: ValueError: MIGRATION_DB_WAIT_SECONDS must be a number of seconds"
    )


@pytest.mark.parametrize(
    ("raw", "expected"), [("", 300.0), ("  ", 300.0), ("0", 0.0), ("45", 45.0)]
)
def test_wait_setting_default_and_values(
    raw: str, expected: float, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MIGRATION_DB_WAIT_SECONDS", raw)

    assert migrate._db_wait_seconds() == expected


# ---------------------------------------------------------------------------
# Upgrade and the rollback contract (SQLite, no lock)
# ---------------------------------------------------------------------------


def test_runner_upgrades_an_empty_database_to_head(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    url = f"sqlite+aiosqlite:///{tmp_path / 'empty.db'}"
    monkeypatch.setenv("DATABASE_URL", url)

    assert main() == 0
    # Read through a new connection: the runner's migrations were committed.
    assert _version(url) == [_head()]


def test_runner_leaves_a_newer_database_alone(
    monkeypatch: pytest.MonkeyPatch, capfd: pytest.CaptureFixture[str]
) -> None:
    # The conftest database is at head; a newer release stamped "ffff9999".
    url = os.environ["DATABASE_URL"]
    _stamp(url, "ffff9999")

    code = main()

    err = capfd.readouterr().err
    assert code == 0
    assert (
        f"migration_skipped: the database is at ffff9999, newer than this image's head {_head()}"
        in err
    )
    assert "migration_failed" not in err
    assert _version(url) == ["ffff9999"]


# ---------------------------------------------------------------------------
# The advisory lock (PostgreSQL, real processes)
# ---------------------------------------------------------------------------


def _docker_socket_present() -> bool:
    return Path("/var/run/docker.sock").exists() or os.environ.get("DOCKER_HOST") is not None


@pytest.fixture(scope="module")
def pg_server() -> Iterator[dict[str, Any]]:
    """One PostgreSQL server for the module; each test makes its own database."""
    from testcontainers.postgres import PostgresContainer

    with PostgresContainer(pgvector_test_image()) as pg:
        yield {
            "host": pg.get_container_host_ip(),
            "port": int(pg.get_exposed_port(5432)),
            "user": pg.username,
            "password": pg.password,
        }


@pytest.fixture
async def pg_database(pg_server: dict[str, Any]) -> dict[str, str]:
    """A new, empty database; the DSNs for asyncpg and for the runner."""
    name = f"migrate_{uuid.uuid4().hex[:12]}"
    auth = f"{pg_server['user']}:{pg_server['password']}@{pg_server['host']}:{pg_server['port']}"
    admin = await asyncpg.connect(f"postgresql://{auth}/postgres")
    try:
        await admin.execute(f'CREATE DATABASE "{name}"')
    finally:
        await admin.close()
    return {
        "dsn": f"postgresql://{auth}/{name}",
        "url": f"postgresql+asyncpg://{auth}/{name}",
    }


async def _start_runner(url: str) -> asyncio.subprocess.Process:
    env = {**os.environ, "DATABASE_URL": url, "MIGRATION_DB_WAIT_SECONDS": "30"}
    return await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "meho_backplane.db.migrate",
        cwd=_BACKEND_DIR,
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )


async def _finish(proc: asyncio.subprocess.Process) -> tuple[int, str]:
    _, err = await asyncio.wait_for(proc.communicate(), timeout=180)
    assert proc.returncode is not None
    return proc.returncode, err.decode()


async def _waiting_for_lock(conn: asyncpg.Connection) -> bool:
    # A bigint advisory key shows in pg_locks as classid (high 32 bits),
    # objid (low 32 bits) and objsubid 1.
    count = await conn.fetchval(
        "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND NOT granted "
        "AND classid::bigint = $1 AND objid::bigint = $2 AND objsubid = 1",
        MIGRATION_LOCK_KEY >> 32,
        MIGRATION_LOCK_KEY & 0xFFFF_FFFF,
    )
    return bool(count)


async def _revisions(conn: asyncpg.Connection) -> list[str]:
    rows = await conn.fetch("SELECT version_num FROM alembic_version")
    return [row["version_num"] for row in rows]


@pytest.mark.skipif(not _docker_socket_present(), reason="Docker is not available here")
async def test_runner_waits_for_the_lock_another_run_holds(pg_database: dict[str, str]) -> None:
    holder = await asyncpg.connect(pg_database["dsn"])
    try:
        await holder.execute("SELECT pg_advisory_lock($1)", MIGRATION_LOCK_KEY)
        proc = await _start_runner(pg_database["url"])

        deadline = time.monotonic() + 60
        while not await _waiting_for_lock(holder):
            assert proc.returncode is None, (await _finish(proc))[1]
            assert time.monotonic() < deadline, "the runner never asked for the lock"
            await asyncio.sleep(0.2)

        # While the lock is held, the runner has applied nothing.
        assert await holder.fetchval("SELECT to_regclass('public.alembic_version')") is None
        assert proc.returncode is None

        await holder.execute("SELECT pg_advisory_unlock($1)", MIGRATION_LOCK_KEY)
        code, err = await _finish(proc)

        assert code == 0, err
        assert "migration_lock_waiting: another migration run holds advisory lock" in err
        assert "migration_lock_acquired" in err
        assert await _revisions(holder) == [_head()]
        # The runner released its lock: this connection gets it at once.
        assert await holder.fetchval("SELECT pg_try_advisory_lock($1)", MIGRATION_LOCK_KEY)
    finally:
        await holder.close()


@pytest.mark.skipif(not _docker_socket_present(), reason="Docker is not available here")
async def test_two_runners_at_once_migrate_only_once(pg_database: dict[str, str]) -> None:
    first, second = await asyncio.gather(
        _start_runner(pg_database["url"]), _start_runner(pg_database["url"])
    )
    results = await asyncio.gather(_finish(first), _finish(second))

    assert [code for code, _ in results] == [0, 0], [err for _, err in results]
    # Alembic logs one "Running upgrade" line per applied step: exactly one
    # run applied the steps, the other found the schema at head.
    applied = ["Running upgrade" in err for _, err in results]
    assert sorted(applied) == [False, True]
    conn = await asyncpg.connect(pg_database["dsn"])
    try:
        assert await _revisions(conn) == [_head()]
    finally:
        await conn.close()
