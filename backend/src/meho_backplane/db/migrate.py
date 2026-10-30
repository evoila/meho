# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Database migration entrypoint — ``python -m meho_backplane.db.migrate``.

The runner applies ``alembic upgrade head``. The chart starts it in one of
two places (#5233):

* the ``pre-install,pre-upgrade`` hook Job
  (``deploy/charts/meho/templates/migration-job.yaml``) when the install
  brings its own PostgreSQL. That database exists before ``helm install``.
* the ``migrate`` init container of the backplane Deployment when the
  built-in PostgreSQL is on (``corePostgres.enabled``). Helm runs hooks
  before it creates the built-in database, so a hook would wait for a
  database that does not exist yet. In the pod, the database and MEHO start
  together, and every replica runs the runner.

The contract is intentionally minimal:

* No CLI flags. ``DATABASE_URL`` names the database and ``ALEMBIC_CONFIG``
  (honoured by :func:`meho_backplane.db.migrations.find_alembic_ini`) the
  config; operators can't accidentally request a partial upgrade.
  Forward-only is enforced by *not* exposing ``downgrade``.
* Exit ``0`` on success, ``1`` on any failure. The hook Job's
  ``backoffLimit`` and the init container's restart retry on non-zero; a
  release whose migration keeps failing never gets a Ready pod.
* Errors render to stderr as ``migration_failed: <ExcClass>: <msg>``.
  The exception class (not the message) is the structured key
  operators alert on; the message is for human triage and may carry
  schema-shape detail but never bearer-token-shaped content (the
  audit-side secret-leak sweep does not run inside this entrypoint;
  Alembic itself is the only thing logging here).

One run does four steps on one database connection:

1. **Wait for the database.** The runner tries to connect every few
   seconds, for at most ``MIGRATION_DB_WAIT_SECONDS`` (default 300, the
   start-up budget of the built-in PostgreSQL). After that it fails with
   :class:`DatabaseNotReadyError`. A server that answers but refuses the
   login or does not know the database (SQLSTATE class 28 or 3D) fails at
   once: waiting does not fix a wrong password.
2. **Take the migration lock.** A session-level PostgreSQL advisory lock
   (:data:`MIGRATION_LOCK_KEY`, ``pg_advisory_lock``) on the same
   connection that runs the migrations. A second runner waits for it, then
   finds the schema at head and changes nothing. The lock is released in a
   ``finally``; the server also releases it when the connection closes.
   This follows the connection rule of :mod:`meho_backplane.db.advisory`:
   lock and unlock run on the same connection.
3. **Leave a newer database alone.** When the database is at a revision
   this image does not know, a newer release stamped it, and the pod
   belongs to an older release (for example after ``helm rollback``).
   Alembic would fail with ``Can't locate revision``; the runner prints
   ``migration_skipped`` and exits ``0`` instead. This is the rollback
   contract of ``docs/codebase/migrations.md``: the older code runs on the
   newer, additive-only schema, and the ``db`` readiness probe accepts it.
4. **Upgrade.** The connection goes to Alembic through
   ``Config.attributes["connection"]`` (the Alembic cookbook recipe
   "Programmatic API use (connection sharing) With Asyncio");
   ``alembic/env.py`` then uses it instead of opening its own.

Status lines go straight to stderr, like the ``migration_failed`` line,
because ``alembic/env.py`` calls :func:`logging.config.fileConfig`, which
turns off every logger that existed before it.

The runner shares :func:`meho_backplane.db.migrations.alembic_config`
with the readiness probe so the migration that *applies* and the
revision-comparison probe that *verifies* never disagree on which
``alembic.ini`` they targeted (env-var override → wheel package data
→ cwd → source tree).
"""

from __future__ import annotations

import asyncio
import math
import os
import sys
import time
from typing import Final

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import make_url, text
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

from meho_backplane.db.migrations import alembic_config, revision_known

__all__ = ["MIGRATION_LOCK_KEY", "DatabaseNotReadyError", "main"]

#: The PostgreSQL advisory-lock key a migration run holds while it migrates
#: (#5233). One key for the whole database, so two runs (two replicas that
#: start at once, or a pod and a hand-run ``python -m
#: meho_backplane.db.migrate``) never apply migrations at the same time.
#: ASCII "MEHOMIGR", the naming scheme of the other fixed keys (scheduler
#: "MEHOSCHD", event drain "MEHOEVTB"). It must stay distinct from them.
MIGRATION_LOCK_KEY: Final[int] = 0x4D45_484F_4D49_4752  # "MEHOMIGR"

_DB_WAIT_ENV_VAR: Final = "MIGRATION_DB_WAIT_SECONDS"
_DB_WAIT_DEFAULT_SECONDS: Final = 300.0
_DB_RETRY_SECONDS: Final = 2.0
#: asyncpg's own connect timeout per attempt (its default is 60 s). A
#: Service without a ready pod can drop packets instead of refusing them.
_DB_CONNECT_TIMEOUT_SECONDS: Final = 10.0
_DB_REPORT_EVERY_SECONDS: Final = 15.0
#: SQLSTATE classes of a server that is up but refuses this login (28,
#: invalid authorization) or does not know the database (3D, invalid
#: catalog name). See https://www.postgresql.org/docs/current/errcodes-appendix.html
_PERMANENT_SQLSTATE_CLASSES: Final = frozenset({"28", "3D"})


class DatabaseNotReadyError(Exception):
    """The database did not accept a connection within the wait limit."""


def _say(line: str) -> None:
    print(line, file=sys.stderr, flush=True)


def _db_wait_seconds() -> float:
    """Return the wait limit from ``MIGRATION_DB_WAIT_SECONDS``.

    Unset or empty means the default. Anything that is not a finite number
    of 0 or more raises :class:`ValueError`, so a typo fails the run with a
    clear message instead of waiting for the wrong time.
    """
    raw = os.environ.get(_DB_WAIT_ENV_VAR, "").strip()
    if not raw:
        return _DB_WAIT_DEFAULT_SECONDS
    try:
        seconds = float(raw)
    except ValueError:
        seconds = -1.0
    if not math.isfinite(seconds) or seconds < 0:
        raise ValueError(f"{_DB_WAIT_ENV_VAR} must be a number of seconds, 0 or more; got {raw!r}")
    return seconds


def _database_url(cfg: Config) -> str:
    """``DATABASE_URL``, else the ini's ``sqlalchemy.url`` (as ``env.py`` reads it)."""
    url = os.environ.get("DATABASE_URL") or cfg.get_main_option("sqlalchemy.url")
    if not url:
        raise ValueError("DATABASE_URL is not set")
    return url


def _build_engine(url: str) -> AsyncEngine:
    connect_args: dict[str, float] = {}
    if make_url(url).get_driver_name() == "asyncpg":
        connect_args["timeout"] = _DB_CONNECT_TIMEOUT_SECONDS
    return create_async_engine(url, poolclass=NullPool, connect_args=connect_args)


def _is_permanent(exc: BaseException) -> bool:
    """``True`` when the server answered with a login or database error.

    asyncpg raises its own exceptions (with ``sqlstate``) from a connect
    attempt; a SQLAlchemy-wrapped error carries the driver error in
    ``orig``. Network errors carry no SQLSTATE and are retried.
    """
    for candidate in (exc, getattr(exc, "orig", None)):
        code = getattr(candidate, "sqlstate", None) or getattr(candidate, "pgcode", None)
        if isinstance(code, str):
            return code[:2] in _PERMANENT_SQLSTATE_CLASSES
    return False


async def _connect(engine: AsyncEngine, *, limit_seconds: float) -> AsyncConnection:
    """Open the runner's connection, waiting up to *limit_seconds* for the database."""
    started = time.monotonic()
    deadline = started + limit_seconds
    last_report = -math.inf
    failed = False
    while True:
        try:
            conn = await engine.connect()
        except Exception as exc:
            if _is_permanent(exc):
                raise
            now = time.monotonic()
            if now >= deadline:
                raise DatabaseNotReadyError(
                    f"the database did not accept a connection within {limit_seconds:g} s "
                    f"({_DB_WAIT_ENV_VAR}); last error: {type(exc).__name__}: {exc}"
                ) from exc
            if now - last_report >= _DB_REPORT_EVERY_SECONDS:
                _say(
                    f"migration_db_waiting: {type(exc).__name__}; "
                    f"waited {now - started:.0f} s of {limit_seconds:g} s"
                )
                last_report = now
            failed = True
            await asyncio.sleep(min(_DB_RETRY_SECONDS, deadline - now))
            continue
        if failed:
            _say(f"migration_db_ready: after {time.monotonic() - started:.0f} s")
        return conn


async def _lock(conn: AsyncConnection) -> None:
    """Take :data:`MIGRATION_LOCK_KEY` on *conn*, waiting while another run holds it."""
    if conn.dialect.name != "postgresql":
        return
    params = {"key": MIGRATION_LOCK_KEY}
    if not await conn.scalar(text("SELECT pg_try_advisory_lock(:key)"), params):
        _say(
            f"migration_lock_waiting: another migration run holds advisory lock "
            f"{MIGRATION_LOCK_KEY}; waiting until it finishes"
        )
        started = time.monotonic()
        await conn.execute(text("SELECT pg_advisory_lock(:key)"), params)
        _say(f"migration_lock_acquired: after {time.monotonic() - started:.0f} s")
    # End the transaction the lock statement began; a session-level lock
    # outlives it. Alembic must find the connection outside a transaction,
    # or it treats it as the caller's transaction and never commits.
    await conn.commit()


async def _unlock(conn: AsyncConnection) -> None:
    """Release :data:`MIGRATION_LOCK_KEY`; never hides the error of the run."""
    if conn.dialect.name != "postgresql":
        return
    try:
        await conn.rollback()
        released = await conn.scalar(
            text("SELECT pg_advisory_unlock(:key)"), {"key": MIGRATION_LOCK_KEY}
        )
        await conn.commit()
    except Exception as exc:
        # The connection closes right after this; closing ends the
        # session, which releases a session-level lock on the server.
        _say(f"migration_lock_release_failed: {type(exc).__name__}; the connection close frees it")
        return
    if not released:
        _say("migration_lock_release_failed: the lock was not held by this connection")


def _upgrade(connection: Connection, cfg: Config) -> None:
    """Run ``alembic upgrade head`` on *connection*, unless the database is newer."""
    script = ScriptDirectory.from_config(cfg)
    current = MigrationContext.configure(connection).get_current_heads()
    # The read began a transaction; end it before Alembic configures
    # itself on this connection (see _lock).
    connection.rollback()
    newer = [revision for revision in current if not revision_known(script, revision)]
    if newer:
        _say(
            f"migration_skipped: the database is at {', '.join(newer)}, newer than this "
            f"image's head {script.get_current_head()}; nothing to apply"
        )
        return
    cfg.attributes["connection"] = connection
    command.upgrade(cfg, "head")


async def _run(cfg: Config) -> None:
    wait_seconds = _db_wait_seconds()
    engine = _build_engine(_database_url(cfg))
    try:
        conn = await _connect(engine, limit_seconds=wait_seconds)
        try:
            await _lock(conn)
            try:
                await conn.run_sync(_upgrade, cfg)
            finally:
                await _unlock(conn)
        finally:
            await conn.close()
    finally:
        await engine.dispose()


def main() -> int:
    """Run the four steps above and return a process exit code.

    Returns ``0`` on success (including a skipped run on a newer
    database), ``1`` on any exception: a database that stayed down, a
    refused login, or anything Alembic raises (including the underlying
    SQLAlchemy / asyncpg / network errors propagated up from ``env.py``).
    """
    try:
        cfg = alembic_config()
        asyncio.run(_run(cfg))
    except Exception as exc:
        print(
            f"migration_failed: {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
