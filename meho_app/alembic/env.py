# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""Unified Alembic environment for the MEHO monolith.

This is the single Alembic environment. It owns the schema for every module
(topology, connectors, knowledge, memory, agents, ingestion, scheduled_tasks,
orchestrator_skills, audit). The previous nine per-module trees were merged
into ``meho_app/alembic/`` so there is one linear history and one
``alembic_version`` table.
"""

from __future__ import annotations

import asyncio
import os
import sys
from logging.config import fileConfig
from pathlib import Path
from typing import TYPE_CHECKING

from alembic import context
from sqlalchemy import pool
from sqlalchemy.ext.asyncio import async_engine_from_config

if TYPE_CHECKING:
    from sqlalchemy.engine import Connection

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import meho_app.modules.agents.models  # noqa: E402, F401
import meho_app.modules.agents.persistence.transcript_models  # noqa: E402, F401
import meho_app.modules.audit.models  # noqa: E402, F401
import meho_app.modules.connectors.email_connector.models  # noqa: E402, F401
import meho_app.modules.connectors.models  # noqa: E402, F401
import meho_app.modules.connectors.rest.models  # noqa: E402, F401
import meho_app.modules.connectors.soap.db_models  # noqa: E402, F401
import meho_app.modules.ingestion.models  # noqa: E402, F401
import meho_app.modules.knowledge.job_models  # noqa: E402, F401
import meho_app.modules.knowledge.models  # noqa: E402, F401
import meho_app.modules.licensing.models  # noqa: E402, F401
import meho_app.modules.memory.models  # noqa: E402, F401
import meho_app.modules.orchestrator_skills.models  # noqa: E402, F401
import meho_app.modules.scheduled_tasks.models  # noqa: E402, F401
import meho_app.modules.topology.models  # noqa: E402, F401
from meho_app.database import Base  # noqa: E402

SQLALCHEMY_URL_KEY = "sqlalchemy.url"

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata

database_url = os.getenv("DATABASE_URL")
if database_url:
    config.set_main_option(SQLALCHEMY_URL_KEY, database_url)


def run_migrations_offline() -> None:
    """Run migrations in offline mode."""
    url = config.get_main_option(SQLALCHEMY_URL_KEY)
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    """Run migrations using the supplied (sync) Connection."""
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
    )

    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    """Run migrations against an async engine."""
    configuration = config.get_section(config.config_ini_section, {})
    configuration[SQLALCHEMY_URL_KEY] = database_url or config.get_main_option(SQLALCHEMY_URL_KEY)

    connectable = async_engine_from_config(
        configuration,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)

    await connectable.dispose()


def run_migrations_online() -> None:
    """Run migrations in online mode (async)."""
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
