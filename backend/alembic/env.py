# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Alembic environment script — async-aware (SQLAlchemy 2.x + asyncpg).

The pattern follows the upstream `Alembic async cookbook
<https://alembic.sqlalchemy.org/en/latest/cookbook.html#using-asyncio-with-alembic>`_:
``run_migrations_online`` enters an :func:`asyncio.run` boundary,
constructs an :class:`AsyncEngine` via
:func:`sqlalchemy.ext.asyncio.async_engine_from_config`, opens an
async connection, and delegates to a synchronous migration callable
through :meth:`AsyncConnection.run_sync`.

Two design decisions worth flagging:

* **URL source.** ``sqlalchemy.url`` in ``alembic.ini`` is left blank;
  the URL is resolved from the ``DATABASE_URL`` env var here so the
  migration runner and the request hot path can never disagree on the
  target database. Operators run ``DATABASE_URL=... alembic upgrade
  head`` and the same env var the backplane already reads is honoured.
* **Empty target_metadata.** v0.1 ships an empty ``versions/``
  directory; the first model lands in T28. ``target_metadata = None``
  means autogeneration is a no-op until then, but ``alembic upgrade
  head`` still works (and creates the ``alembic_version`` table on
  first run, which is what the readiness probe in T27 looks for).
  When T28 adds the audit-log model, this file imports its
  ``Base.metadata`` and assigns it here.

Offline mode is preserved for completeness — operators rendering
SQL without a live DB connection (``alembic upgrade head --sql``)
still need it. The offline path uses the URL straight from the env
var, no engine construction.
"""

from __future__ import annotations

import asyncio
import os
from logging.config import fileConfig
from typing import Any

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

# this is the Alembic Config object, which provides access to values
# within the .ini file in use.
config = context.config

# Interpret the config file for Python logging. Skipped when running
# under tools that do not pass an ini file (some IDE integrations).
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Resolve the database URL from the env var the running backplane
# reads. Mirrors the migration-runner contract documented in the
# T29 issue and keeps env.py and the live engine in lock-step.
_database_url = os.environ.get("DATABASE_URL")
if _database_url:
    config.set_main_option("sqlalchemy.url", _database_url)

# v0.1 chassis: no models declared yet. T28's audit-log migration
# imports ``meho_backplane.db.models.Base.metadata`` and assigns it
# here. Until then ``--autogenerate`` is a no-op.
target_metadata: Any = None


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode.

    Renders SQL without binding to a live DB. Useful for review and
    for CI dry-runs before ``upgrade head`` runs in production.
    """
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    """Synchronous core of an online migration run.

    Receives a sync :class:`Connection` (which the async wrapper
    provides via ``run_sync``); configures Alembic against it and
    invokes the migrations inside a transaction. Splitting the
    function out is what lets ``async_engine_from_config`` /
    ``run_sync`` work without re-entering an event loop from inside
    the migration scripts.
    """
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    """Build an async engine and run migrations through ``run_sync``."""
    section = config.get_section(config.config_ini_section) or {}
    connectable = async_engine_from_config(
        section,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


def run_migrations_online() -> None:
    """Entry point Alembic invokes for online migrations."""
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
