# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Migration ``0185`` on a real PostgreSQL, with the append-only trigger in place.

The upgrade only adds the column ``audit_log.secret_read_as``. It runs no
``UPDATE``, so the ``audit_log`` append-only trigger does not fire: the
upgrade succeeds on a table that holds rows, the old rows read as empty,
and the trigger still refuses an ``UPDATE`` afterwards. The downgrade
drops the column.

Needs Docker (testcontainers). CI has it; a sandbox without it skips.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import create_async_engine

from meho_backplane.db.migrations import alembic_config
from tests._pgvector_image import pgvector_test_image


def _docker_socket_present() -> bool:
    """Same Docker heuristic as the other testcontainers-PG suites."""
    return Path("/var/run/docker.sock").exists() or os.environ.get("DOCKER_HOST") is not None


pytestmark = pytest.mark.skipif(
    not _docker_socket_present(),
    reason=(
        "Docker socket unavailable in this sandbox; runs in CI where containers are provisioned."
    ),
)


def _alembic(async_url: str, action: str, revision: str) -> None:
    """Run ``alembic upgrade|downgrade <revision>`` against *async_url*."""
    cfg = alembic_config()
    cfg.set_main_option("sqlalchemy.url", async_url)
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("DATABASE_URL", async_url)
        getattr(command, action)(cfg, revision)


@pytest.fixture(scope="module")
def async_url() -> Iterator[str]:
    """One PostgreSQL migrated to ``0184``, with one old audit row."""
    from testcontainers.postgres import PostgresContainer

    with PostgresContainer(pgvector_test_image()) as container:
        url = container.get_connection_url().replace(
            "postgresql+psycopg2://", "postgresql+asyncpg://"
        )
        _alembic(url, "upgrade", "0184")
        yield url


async def _scalar(url: str, sql: str, **params: object) -> object:
    engine = create_async_engine(url)
    try:
        async with engine.begin() as conn:
            return (await conn.execute(text(sql), params)).scalar()
    finally:
        await engine.dispose()


def test_upgrade_with_trigger_keeps_old_rows_empty_and_guard_in_place(async_url: str) -> None:
    row_id = uuid.uuid4()
    asyncio.run(
        _scalar(
            async_url,
            "INSERT INTO audit_log (id, occurred_at, operator_sub, method, path, status_code, "
            "payload) VALUES (:id, now(), 'old-row-sub', 'DISPATCH', 'test.op', 200, '{}') "
            "RETURNING id",
            id=row_id,
        )
    )
    _alembic(async_url, "upgrade", "0185")

    old = asyncio.run(
        _scalar(async_url, "SELECT secret_read_as FROM audit_log WHERE id = :id", id=row_id)
    )
    assert old is None

    with pytest.raises(DBAPIError, match="append-only"):
        asyncio.run(
            _scalar(
                async_url,
                "UPDATE audit_log SET secret_read_as = 'x' WHERE id = :id RETURNING id",
                id=row_id,
            )
        )


def test_downgrade_drops_the_column(async_url: str) -> None:
    _alembic(async_url, "upgrade", "0185")
    _alembic(async_url, "downgrade", "0184")
    present = asyncio.run(
        _scalar(
            async_url,
            "SELECT count(*) FROM information_schema.columns "
            "WHERE table_name = 'audit_log' AND column_name = 'secret_read_as'",
        )
    )
    assert present == 0
