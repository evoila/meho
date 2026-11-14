# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""PostgreSQL tests for migration ``0155_agent_read_everything_at_upgrade``.

The SQLite twin is
``tests/migrations/test_migration_0155_agent_read_everything_at_upgrade.py``
and holds the shared seed data and helpers. This module runs the same
checks on PostgreSQL, where ``id`` and ``tenant_id`` are real ``uuid``
columns and ``revoked`` is a real ``boolean``:

* the permission answers are the same before and after the upgrade;
* the right principals get the row;
* the upgrade is safe to run twice, and the downgrade deletes only the
  marker rows.

Tests are sync because :func:`alembic.command.upgrade` runs its own
:func:`asyncio.run`. Every test leaves the container at ``head``.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from meho_backplane.db.engine import reset_engine_for_testing
from meho_backplane.db.migrations import alembic_config
from tests.integration.conftest import DOCKER_AVAILABLE, SKIP_REASON
from tests.migrations.test_migration_0155_agent_read_everything_at_upgrade import (
    _DOWN_REVISION,
    _OTHER_TENANT,
    _REVISION,
    _TENANT,
    _add_again,
    _expected_marker_subs,
    _marker_subs,
    _operators,
    _rows,
    _seed,
    _verdict_matrix,
)

pytestmark = pytest.mark.skipif(not DOCKER_AVAILABLE, reason=SKIP_REASON)


async def _clear(url: str) -> None:
    engine = create_async_engine(url)
    try:
        async with engine.begin() as conn:
            await conn.execute(text("TRUNCATE agent_permission, agent_principal"))
            await conn.execute(
                text("DELETE FROM tenant WHERE id IN (:one, :two)"),
                {"one": _TENANT, "two": _OTHER_TENANT},
            )
    finally:
        await engine.dispose()


@pytest.fixture
def migration_cfg(integration_env: None, async_pg_url: str) -> Iterator[tuple[Config, str]]:
    """Seeded container at ``0154``; cleared and back at ``head`` afterwards."""
    reset_engine_for_testing()
    cfg = alembic_config()
    cfg.set_main_option("sqlalchemy.url", async_pg_url)
    asyncio.run(_clear(async_pg_url))
    command.downgrade(cfg, _DOWN_REVISION)
    asyncio.run(_seed(async_pg_url))
    try:
        yield cfg, async_pg_url
    finally:
        asyncio.run(_clear(async_pg_url))
        command.upgrade(cfg, "head")
        reset_engine_for_testing()


def test_answers_do_not_change_and_the_right_principals_get_the_row(
    migration_cfg: tuple[Config, str],
) -> None:
    cfg, url = migration_cfg
    operators = _operators()

    before = asyncio.run(_verdict_matrix(url, operators))
    command.upgrade(cfg, _REVISION)
    after = asyncio.run(_verdict_matrix(url, operators))

    assert before == after
    assert _marker_subs(url) == _expected_marker_subs()


def test_safe_to_run_twice_and_downgrade_removes_only_marker_rows(
    migration_cfg: tuple[Config, str],
) -> None:
    cfg, url = migration_cfg
    original = _rows(url)

    command.upgrade(cfg, _REVISION)
    once = _rows(url)
    assert asyncio.run(_add_again(url)) == 0
    assert _rows(url) == once

    command.downgrade(cfg, _DOWN_REVISION)
    assert _rows(url) == original
