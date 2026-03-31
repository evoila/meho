# SPDX-License-Identifier: AGPL-3.0-only
"""
Alembic environment configuration for meho_knowledge.
"""

import asyncio
import os
import sys
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

# Add repository root to path to import meho_app
# .../meho_app/modules/knowledge/alembic/env.py -> .../
sys.path.insert(
    0,
    os.path.dirname(
        os.path.dirname(
            os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        )
    ),
)

# Import models so Alembic can detect them
from meho_app.modules.knowledge.models import Base

# Module-specific version table to avoid collisions with other modules.
# All modules (memory, agent, topology) already use scoped names.
# Existing deployments: run scripts/migrate_knowledge_alembic_version.py once before
# running knowledge migrations to move version state to the new table.
VERSION_TABLE = "alembic_version_meho_knowledge"

# Alembic Config object
config = context.config

# Interpret the config file for Python logging
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Set target metadata for autogenerate
target_metadata = Base.metadata

# Override sqlalchemy.url from environment if available
database_url = os.getenv("DATABASE_URL")
if database_url:
    config.set_main_option("sqlalchemy.url", database_url)


def run_migrations_offline() -> None:
    """
    Run migrations in 'offline' mode.

    This configures the context with just a URL and not an Engine,
    though an Engine is acceptable here as well.
    """
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        version_table=VERSION_TABLE,
    )

    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    """Run migrations with connection"""
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        version_table=VERSION_TABLE,
    )

    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    """Run migrations in async mode"""
    configuration = config.get_section(config.config_ini_section, {})
    configuration["sqlalchemy.url"] = database_url or config.get_main_option("sqlalchemy.url")

    connectable = async_engine_from_config(
        configuration,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)

    await connectable.dispose()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode (async)"""
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
