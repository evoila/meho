# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""PostgreSQL tests for migration ``0175_add_people_behind_columns``.

The SQLite twin is
``tests/migrations/test_migration_0175_add_people_behind_columns.py`` and
holds the seed data. On PostgreSQL the fill reads the ``jsonb`` payload of
the audit rows with ``->>``. The tests check:

* the upgrade adds the three columns and the downgrade drops them;
* ``agent_definition.updated_by_sub`` comes from the newest successful
  ``agent.edit`` audit row of the definition, else from ``created_by_sub``;
  an edit from before the definition was created does not count.

Tests are sync because :func:`alembic.command.upgrade` runs its own
:func:`asyncio.run`. Every test leaves the container at ``head``.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import create_async_engine

from meho_backplane.db.engine import reset_engine_for_testing
from meho_backplane.db.migrations import alembic_config
from tests.integration.conftest import DOCKER_AVAILABLE, SKIP_REASON
from tests.migrations.test_migration_0175_add_people_behind_columns import (
    _DOWN_REVISION,
    _NEW,
    _OTHER_TENANT,
    _REVISION,
    _TENANT,
    AUDIT_ROWS,
    DEFINITIONS,
    EXPECTED,
)

pytestmark = pytest.mark.skipif(not DOCKER_AVAILABLE, reason=SKIP_REASON)

_TENANTS = (_TENANT, _OTHER_TENANT)


async def _clear(url: str) -> None:
    engine = create_async_engine(url)
    try:
        async with engine.begin() as conn:
            # The audit log takes no row DELETE on PostgreSQL; TRUNCATE is allowed.
            await conn.execute(text("TRUNCATE audit_log"))
            await conn.execute(
                text("DELETE FROM agent_definition WHERE tenant_id IN (:one, :two)"),
                {"one": uuid.UUID(_TENANT), "two": uuid.UUID(_OTHER_TENANT)},
            )
            await conn.execute(
                text("DELETE FROM tenant WHERE id IN (:one, :two)"),
                {"one": uuid.UUID(_TENANT), "two": uuid.UUID(_OTHER_TENANT)},
            )
    finally:
        await engine.dispose()


async def _seed(url: str) -> None:
    engine = create_async_engine(url)
    created = datetime(2026, 10, 1, tzinfo=UTC)
    try:
        async with engine.begin() as conn:
            for tenant in _TENANTS:
                await conn.execute(
                    text(
                        "INSERT INTO tenant (id, slug, name, created_at) "
                        "VALUES (:id, :slug, 'tenant', :at)"
                    ),
                    {"id": uuid.UUID(tenant), "slug": f"t-{tenant[-4:]}", "at": created},
                )
            for definition_id, tenant, name, creator, created_at in DEFINITIONS:
                await conn.execute(
                    text(
                        "INSERT INTO agent_definition (id, tenant_id, name, identity_ref, "
                        "model_tier, system_prompt, toolset, turn_budget, enabled, "
                        "created_by_sub, created_at, updated_at) VALUES (:id, :tenant, :name, "
                        "'agent:x', 'small', 'p', CAST('{}' AS jsonb), 5, true, :creator, "
                        ":at, :at)"
                    ),
                    {
                        "id": uuid.UUID(definition_id),
                        "tenant": uuid.UUID(tenant),
                        "name": name,
                        "creator": creator,
                        "at": datetime.fromisoformat(created_at).replace(tzinfo=UTC),
                    },
                )
            for sub, tenant, op_id, agent_name, status, at in AUDIT_ROWS:
                await conn.execute(
                    text(
                        "INSERT INTO audit_log (id, occurred_at, operator_sub, method, path, "
                        "status_code, payload, tenant_id, event_class, sandbox_mode) VALUES "
                        "(:id, :at, :sub, 'PATCH', '/api/v1/agents/x', :status, "
                        "CAST(:payload AS jsonb), :tenant, 'legacy_request', false)"
                    ),
                    {
                        "id": uuid.uuid4(),
                        "at": datetime.fromisoformat(at).replace(tzinfo=UTC),
                        "sub": sub,
                        "status": status,
                        "payload": json.dumps({"op_id": op_id, "agent_name": agent_name}),
                        "tenant": uuid.UUID(tenant),
                    },
                )
    finally:
        await engine.dispose()


async def _updated_by(url: str) -> list[tuple[str, str | None]]:
    engine = create_async_engine(url)
    try:
        async with engine.connect() as conn:
            result = await conn.execute(
                text(
                    "SELECT id, updated_by_sub FROM agent_definition "
                    "WHERE tenant_id = :tenant ORDER BY id"
                ),
                {"tenant": uuid.UUID(_TENANT)},
            )
            return [(str(row[0]), row[1]) for row in result]
    finally:
        await engine.dispose()


async def _has_columns(url: str) -> dict[str, bool]:
    engine = create_async_engine(url)
    try:
        async with engine.connect() as conn:

            def _check(sync_conn: object) -> dict[str, bool]:
                inspector = inspect(sync_conn)
                return {
                    table: column in {c["name"] for c in inspector.get_columns(table)}
                    for table, column in _NEW.items()
                }

            return await conn.run_sync(_check)
    finally:
        await engine.dispose()


@pytest.fixture
def migration_cfg(integration_env: None, async_pg_url: str) -> Iterator[tuple[Config, str]]:
    """Seeded container at ``0174``; cleared and back at ``head`` afterwards."""
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


def test_up_fills_updated_by_sub_and_down_drops_the_columns(
    migration_cfg: tuple[Config, str],
) -> None:
    cfg, url = migration_cfg
    assert not any(asyncio.run(_has_columns(url)).values())

    command.upgrade(cfg, _REVISION)
    assert all(asyncio.run(_has_columns(url)).values())
    assert asyncio.run(_updated_by(url)) == EXPECTED

    command.downgrade(cfg, _DOWN_REVISION)
    assert not any(asyncio.run(_has_columns(url)).values())
