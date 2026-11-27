# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Migration ``0191`` on a real PostgreSQL, with the append-only trigger in place.

On PostgreSQL the migration adds the column and then the CHECK
``NOT VALID``. The tests check that the upgrade works on a table that holds
rows, that an old row keeps ``NULL``, that the CHECK refuses a value outside
the five kinds, that the append-only trigger still refuses an ``UPDATE``,
and that the downgrade drops the CHECK and the column.

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

from meho_backplane.auth.caller_kind import CALLER_KINDS
from meho_backplane.db.migrations import alembic_config
from tests._pgvector_image import pgvector_test_image

_REVISION = "0191"
_DOWN_REVISION = "0190"
_CHECK = "ck_audit_log_caller_kind"


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
    """One PostgreSQL migrated to ``0190``."""
    from testcontainers.postgres import PostgresContainer

    with PostgresContainer(pgvector_test_image()) as container:
        url = container.get_connection_url().replace(
            "postgresql+psycopg2://", "postgresql+asyncpg://"
        )
        _alembic(url, "upgrade", _DOWN_REVISION)
        yield url


async def _scalar(url: str, sql: str, **params: object) -> object:
    engine = create_async_engine(url)
    try:
        async with engine.begin() as conn:
            return (await conn.execute(text(sql), params)).scalar()
    finally:
        await engine.dispose()


def _insert(url: str, caller_kind: str | None) -> uuid.UUID:
    row_id = uuid.uuid4()
    if caller_kind is None:
        sql = (
            "INSERT INTO audit_log (id, occurred_at, operator_sub, method, path, status_code, "
            "payload) VALUES (:id, now(), 'op', 'DISPATCH', 'test.op', 200, '{}') RETURNING id"
        )
        asyncio.run(_scalar(url, sql, id=row_id))
    else:
        sql = (
            "INSERT INTO audit_log (id, occurred_at, operator_sub, method, path, status_code, "
            "payload, caller_kind) VALUES (:id, now(), 'op', 'DISPATCH', 'test.op', 200, '{}', "
            ":kind) RETURNING id"
        )
        asyncio.run(_scalar(url, sql, id=row_id, kind=caller_kind))
    return row_id


def _caller_kind_of(url: str, row_id: uuid.UUID) -> object:
    return asyncio.run(_scalar(url, "SELECT caller_kind FROM audit_log WHERE id = :id", id=row_id))


def test_upgrade_keeps_old_rows_null_and_check_refuses_other_values(async_url: str) -> None:
    old_id = _insert(async_url, None)

    _alembic(async_url, "upgrade", _REVISION)

    assert _caller_kind_of(async_url, old_id) is None
    for kind in CALLER_KINDS:
        assert _caller_kind_of(async_url, _insert(async_url, kind)) == kind
    with pytest.raises(DBAPIError, match=_CHECK):
        _insert(async_url, "agent")
    with pytest.raises(DBAPIError, match="append-only"):
        asyncio.run(
            _scalar(
                async_url,
                "UPDATE audit_log SET caller_kind = 'person' WHERE id = :id RETURNING id",
                id=old_id,
            )
        )


def test_downgrade_drops_the_check_and_the_column(async_url: str) -> None:
    _alembic(async_url, "upgrade", _REVISION)
    _alembic(async_url, "downgrade", _DOWN_REVISION)
    column = asyncio.run(
        _scalar(
            async_url,
            "SELECT count(*) FROM information_schema.columns "
            "WHERE table_name = 'audit_log' AND column_name = 'caller_kind'",
        )
    )
    check = asyncio.run(
        _scalar(async_url, "SELECT count(*) FROM pg_constraint WHERE conname = :name", name=_CHECK)
    )
    assert (column, check) == (0, 0)
