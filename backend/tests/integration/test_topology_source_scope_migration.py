# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""PostgreSQL tests for migration ``0108_add_graph_node_source_scope`` (#3739).

The SQLite twin is ``tests/migrations/test_migration_0108_add_graph_node_source_scope.py``.
This module covers what only PostgreSQL can show:

* **NULL uniqueness** -- a plain unique index treats NULLs as distinct,
  so a fourth nullable ``source_scope`` key alone would let two
  unresolved rows share a name. ``NULLS NOT DISTINCT`` does not. This
  is why the expand step keeps the old ``(tenant_id, kind, name)`` key.
* **Old key kept** -- two rows with the same name but different
  ``target:`` scopes are still rejected before the cut-over.
* **Upgrade / downgrade before the cut-over** on a legacy fixture:
  legacy columns unchanged, ``global`` only for ``kind='target'``, the
  snapshot written, the CHECK enforced, every row and the snapshot kept
  on downgrade, and downgrade refused when it would lose scoped data.
* **Moved target** -- a refresh that moves ``target_id`` after the
  upgrade does not rewrite the snapshot across a downgrade and a
  re-upgrade.

Tests are sync because :func:`alembic.command.upgrade` runs its own
:func:`asyncio.run`; database reads go through a short
:func:`asyncio.run` per call. Every test leaves the container at
``head``.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import insert, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine

from meho_backplane.db.engine import reset_engine_for_testing
from meho_backplane.db.migrations import alembic_config
from meho_backplane.db.models import Target, Tenant
from tests.integration.conftest import DOCKER_AVAILABLE, SKIP_REASON

pytestmark = pytest.mark.skipif(not DOCKER_AVAILABLE, reason=SKIP_REASON)

_REVISION = "0108"
_DOWN_REVISION = "0107"


async def _run_sql(url: str, statements: list[tuple[str, dict[str, Any]]]) -> list[Any]:
    """Run *statements* in one transaction; return the last result's rows."""
    engine = create_async_engine(url)
    rows: list[Any] = []
    try:
        async with engine.begin() as conn:
            for sql, params in statements:
                result = await conn.execute(text(sql), params)
                rows = list(result.all()) if result.returns_rows else []
    finally:
        await engine.dispose()
    return rows


def _sql(url: str, sql: str, **params: Any) -> list[Any]:
    return asyncio.run(_run_sql(url, [(sql, params)]))


@pytest.fixture
def migration_cfg(integration_env: None, async_pg_url: str) -> Iterator[tuple[Config, str]]:
    """Alembic config on the container; graph and snapshot cleared; head afterwards.

    The snapshot table survives a downgrade, so it is cleared here as
    well. Otherwise rows from one test would be kept by the next test's
    upgrade.
    """
    reset_engine_for_testing()
    cfg = alembic_config()
    cfg.set_main_option("sqlalchemy.url", async_pg_url)
    _sql(async_pg_url, "TRUNCATE graph_node, graph_node_scope_snapshot CASCADE")
    try:
        yield cfg, async_pg_url
    finally:
        _sql(async_pg_url, "TRUNCATE graph_node CASCADE")
        command.upgrade(cfg, "head")
        _sql(async_pg_url, "TRUNCATE graph_node_scope_snapshot")
        reset_engine_for_testing()


def _seed_tenant_and_target(url: str) -> tuple[str, str]:
    """Insert a tenant and a target at ``head`` (0108 does not touch either table)."""
    tenant_id = str(uuid.uuid4())

    async def _insert() -> None:
        engine = create_async_engine(url)
        try:
            async with engine.begin() as conn:
                await conn.execute(
                    insert(Tenant).values(
                        id=uuid.UUID(tenant_id), slug=f"scope-{tenant_id[:8]}", name="Scope"
                    )
                )
        finally:
            await engine.dispose()

    asyncio.run(_insert())
    return tenant_id, _insert_target(url, tenant_id, "cluster-a")


def _insert_target(url: str, tenant_id: str, name: str) -> str:
    target_id = str(uuid.uuid4())

    async def _insert() -> None:
        engine = create_async_engine(url)
        try:
            async with engine.begin() as conn:
                await conn.execute(
                    insert(Target).values(
                        id=uuid.UUID(target_id),
                        tenant_id=uuid.UUID(tenant_id),
                        name=name,
                        aliases=[],
                        product="kubernetes",
                        host="k8s.example.test",
                    )
                )
        finally:
            await engine.dispose()

    asyncio.run(_insert())
    return target_id


def _seed_legacy_nodes(url: str, tenant_id: str, target_id: str) -> dict[str, str]:
    """Write pre-0108 nodes: the target itself, a shared bare name, a curated VM."""
    ids: dict[str, str] = {}
    statements: list[tuple[str, dict[str, Any]]] = []
    for key, kind, name, node_target, source in (
        ("target_node", "target", "cluster-a", target_id, "auto"),
        ("namespace_node", "namespace", "default", target_id, "auto"),
        ("vm_node", "vm", "vm-1", None, "curated"),
    ):
        ids[key] = str(uuid.uuid4())
        statements.append(
            (
                "INSERT INTO graph_node (id, tenant_id, kind, name, target_id, source, "
                "properties, discovered_by) VALUES (:id, :tenant_id, :kind, :name, "
                ":target_id, :source, '{}'::jsonb, 'kubernetes')",
                {
                    "id": uuid.UUID(ids[key]),
                    "tenant_id": uuid.UUID(tenant_id),
                    "kind": kind,
                    "name": name,
                    "target_id": uuid.UUID(node_target) if node_target else None,
                    "source": source,
                },
            )
        )
    asyncio.run(_run_sql(url, statements))
    return ids


def _legacy_view(url: str) -> list[tuple[Any, ...]]:
    rows = _sql(
        url,
        "SELECT id, tenant_id, kind, name, target_id, source, properties, discovered_by, "
        "first_seen, last_seen FROM graph_node ORDER BY kind, name",
    )
    return [tuple(row) for row in rows]


def test_null_scope_is_distinct_in_a_plain_unique_index(
    migration_cfg: tuple[Config, str],
) -> None:
    """Why the old three-column key must stay until every row has a scope."""
    _, url = migration_cfg
    plain = asyncio.run(
        _run_sql(
            url,
            [
                ("CREATE TEMP TABLE scope_probe (kind text, name text, source_scope text)", {}),
                ("CREATE UNIQUE INDEX ON scope_probe (kind, name, source_scope)", {}),
                ("INSERT INTO scope_probe VALUES ('namespace', 'default', NULL)", {}),
                ("INSERT INTO scope_probe VALUES ('namespace', 'default', NULL)", {}),
                ("SELECT count(*) FROM scope_probe", {}),
            ],
        )
    )
    assert plain[0][0] == 2, "a plain unique index lets two NULL-scope rows share a name"

    with pytest.raises(IntegrityError):
        asyncio.run(
            _run_sql(
                url,
                [
                    ("CREATE TEMP TABLE scope_probe (kind text, name text, source_scope text)", {}),
                    (
                        "CREATE UNIQUE INDEX ON scope_probe (kind, name, source_scope) "
                        "NULLS NOT DISTINCT",
                        {},
                    ),
                    ("INSERT INTO scope_probe VALUES ('namespace', 'default', NULL)", {}),
                    ("INSERT INTO scope_probe VALUES ('namespace', 'default', NULL)", {}),
                ],
            )
        )


def test_old_key_rejects_scoped_duplicates_before_cutover(
    migration_cfg: tuple[Config, str],
) -> None:
    _, url = migration_cfg
    tenant_id, _ = _seed_tenant_and_target(url)
    insert = (
        "INSERT INTO graph_node (id, tenant_id, kind, name, properties, discovered_by, "
        "source_scope) VALUES (gen_random_uuid(), :tenant_id, 'namespace', "
        "'default', '{}'::jsonb, 'test', :scope)"
    )
    _sql(url, insert, tenant_id=uuid.UUID(tenant_id), scope=f"target:{uuid.uuid4()}")

    with pytest.raises(IntegrityError, match="graph_node_tenant_kind_name_idx"):
        _sql(url, insert, tenant_id=uuid.UUID(tenant_id), scope=f"target:{uuid.uuid4()}")


def test_upgrade_and_downgrade_before_cutover_keep_legacy_rows(
    migration_cfg: tuple[Config, str],
) -> None:
    cfg, url = migration_cfg
    tenant_id, target_id = _seed_tenant_and_target(url)
    command.downgrade(cfg, _DOWN_REVISION)
    ids = _seed_legacy_nodes(url, tenant_id, target_id)
    before = _legacy_view(url)

    command.upgrade(cfg, _REVISION)

    assert _legacy_view(url) == before
    scopes = {
        str(row.id): (row.source_scope, row.source_scope_provenance)
        for row in _sql(url, "SELECT id, source_scope, source_scope_provenance FROM graph_node")
    }
    assert scopes == {
        ids["target_node"]: ("global", "migration:0108"),
        ids["namespace_node"]: (None, None),
        ids["vm_node"]: (None, None),
    }
    snapshot = {
        str(row.node_id): row
        for row in _sql(
            url,
            "SELECT node_id, last_target_id, scope_status, schema_version "
            "FROM graph_node_scope_snapshot",
        )
    }
    assert set(snapshot) == set(ids.values())
    assert snapshot[ids["namespace_node"]].scope_status == "unresolved"
    assert str(snapshot[ids["namespace_node"]].last_target_id) == target_id
    assert snapshot[ids["target_node"]].scope_status == "global"
    assert {row.schema_version for row in snapshot.values()} == {1}

    with pytest.raises(IntegrityError, match="ck_graph_node_source_scope"):
        _sql(
            url,
            "UPDATE graph_node SET source_scope = :scope WHERE id = :id",
            scope="target:gggggggg-0000-0000-0000-000000000000",
            id=uuid.UUID(ids["vm_node"]),
        )

    command.downgrade(cfg, _DOWN_REVISION)

    assert _legacy_view(url) == before
    columns = {
        row.column_name
        for row in _sql(
            url,
            "SELECT column_name FROM information_schema.columns WHERE table_name = 'graph_node'",
        )
    }
    assert "source_scope" not in columns
    assert "source_scope_provenance" not in columns
    kept = {
        str(row.node_id): (row.last_target_id and str(row.last_target_id), row.scope_status)
        for row in _sql(
            url, "SELECT node_id, last_target_id, scope_status FROM graph_node_scope_snapshot"
        )
    }
    assert kept == {
        ids["target_node"]: (target_id, "global"),
        ids["namespace_node"]: (target_id, "unresolved"),
        ids["vm_node"]: (None, "unresolved"),
    }, "the repair input must survive the downgrade"


def test_downgrade_keeps_the_snapshot_when_the_target_moved(
    migration_cfg: tuple[Config, str],
) -> None:
    """A refresh that moves ``target_id`` after the upgrade must not rewrite the input."""
    cfg, url = migration_cfg
    tenant_id, target_id = _seed_tenant_and_target(url)
    command.downgrade(cfg, _DOWN_REVISION)
    ids = _seed_legacy_nodes(url, tenant_id, target_id)
    command.upgrade(cfg, _REVISION)
    other_target = _insert_target(url, tenant_id, "cluster-b")
    _sql(
        url,
        "UPDATE graph_node SET target_id = :target_id WHERE id = :id",
        target_id=uuid.UUID(other_target),
        id=uuid.UUID(ids["namespace_node"]),
    )

    def _snapshot_row() -> tuple[str, str]:
        row = _sql(
            url,
            "SELECT last_target_id, scope_status FROM graph_node_scope_snapshot "
            "WHERE node_id = :id",
            id=uuid.UUID(ids["namespace_node"]),
        )[0]
        return str(row.last_target_id), row.scope_status

    assert _snapshot_row() == (target_id, "unresolved")

    command.downgrade(cfg, _DOWN_REVISION)
    assert _snapshot_row() == (target_id, "unresolved")

    command.upgrade(cfg, _REVISION)
    assert _snapshot_row() == (target_id, "unresolved")
    live = _sql(
        url, "SELECT target_id FROM graph_node WHERE id = :id", id=uuid.UUID(ids["namespace_node"])
    )
    assert str(live[0].target_id) == other_target
    assert len(_sql(url, "SELECT node_id FROM graph_node_scope_snapshot")) == 3


def test_downgrade_refuses_when_scoped_rows_exist(migration_cfg: tuple[Config, str]) -> None:
    cfg, url = migration_cfg
    tenant_id, target_id = _seed_tenant_and_target(url)
    command.downgrade(cfg, _DOWN_REVISION)
    ids = _seed_legacy_nodes(url, tenant_id, target_id)
    command.upgrade(cfg, _REVISION)
    _sql(
        url,
        "UPDATE graph_node SET source_scope = :scope, source_scope_provenance = 'repair' "
        "WHERE id = :id",
        scope=f"target:{target_id}",
        id=uuid.UUID(ids["namespace_node"]),
    )

    with pytest.raises(RuntimeError, match="Roll forward"):
        command.downgrade(cfg, _DOWN_REVISION)

    kept = _sql(
        url,
        "SELECT source_scope FROM graph_node WHERE id = :id",
        id=uuid.UUID(ids["namespace_node"]),
    )
    assert kept[0].source_scope == f"target:{target_id}"
