# SPDX-License-Identifier: AGPL-3.0-only
"""
Alembic environment configuration for MEHO Connectors.

This is the shared alembic location for all connector-related migrations.
"""

import asyncio
import os
import sys
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

SQLALCHEMY_URL_KEY = "sqlalchemy.url"

# Add repository root to path to import meho_app
# Path: alembic/env.py -> connectors -> modules -> meho_app -> repo_root (5 levels up)
_this_file = os.path.abspath(__file__)
_repo_root = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(_this_file))))
)
sys.path.insert(0, _repo_root)

# Import all models so Alembic can detect them
try:
    from meho_app.database import Base

    # Import all SQLAlchemy models from the connectors module
    from meho_app.modules.connectors.models import (
        ConnectorModel,  # noqa: F401 -- re-export
        ConnectorOperationModel,  # noqa: F401 -- re-export
        ConnectorTypeModel,  # noqa: F401 -- re-export
        UserCredentialModel,  # noqa: F401 -- re-export
    )
    from meho_app.modules.connectors.rest.models import (
        EndpointDescriptorModel,  # noqa: F401 -- re-export
        OpenAPISpecModel,  # noqa: F401 -- re-export
    )
    from meho_app.modules.connectors.soap.db_models import (
        SoapOperationDescriptorModel,  # noqa: F401 -- re-export
        SoapTypeDescriptorModel,  # noqa: F401 -- re-export
    )
except ImportError as e:
    import logging as _logging

    _fallback_logger = _logging.getLogger(__name__)
    _fallback_logger.warning("Could not import models", exc_info=e)
    Base = None

# Alembic Config object
config = context.config

# Interpret the config file for Python logging
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Set target metadata for autogenerate
target_metadata = Base.metadata if Base is not None else None

# Keep the same version table name for backward compatibility
VERSION_TABLE = "alembic_version_meho_openapi"

# Override sqlalchemy.url from environment if available
database_url = os.getenv("DATABASE_URL")
if database_url:
    config.set_main_option(SQLALCHEMY_URL_KEY, database_url)


def run_migrations_offline() -> None:
    """
    Run migrations in 'offline' mode.

    This configures the context with just a URL and not an Engine,
    though an Engine is acceptable here as well.
    """
    url = config.get_main_option(SQLALCHEMY_URL_KEY)
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
    configuration[SQLALCHEMY_URL_KEY] = (
        database_url or config.get_main_option(SQLALCHEMY_URL_KEY) or ""
    )

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
