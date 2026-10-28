# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for Alembic migration ``0108_add_graph_node_source_scope``.

#3739 is the expand-only step of source-qualified topology. The
migration adds ``graph_node.source_scope`` / ``source_scope_provenance``,
the portable ``ck_graph_node_source_scope`` CHECK, a non-unique scope
index and the ``graph_node_scope_snapshot`` repair input, and backfills
``global`` only for ``kind = 'target'`` rows.

Coverage (SQLite driver; the PostgreSQL twin lives in
``tests/integration/test_topology_source_scope_migration.py``):

* **Legacy fixture** -- rows written at ``0107`` stay readable and
  unchanged; only ``kind='target'`` rows get ``global``; everything else
  stays NULL even when ``target_id`` is set (no scope guessing).
* **Snapshot** -- one row per pre-existing node with the last target,
  ``scope_status`` and ``schema_version = 1``.
* **Old key kept** -- ``graph_node_tenant_kind_name_idx`` still rejects a
  duplicate name, whatever the scope.
* **CHECK** -- the three scope forms are accepted; malformed
  ``target:`` / ``legacy:`` forms are rejected.
* **Downgrade** -- keeps every row and the snapshot, drops the added
  columns, refuses when a scope value this migration did not write is
  present; round-trip. A ``target_id`` moved after the upgrade does not
  rewrite the snapshot.

Every step targets this migration's own revision (``0108``) and its
``down_revision`` (``0107``), never ``head``.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine as sa_create_engine
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError

from meho_backplane.db.engine import reset_engine_for_testing
from meho_backplane.db.migrations import alembic_config
from meho_backplane.settings import get_settings

_REVISION = "0108"
_DOWN_REVISION = "0107"
_UUID = "0f8fad5b-d9cb-469f-a165-70867728950e"


@pytest.fixture
def alembic_cfg(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> Iterator[tuple[Config, str]]:
    """Pin env, reset caches, return an Alembic config + sync URL."""
    db_path = tmp_path / "migration_0108.db"
    async_url = f"sqlite+aiosqlite:///{db_path}"
    sync_url = f"sqlite:///{db_path}"
    monkeypatch.setenv("DATABASE_URL", async_url)
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    reset_engine_for_testing()

    cfg = alembic_config()
    cfg.set_main_option("sqlalchemy.url", async_url)
    try:
        yield cfg, sync_url
    finally:
        get_settings.cache_clear()
        reset_engine_for_testing()


def _execute(sync_url: str, sql: str, params: dict[str, Any] | None = None) -> None:
    eng = sa_create_engine(sync_url)
    try:
        with eng.begin() as conn:
            conn.execute(text(sql), params or {})
    finally:
        eng.dispose()


def _fetch(sync_url: str, sql: str, params: dict[str, Any] | None = None) -> list[Any]:
    eng = sa_create_engine(sync_url)
    try:
        with eng.connect() as conn:
            return list(conn.execute(text(sql), params or {}).all())
    finally:
        eng.dispose()


def _seed_legacy_graph(sync_url: str) -> dict[str, str]:
    """Write a pre-0108 graph: a tenant, a target and three node shapes.

    * ``target`` node ``cluster-a`` -- the registered target itself.
    * ``namespace`` node ``default`` -- last seen by ``cluster-a``
      (``target_id`` set), the bare name two clusters can share.
    * ``vm`` node ``vm-1`` -- curated, no ``target_id``.
    """
    tenant_id = str(uuid.uuid4())
    now = datetime.now(UTC).isoformat()
    _execute(
        sync_url,
        "INSERT INTO tenant (id, slug, name, created_at) VALUES (:id, :slug, 'T', :now)",
        {"id": tenant_id, "slug": f"t-{tenant_id[:8]}", "now": now},
    )
    target_id = _insert_target(sync_url, tenant_id, "cluster-a")
    ids = {"tenant": tenant_id, "target": target_id}
    for key, kind, name, node_target, source in (
        ("target_node", "target", "cluster-a", target_id, "auto"),
        ("namespace_node", "namespace", "default", target_id, "auto"),
        ("vm_node", "vm", "vm-1", None, "curated"),
    ):
        node_id = str(uuid.uuid4())
        _execute(
            sync_url,
            "INSERT INTO graph_node (id, tenant_id, kind, name, target_id, source, "
            "properties, discovered_by, first_seen) VALUES (:id, :tenant_id, :kind, "
            ":name, :target_id, :source, '{}', 'kubernetes', :now)",
            {
                "id": node_id,
                "tenant_id": tenant_id,
                "kind": kind,
                "name": name,
                "target_id": node_target,
                "source": source,
                "now": now,
            },
        )
        ids[key] = node_id
    return ids


def _insert_target(sync_url: str, tenant_id: str, name: str) -> str:
    target_id = str(uuid.uuid4())
    now = datetime.now(UTC).isoformat()
    _execute(
        sync_url,
        "INSERT INTO targets (id, tenant_id, name, aliases, product, host, created_at, "
        "updated_at) VALUES (:id, :tenant_id, :name, '[]', 'kubernetes', "
        "'k8s.example.test', :now, :now)",
        {"id": target_id, "tenant_id": tenant_id, "name": name, "now": now},
    )
    return target_id


def _insert_scoped_node(sync_url: str, tenant_id: str, name: str, scope: str | None) -> None:
    _execute(
        sync_url,
        "INSERT INTO graph_node (id, tenant_id, kind, name, properties, discovered_by, "
        "first_seen, source_scope) VALUES (:id, :tenant_id, 'namespace', :name, '{}', "
        "'test', :now, :scope)",
        {
            "id": str(uuid.uuid4()),
            "tenant_id": tenant_id,
            "name": name,
            "now": datetime.now(UTC).isoformat(),
            "scope": scope,
        },
    )


def _snapshot_view(sync_url: str) -> list[tuple[Any, ...]]:
    """The repair input, in a stable order."""
    rows = _fetch(
        sync_url,
        "SELECT node_id, tenant_id, kind, name, last_target_id, source, discovered_by, "
        "scope_status, schema_version, captured_at FROM graph_node_scope_snapshot "
        "ORDER BY kind, name",
    )
    return [tuple(row) for row in rows]


def _legacy_view(sync_url: str) -> list[tuple[Any, ...]]:
    """The pre-0108 columns of every node, in a stable order."""
    rows = _fetch(
        sync_url,
        "SELECT id, tenant_id, kind, name, target_id, source, properties, discovered_by "
        "FROM graph_node ORDER BY kind, name",
    )
    return [tuple(row) for row in rows]


def test_upgrade_backfills_only_target_nodes_and_keeps_legacy_rows(
    alembic_cfg: tuple[Config, str],
    copy_migrated_db: Callable[[Config, str], None],
) -> None:
    """Only ``kind='target'`` rows get ``global``; other rows stay unresolved."""
    cfg, sync_url = alembic_cfg
    copy_migrated_db(cfg, _DOWN_REVISION)
    ids = _seed_legacy_graph(sync_url)
    before = _legacy_view(sync_url)

    command.upgrade(cfg, _REVISION)

    assert _legacy_view(sync_url) == before, "legacy columns must not change"
    scopes = {
        row.id: (row.source_scope, row.source_scope_provenance)
        for row in _fetch(
            sync_url, "SELECT id, source_scope, source_scope_provenance FROM graph_node"
        )
    }
    assert scopes[ids["target_node"]] == ("global", "migration:0108")
    # target_id is set on the namespace row, but it only names the last
    # target that saw it -- the migration must not turn it into a scope.
    assert scopes[ids["namespace_node"]] == (None, None)
    assert scopes[ids["vm_node"]] == (None, None)


def test_upgrade_records_snapshot_with_schema_version(
    alembic_cfg: tuple[Config, str],
    copy_migrated_db: Callable[[Config, str], None],
) -> None:
    """The snapshot holds one row per node, keyed by node id."""
    cfg, sync_url = alembic_cfg
    copy_migrated_db(cfg, _DOWN_REVISION)
    ids = _seed_legacy_graph(sync_url)
    command.upgrade(cfg, _REVISION)

    rows = {
        row.node_id: row
        for row in _fetch(
            sync_url,
            "SELECT node_id, tenant_id, kind, name, last_target_id, source, "
            "discovered_by, scope_status, schema_version, captured_at "
            "FROM graph_node_scope_snapshot",
        )
    }
    assert set(rows) == {ids["target_node"], ids["namespace_node"], ids["vm_node"]}
    assert rows[ids["target_node"]].scope_status == "global"
    namespace = rows[ids["namespace_node"]]
    assert namespace.scope_status == "unresolved"
    assert namespace.last_target_id == ids["target"]
    assert (namespace.kind, namespace.name) == ("namespace", "default")
    assert rows[ids["vm_node"]].scope_status == "unresolved"
    assert rows[ids["vm_node"]].last_target_id is None
    assert {row.schema_version for row in rows.values()} == {1}
    assert all(row.captured_at is not None for row in rows.values())


def test_upgrade_keeps_old_tenant_kind_name_key(
    alembic_cfg: tuple[Config, str], copy_migrated_db: Callable[[Config, str], None]
) -> None:
    """Scoped duplicates stay impossible while the old key exists."""
    cfg, sync_url = alembic_cfg
    copy_migrated_db(cfg, _DOWN_REVISION)
    ids = _seed_legacy_graph(sync_url)
    command.upgrade(cfg, _REVISION)

    eng = sa_create_engine(sync_url)
    try:
        with eng.connect() as conn:
            indexes = {ix["name"]: ix for ix in inspect(conn).get_indexes("graph_node")}
    finally:
        eng.dispose()
    assert indexes["graph_node_tenant_kind_name_idx"]["unique"]
    assert indexes["graph_node_tenant_kind_name_idx"]["column_names"] == [
        "tenant_id",
        "kind",
        "name",
    ]
    assert not indexes["graph_node_tenant_scope_kind_name_idx"]["unique"]
    assert indexes["graph_node_tenant_scope_kind_name_idx"]["column_names"] == [
        "tenant_id",
        "source_scope",
        "kind",
        "name",
    ]

    with pytest.raises(IntegrityError):
        _insert_scoped_node(sync_url, ids["tenant"], "default", f"target:{_UUID}")


@pytest.mark.parametrize(
    "scope",
    ["global", f"target:{_UUID}", f"legacy:{_UUID}", None],
)
def test_check_accepts_documented_scope_forms(
    alembic_cfg: tuple[Config, str],
    scope: str | None,
    copy_migrated_db: Callable[[Config, str], None],
) -> None:
    cfg, sync_url = alembic_cfg
    copy_migrated_db(cfg, _DOWN_REVISION)
    ids = _seed_legacy_graph(sync_url)
    command.upgrade(cfg, _REVISION)

    _insert_scoped_node(sync_url, ids["tenant"], "fresh-name", scope)


@pytest.mark.parametrize(
    "scope",
    [
        "",
        "Global",
        "tenant",
        f"target:{_UUID.upper()}",
        f"TARGET:{_UUID}",
        f"cluster:{_UUID}",
        f"target:{_UUID[:-1]}",
        f"target:{_UUID}0",
        f"target:{_UUID.replace('-', '')}",
        "target:" + "-" * 36,
        f"legacy:{_UUID[:-1]}g",
        f"legacy:{{{_UUID}}}",
        "legacy:",
    ],
)
def test_check_rejects_malformed_scope_forms(
    alembic_cfg: tuple[Config, str],
    scope: str,
    copy_migrated_db: Callable[[Config, str], None],
) -> None:
    cfg, sync_url = alembic_cfg
    copy_migrated_db(cfg, _DOWN_REVISION)
    ids = _seed_legacy_graph(sync_url)
    command.upgrade(cfg, _REVISION)

    with pytest.raises(IntegrityError):
        _insert_scoped_node(sync_url, ids["tenant"], "fresh-name", scope)


def test_downgrade_keeps_rows_and_round_trips(
    alembic_cfg: tuple[Config, str], copy_migrated_db: Callable[[Config, str], None]
) -> None:
    """``0108 -> 0107 -> 0108`` keeps every row and the snapshot; replays the backfill."""
    cfg, sync_url = alembic_cfg
    copy_migrated_db(cfg, _DOWN_REVISION)
    ids = _seed_legacy_graph(sync_url)
    before = _legacy_view(sync_url)
    command.upgrade(cfg, _REVISION)
    snapshot = _snapshot_view(sync_url)

    command.downgrade(cfg, _DOWN_REVISION)

    eng = sa_create_engine(sync_url)
    try:
        with eng.connect() as conn:
            inspector = inspect(conn)
            columns = {c["name"] for c in inspector.get_columns("graph_node")}
            tables = set(inspector.get_table_names())
            index_names = {ix["name"] for ix in inspector.get_indexes("graph_node")}
    finally:
        eng.dispose()
    assert "source_scope" not in columns
    assert "source_scope_provenance" not in columns
    assert "graph_node_scope_snapshot" in tables, "the repair input must survive"
    assert "graph_node_tenant_scope_kind_name_idx" not in index_names
    assert "graph_node_tenant_kind_name_idx" in index_names
    assert _legacy_view(sync_url) == before
    assert _snapshot_view(sync_url) == snapshot

    # A node written while the schema is back at 0107 has no snapshot
    # row yet; the re-upgrade adds one without touching the others.
    _execute(
        sync_url,
        "INSERT INTO graph_node (id, tenant_id, kind, name, properties, discovered_by, "
        "first_seen) VALUES (:id, :tenant_id, 'vm', 'vm-2', '{}', 'test', :now)",
        {"id": str(uuid.uuid4()), "tenant_id": ids["tenant"], "now": datetime.now(UTC).isoformat()},
    )

    command.upgrade(cfg, _REVISION)
    rows = _fetch(
        sync_url, "SELECT id, source_scope FROM graph_node WHERE source_scope IS NOT NULL"
    )
    assert [(row.id, row.source_scope) for row in rows] == [(ids["target_node"], "global")]
    after = _snapshot_view(sync_url)
    assert len(after) == 4
    assert [row for row in after if row[3] != "vm-2"] == snapshot


def test_downgrade_keeps_the_snapshot_when_the_target_moved(
    alembic_cfg: tuple[Config, str],
    copy_migrated_db: Callable[[Config, str], None],
) -> None:
    """A refresh that moves ``target_id`` after the upgrade must not rewrite the input.

    Without the kept table, a downgrade followed by a re-upgrade would
    snapshot the *new* target and lose the one the row had at the cut.
    """
    cfg, sync_url = alembic_cfg
    copy_migrated_db(cfg, _DOWN_REVISION)
    ids = _seed_legacy_graph(sync_url)
    command.upgrade(cfg, _REVISION)
    other_target = _insert_target(sync_url, ids["tenant"], "cluster-b")
    _execute(
        sync_url,
        "UPDATE graph_node SET target_id = :target_id WHERE id = :id",
        {"target_id": other_target, "id": ids["namespace_node"]},
    )

    def _snapshot_row() -> tuple[Any, ...]:
        rows = _fetch(
            sync_url,
            "SELECT last_target_id, scope_status FROM graph_node_scope_snapshot "
            "WHERE node_id = :id",
            {"id": ids["namespace_node"]},
        )
        return tuple(rows[0])

    assert _snapshot_row() == (ids["target"], "unresolved")

    command.downgrade(cfg, _DOWN_REVISION)
    assert _snapshot_row() == (ids["target"], "unresolved")

    command.upgrade(cfg, _REVISION)
    assert _snapshot_row() == (ids["target"], "unresolved")
    live = _fetch(
        sync_url, "SELECT target_id FROM graph_node WHERE id = :id", {"id": ids["namespace_node"]}
    )
    assert live[0].target_id == other_target
    assert len(_fetch(sync_url, "SELECT node_id FROM graph_node_scope_snapshot")) == 3


@pytest.mark.parametrize(
    ("scope", "provenance"),
    [
        (f"target:{_UUID}", "repair"),
        (f"legacy:{_UUID}", None),
        ("global", "rediscovery"),
    ],
)
def test_downgrade_refuses_scope_values_it_did_not_write(
    alembic_cfg: tuple[Config, str],
    scope: str,
    provenance: str | None,
    copy_migrated_db: Callable[[Config, str], None],
) -> None:
    """Data the old schema cannot hold blocks the downgrade; roll forward instead."""
    cfg, sync_url = alembic_cfg
    copy_migrated_db(cfg, _DOWN_REVISION)
    ids = _seed_legacy_graph(sync_url)
    command.upgrade(cfg, _REVISION)
    _execute(
        sync_url,
        "UPDATE graph_node SET source_scope = :scope, source_scope_provenance = :prov "
        "WHERE id = :id",
        {"scope": scope, "prov": provenance, "id": ids["namespace_node"]},
    )

    with pytest.raises(RuntimeError, match="Roll forward"):
        command.downgrade(cfg, _DOWN_REVISION)

    columns = _fetch(
        sync_url,
        "SELECT source_scope FROM graph_node WHERE id = :id",
        {"id": ids["namespace_node"]},
    )
    assert columns[0].source_scope == scope
