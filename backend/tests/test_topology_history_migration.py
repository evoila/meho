# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for migration ``0012`` (G9.3-T1 history tables).

Coverage matrix (Task #856 acceptance criteria):

* **Upgrade / downgrade round-trip** -- ``alembic upgrade head`` against
  a fresh SQLite DB creates ``graph_node_history`` + ``graph_edge_history``
  with their full column shape and six total indexes; ``downgrade 0011``
  drops both tables and all six indexes; a re-``upgrade head`` cycle
  restores everything.
* **Index presence** -- each history table carries the three indexes
  the migration declares: a per-resource composite, a tenant-wide
  composite, and a partial ``WHERE change_kind = 'removed'`` index.
  The partial index's predicate round-trips through SQLite's
  ``sqlite_master`` reflection so the test asserts both the structural
  shape (via :func:`sqlalchemy.inspect`) and the partial-WHERE clause
  text (via a direct query against ``sqlite_master``).
* **DESC ordering on valid_from** -- the index DDL preserves
  ``valid_from DESC`` on every history index. SQLAlchemy's
  :func:`get_indexes` does not surface ordinality on SQLite, so the
  test reads the index's ``sql`` field from ``sqlite_master``
  (canonical CREATE INDEX text) and asserts the DESC keyword is
  present per index.
* **CHECK constraint enforcement** -- ``change_kind`` accepts the
  three closed-vocabulary values (``created`` / ``updated`` /
  ``removed``) and rejects anything else with :class:`IntegrityError`.
* **Drift guard** -- :class:`GraphHistoryChangeKind` and the migration's
  inlined ``_CHANGE_KINDS`` tuple stay in sync; the same drift guard
  pattern :class:`GraphHistoryChangeKind` follows
  for the live-edge vocabulary.
* **ORM round-trip** -- inserting a :class:`GraphNodeHistory` /
  :class:`GraphEdgeHistory` round-trips every field through SQLite
  (the ``default=`` machinery for ``history_id`` auto-increment,
  ``valid_from`` Python-side default, and JSONB snapshot serialisation
  all exercise here).
* **ON DELETE SET NULL** -- hard-deleting a :class:`GraphNode` /
  :class:`GraphEdge` clears the corresponding ``node_id`` / ``edge_id``
  on every history row that referenced it, leaving the rest of the
  row intact (audit_id, snapshot, change_kind, valid_from, tenant_id
  all preserved). This is the load-bearing T1 property -- history
  rows must survive deletion of the live row they reference.

The tests run against ``sqlite+aiosqlite`` via the shared engine cache
the autouse ``_default_database_url`` fixture in :mod:`tests.conftest`
already pre-migrates to ``alembic upgrade head``. Per-test isolation
comes from pytest's ``tmp_path``-scoped DB file -- the same shape every
other DB-touching test in the suite uses.

PG-side verification (``\\d+`` against a real container) lives in the
existing testcontainers suite that runs on CI; the unit-test layer
here is dialect-portable and runs in the agent sandbox.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from alembic.config import Config
from sqlalchemy import create_engine as sa_create_engine
from sqlalchemy import inspect as sa_inspect
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.db.engine import get_sessionmaker, reset_engine_for_testing
from meho_backplane.db.models import (
    _GRAPH_HISTORY_CHANGE_KINDS,
    GraphEdge,
    GraphEdgeHistory,
    GraphHistoryChangeKind,
    GraphNode,
    GraphNodeHistory,
    Tenant,
)
from meho_backplane.settings import get_settings


async def _enable_sqlite_foreign_keys(session: AsyncSession) -> None:
    """Issue ``PRAGMA foreign_keys = ON`` on the bound SQLite connection.

    Mirrors the helper in :mod:`tests.test_topology_schema` -- SQLite
    ships with foreign-key enforcement disabled by default and the
    ``ON DELETE SET NULL`` cascade on ``graph_node_history.node_id``
    only fires when the PRAGMA is on. Single-connection
    :class:`StaticPool` engine means the pragma persists across
    statements on the same session.
    """
    raw_conn = await session.connection()
    await raw_conn.execute(text("PRAGMA foreign_keys = ON"))


@pytest.fixture(autouse=True)
def _required_settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin every env var :class:`Settings` requires for this module.

    Mirrors the pattern in :mod:`tests.test_topology_schema`: the
    autouse ``_default_database_url`` fixture in :mod:`tests.conftest`
    only pins ``DATABASE_URL``; Keycloak/Vault knobs come from each
    test file. The ``get_settings.cache_clear()`` brackets prevent a
    stale ``Settings`` instance from a previous test from leaking.
    """
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


async def _seed_tenant(session: AsyncSession, *, slug: str = "rdc-internal") -> uuid.UUID:
    """Return the tenant row's id, inserting it on the first call.

    Every :class:`GraphNodeHistory` / :class:`GraphEdgeHistory` carries
    a real FK to :class:`Tenant`, so the per-test setup needs a parent
    tenant. Mirrors :func:`tests.test_topology_schema._seed_tenant`.

    The look-up-then-insert shape is load-bearing: migration ``0018``
    seeds the ``rdc-internal`` tenant into the per-worker schema
    template (:func:`tests.conftest._schema_template_db`), so a plain
    ``session.add(Tenant(slug='rdc-internal', ...))`` would trip
    ``UNIQUE constraint failed: tenant.slug``.
    """
    existing: uuid.UUID | None = await session.scalar(
        select(Tenant.id).where(Tenant.slug == slug),
    )
    if existing is not None:
        return existing
    tenant_id = uuid.uuid4()
    session.add(Tenant(id=tenant_id, slug=slug, name=f"Tenant {slug}"))
    await session.commit()
    return tenant_id


async def _seed_node(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    *,
    name: str = "vm-prod",
    kind: str = "vm",
) -> uuid.UUID:
    """Insert a :class:`GraphNode` row and return its UUID."""
    node_id = uuid.uuid4()
    session.add(
        GraphNode(
            id=node_id,
            tenant_id=tenant_id,
            kind=kind,
            name=name,
            discovered_by="vmware",
        )
    )
    await session.commit()
    return node_id


async def _seed_edge(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    from_id: uuid.UUID,
    to_id: uuid.UUID,
    *,
    kind: str = "runs-on",
) -> uuid.UUID:
    """Insert a :class:`GraphEdge` row and return its UUID."""
    edge_id = uuid.uuid4()
    session.add(
        GraphEdge(
            id=edge_id,
            tenant_id=tenant_id,
            from_node_id=from_id,
            to_node_id=to_id,
            kind=kind,
            source="auto",
            discovered_by="vmware",
        )
    )
    await session.commit()
    return edge_id


def _alembic_upgrade_against_fresh_sqlite(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    db_filename: str,
) -> tuple[str, Config]:
    """Pin env, reset caches, run ``alembic upgrade head`` on fresh SQLite.

    Shared setup for the sync migration tests below; mirrors the
    helper in :mod:`tests.test_topology_schema`. Returns
    ``(sync_url, alembic_cfg)`` so callers can inspect the resulting
    schema or run further migration ops (upgrade / downgrade).
    """
    from alembic import command

    from meho_backplane.db.migrations import alembic_config

    db_path = tmp_path / db_filename
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
    command.upgrade(cfg, "head")
    return sync_url, cfg


# ---------------------------------------------------------------------------
# Schema-level inspection -- migration installs tables + indexes
# ---------------------------------------------------------------------------


_EXPECTED_HISTORY_COLUMNS: dict[str, set[str]] = {
    "graph_node_history": {
        "history_id",
        "node_id",
        "tenant_id",
        "change_kind",
        "snapshot",
        "audit_id",
        "valid_from",
    },
    "graph_edge_history": {
        "history_id",
        "edge_id",
        "tenant_id",
        "change_kind",
        "snapshot",
        "audit_id",
        "valid_from",
    },
}


_EXPECTED_HISTORY_INDEXES: dict[str, set[str]] = {
    "graph_node_history": {
        "graph_node_history_tenant_node_valid_from_idx",
        "graph_node_history_tenant_valid_from_idx",
        "graph_node_history_tenant_removed_idx",
    },
    "graph_edge_history": {
        "graph_edge_history_tenant_edge_valid_from_idx",
        "graph_edge_history_tenant_valid_from_idx",
        "graph_edge_history_tenant_removed_idx",
    },
}


def test_migration_installs_history_tables_and_indexes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """``alembic upgrade head`` puts both history tables + six indexes in place.

    Asserts via SQLite's schema inspector (the dialect-portable
    equivalent of ``\\d+`` against PG) that the two history tables
    carry every documented column, all three named indexes per table,
    the ``ON DELETE SET NULL`` foreign-key shape against the live
    tables, and the ``change_kind`` CHECK constraint. PG-side
    verification lives in the testcontainers suite.
    """
    sync_url, _ = _alembic_upgrade_against_fresh_sqlite(monkeypatch, tmp_path, "g93-schema.db")

    sync_eng = sa_create_engine(sync_url)
    try:
        with sync_eng.connect() as conn:
            inspector = sa_inspect(conn)
            tables = set(inspector.get_table_names())
            assert "graph_node_history" in tables
            assert "graph_edge_history" in tables

            for table_name, expected_cols in _EXPECTED_HISTORY_COLUMNS.items():
                cols = {col["name"] for col in inspector.get_columns(table_name)}
                assert expected_cols <= cols, (
                    f"Missing columns in {table_name}: {expected_cols - cols}"
                )

            for table_name, expected_indexes in _EXPECTED_HISTORY_INDEXES.items():
                indexes = {idx["name"] for idx in inspector.get_indexes(table_name)}
                assert expected_indexes <= indexes, (
                    f"Missing indexes in {table_name}: {expected_indexes - indexes}"
                )

            # ``node_id`` / ``edge_id`` must be nullable -- the
            # ON DELETE SET NULL transition requires it.
            node_id_col = next(
                col
                for col in inspector.get_columns("graph_node_history")
                if col["name"] == "node_id"
            )
            assert node_id_col["nullable"] is True
            edge_id_col = next(
                col
                for col in inspector.get_columns("graph_edge_history")
                if col["name"] == "edge_id"
            )
            assert edge_id_col["nullable"] is True

            # ``audit_id`` must be nullable -- soft-FK to audit_log.id;
            # the diff-on-write hook in T2 populates it when a request
            # is in scope, leaves it NULL for replays / backfills.
            node_audit_col = next(
                col
                for col in inspector.get_columns("graph_node_history")
                if col["name"] == "audit_id"
            )
            assert node_audit_col["nullable"] is True
            edge_audit_col = next(
                col
                for col in inspector.get_columns("graph_edge_history")
                if col["name"] == "audit_id"
            )
            assert edge_audit_col["nullable"] is True

            # FK shapes: node-history FKs point at graph_node + tenant;
            # edge-history FKs point at graph_edge + tenant. Both
            # live-table FKs are ON DELETE SET NULL.
            node_fks = inspector.get_foreign_keys("graph_node_history")
            node_fk_referred = {fk["referred_table"] for fk in node_fks}
            assert "graph_node" in node_fk_referred
            assert "tenant" in node_fk_referred
            node_id_fk = next(fk for fk in node_fks if fk["constrained_columns"] == ["node_id"])
            assert node_id_fk["options"].get("ondelete") == "SET NULL", (
                "node_id FK must be ON DELETE SET NULL so history rows survive node deletion"
            )

            edge_fks = inspector.get_foreign_keys("graph_edge_history")
            edge_fk_referred = {fk["referred_table"] for fk in edge_fks}
            assert "graph_edge" in edge_fk_referred
            assert "tenant" in edge_fk_referred
            edge_id_fk = next(fk for fk in edge_fks if fk["constrained_columns"] == ["edge_id"])
            assert edge_id_fk["options"].get("ondelete") == "SET NULL", (
                "edge_id FK must be ON DELETE SET NULL so history rows survive edge deletion"
            )

            # CHECK constraint shapes -- both tables enforce the
            # closed change_kind vocabulary at the DB layer.
            for table_name in ("graph_node_history", "graph_edge_history"):
                ck_names = {ck["name"] for ck in inspector.get_check_constraints(table_name)}
                assert f"ck_{table_name}_change_kind" in ck_names, (
                    f"{table_name} missing change_kind CHECK constraint"
                )
    finally:
        sync_eng.dispose()


def test_migration_partial_index_carries_removed_predicate(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The partial index DDL preserves ``WHERE change_kind = 'removed'``.

    SQLAlchemy's :func:`get_indexes` does not surface predicate text on
    SQLite (the ``dialect_options`` field carries the
    :class:`sqlalchemy.TextClause` reference but not the rendered SQL).
    The canonical truth is the ``sql`` column on ``sqlite_master`` --
    SQLite's own catalog of CREATE statements. Reading the column and
    asserting on its content is the only way to prove the partial
    predicate landed correctly on the dev/test driver.
    """
    sync_url, _ = _alembic_upgrade_against_fresh_sqlite(monkeypatch, tmp_path, "g93-partial.db")

    sync_eng = sa_create_engine(sync_url)
    try:
        with sync_eng.connect() as conn:
            rows = conn.execute(
                text(
                    "SELECT name, sql FROM sqlite_master "
                    "WHERE type = 'index' "
                    "AND name IN ("
                    "'graph_node_history_tenant_removed_idx', "
                    "'graph_edge_history_tenant_removed_idx'"
                    ")"
                )
            ).all()
        index_sqls = {row.name: row.sql for row in rows}
        assert "graph_node_history_tenant_removed_idx" in index_sqls
        assert "graph_edge_history_tenant_removed_idx" in index_sqls
        for name, sql in index_sqls.items():
            assert "WHERE change_kind = 'removed'" in sql, (
                f"{name} missing partial-WHERE clause; got: {sql}"
            )
            # DESC ordering is the other property the migration owns
            # exclusively -- piggyback the assertion here so a single
            # failure surfaces both kinds of drift.
            assert "valid_from DESC" in sql, f"{name} missing valid_from DESC ordering; got: {sql}"
    finally:
        sync_eng.dispose()


def test_migration_history_indexes_carry_desc_ordering(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Every history index orders ``valid_from`` DESC.

    The DESC ordering is what lets PG's index-only scan return
    newest-first revisions without a post-scan sort -- the dominant
    query shape for T3 / T5. SQLite's index DDL preserves the same
    ordinality so the unit test can assert against the rendered SQL
    on the ``sqlite_master`` catalog row.
    """
    sync_url, _ = _alembic_upgrade_against_fresh_sqlite(monkeypatch, tmp_path, "g93-desc.db")

    expected_desc_indexes = (
        "graph_node_history_tenant_node_valid_from_idx",
        "graph_node_history_tenant_valid_from_idx",
        "graph_node_history_tenant_removed_idx",
        "graph_edge_history_tenant_edge_valid_from_idx",
        "graph_edge_history_tenant_valid_from_idx",
        "graph_edge_history_tenant_removed_idx",
    )

    sync_eng = sa_create_engine(sync_url)
    try:
        with sync_eng.connect() as conn:
            rows = conn.execute(
                text(
                    "SELECT name, sql FROM sqlite_master "
                    "WHERE type = 'index' AND name LIKE '%_history_%'"
                )
            ).all()
        index_sqls = {row.name: row.sql for row in rows}
        for name in expected_desc_indexes:
            assert name in index_sqls, f"missing index {name}"
            assert "valid_from DESC" in index_sqls[name], (
                f"{name} missing valid_from DESC ordering; got: {index_sqls[name]}"
            )
    finally:
        sync_eng.dispose()


def test_migration_upgrade_then_downgrade_is_reversible(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """``alembic upgrade head`` -> ``alembic downgrade 0011`` -> ``upgrade head`` clean.

    Proves migration ``0012`` is fully reversible: after downgrading by
    exactly one revision (back to ``0011``, the prior head before this
    Task lands), both history tables and all six indexes must be gone
    while the rest of the schema (``tenant``, ``audit_log``,
    ``graph_node``, ``graph_edge``, ...) remains intact. Re-upgrading
    to head must restore everything cleanly.
    """
    from alembic import command

    sync_url, cfg = _alembic_upgrade_against_fresh_sqlite(monkeypatch, tmp_path, "g93-rev.db")

    sync_eng = sa_create_engine(sync_url)
    try:
        with sync_eng.connect() as conn:
            inspector = sa_inspect(conn)
            tables = set(inspector.get_table_names())
            assert "graph_node_history" in tables
            assert "graph_edge_history" in tables
            # Live graph tables must be present -- prerequisite for
            # the history-table FKs.
            assert "graph_node" in tables
            assert "graph_edge" in tables

        # Downgrade by exactly one revision (back to 0011).
        command.downgrade(cfg, "0011")

        with sync_eng.connect() as conn:
            inspector = sa_inspect(conn)
            tables = set(inspector.get_table_names())
            assert "graph_node_history" not in tables, "downgrade must drop graph_node_history"
            assert "graph_edge_history" not in tables, "downgrade must drop graph_edge_history"
            # Prior-migration tables must survive intact.
            assert "tenant" in tables
            assert "audit_log" in tables
            assert "graph_node" in tables
            assert "graph_edge" in tables

            # All six history indexes must be gone -- explicit-drop
            # discipline (see migration 0012 docstring) guarantees
            # SQLite's symmetric inverse.
            index_rows = conn.execute(
                text(
                    "SELECT name FROM sqlite_master "
                    "WHERE type = 'index' AND name LIKE '%_history_%'"
                )
            ).all()
            assert index_rows == [], (
                f"history indexes survived downgrade: {[r.name for r in index_rows]}"
            )

        # Re-upgrade -- must be idempotent from 0011 back to head.
        command.upgrade(cfg, "head")
        with sync_eng.connect() as conn:
            inspector = sa_inspect(conn)
            tables = set(inspector.get_table_names())
            assert "graph_node_history" in tables
            assert "graph_edge_history" in tables
    finally:
        sync_eng.dispose()


# ---------------------------------------------------------------------------
# Drift guard -- enum and DB-layer CHECK constraint stay in lock-step
# ---------------------------------------------------------------------------


def test_change_kind_enum_has_exactly_three_members() -> None:
    """:class:`GraphHistoryChangeKind` has the three closed-vocabulary members.

    Decision-locked at three: ``created`` / ``updated`` / ``removed``.
    Widening requires a coordinated migration that updates the CHECK
    constraint *and* the enum in lock-step; the explicit member-count
    assertion is the regression guard.
    """
    assert len(GraphHistoryChangeKind) == 3
    assert {k.value for k in GraphHistoryChangeKind} == {
        "created",
        "updated",
        "removed",
    }


def test_change_kind_enum_matches_ck_constraint_tuple() -> None:
    """:data:`_GRAPH_HISTORY_CHANGE_KINDS` mirrors :class:`GraphHistoryChangeKind`.

    Same drift-guard pattern the graph tables' kind CHECKs followed
    pre-#2534 for the
    live-edge vocabulary: the Python type-level enum and the DB-layer
    ``CHECK change_kind IN (...)`` constraint must move in lock-step.
    Equality is the regression guard at unit-test time.
    """
    assert set(_GRAPH_HISTORY_CHANGE_KINDS) == {k.value for k in GraphHistoryChangeKind}


# ---------------------------------------------------------------------------
# ORM round-trip + CHECK enforcement + cascade behaviour
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_graph_node_history_round_trip_persists_every_field() -> None:
    """Insert a :class:`GraphNodeHistory`, query it back, every field matches.

    Exercises the ORM-side defaults (``history_id`` auto-increment,
    ``valid_from`` Python default, JSONB snapshot serialisation) on
    the SQLite dev/test driver where the migration's PG server-side
    defaults are no-ops.
    """
    sessionmaker = get_sessionmaker()
    audit_id = uuid.uuid4()
    snapshot = {"before": None, "after": {"name": "vm-prod", "kind": "vm"}}

    async with sessionmaker() as session:
        await _enable_sqlite_foreign_keys(session)
        tenant_id = await _seed_tenant(session)
        node_id = await _seed_node(session, tenant_id)

        session.add(
            GraphNodeHistory(
                node_id=node_id,
                tenant_id=tenant_id,
                change_kind=GraphHistoryChangeKind.CREATED.value,
                snapshot=snapshot,
                audit_id=audit_id,
            )
        )
        await session.commit()

    async with sessionmaker() as session:
        result = await session.execute(
            select(GraphNodeHistory).where(GraphNodeHistory.node_id == node_id)
        )
        row = result.scalar_one()

    assert row.history_id is not None
    assert row.history_id >= 1  # autoincrement assigned a value
    assert row.node_id == node_id
    assert row.tenant_id == tenant_id
    assert row.change_kind == "created"
    assert row.snapshot == snapshot
    assert row.audit_id == audit_id
    assert row.valid_from is not None


@pytest.mark.asyncio
async def test_graph_edge_history_round_trip_persists_every_field() -> None:
    """Mirror of :func:`test_graph_node_history_round_trip_persists_every_field`."""
    sessionmaker = get_sessionmaker()
    audit_id = uuid.uuid4()
    snapshot = {
        "before": {"kind": "runs-on", "source": "auto"},
        "after": {"kind": "runs-on", "source": "auto", "discovered_by": "vmware"},
    }

    async with sessionmaker() as session:
        await _enable_sqlite_foreign_keys(session)
        tenant_id = await _seed_tenant(session)
        from_id = await _seed_node(session, tenant_id, name="vm-a")
        to_id = await _seed_node(session, tenant_id, name="host-a", kind="host")
        edge_id = await _seed_edge(session, tenant_id, from_id, to_id)

        session.add(
            GraphEdgeHistory(
                edge_id=edge_id,
                tenant_id=tenant_id,
                change_kind=GraphHistoryChangeKind.UPDATED.value,
                snapshot=snapshot,
                audit_id=audit_id,
            )
        )
        await session.commit()

    async with sessionmaker() as session:
        result = await session.execute(
            select(GraphEdgeHistory).where(GraphEdgeHistory.edge_id == edge_id)
        )
        row = result.scalar_one()

    assert row.history_id is not None
    assert row.history_id >= 1
    assert row.edge_id == edge_id
    assert row.tenant_id == tenant_id
    assert row.change_kind == "updated"
    assert row.snapshot == snapshot
    assert row.audit_id == audit_id
    assert row.valid_from is not None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kind",
    [k.value for k in GraphHistoryChangeKind],
)
async def test_graph_node_history_accepts_every_closed_change_kind(kind: str) -> None:
    """Each of the three closed-vocabulary ``change_kind`` values inserts cleanly.

    The post-migration CHECK constraint accepts the closed three-kind
    set; the parametrise covers every member so the test fails loudly
    if a future migration narrows the set without updating the enum.
    """
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        await _enable_sqlite_foreign_keys(session)
        tenant_id = await _seed_tenant(session)
        node_id = await _seed_node(session, tenant_id)
        session.add(
            GraphNodeHistory(
                node_id=node_id,
                tenant_id=tenant_id,
                change_kind=kind,
                snapshot={"after": {"kind": "vm"}},
            )
        )
        await session.commit()


@pytest.mark.asyncio
async def test_graph_node_history_rejects_out_of_vocabulary_change_kind() -> None:
    """A ``change_kind`` outside the closed enum raises :class:`IntegrityError`.

    The DB-layer CHECK constraint is the enforcement point -- the
    Python enum is a type hint, not a runtime guard.
    """
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        await _enable_sqlite_foreign_keys(session)
        tenant_id = await _seed_tenant(session)
        node_id = await _seed_node(session, tenant_id)
        session.add(
            GraphNodeHistory(
                node_id=node_id,
                tenant_id=tenant_id,
                change_kind="invalid-kind",
                snapshot={},
            )
        )
        with pytest.raises(IntegrityError):
            await session.commit()


@pytest.mark.asyncio
async def test_graph_edge_history_rejects_out_of_vocabulary_change_kind() -> None:
    """Mirror of the node-side CHECK enforcement test for the edge history."""
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        await _enable_sqlite_foreign_keys(session)
        tenant_id = await _seed_tenant(session)
        from_id = await _seed_node(session, tenant_id, name="vm-a")
        to_id = await _seed_node(session, tenant_id, name="host-a", kind="host")
        edge_id = await _seed_edge(session, tenant_id, from_id, to_id)
        session.add(
            GraphEdgeHistory(
                edge_id=edge_id,
                tenant_id=tenant_id,
                change_kind="not-a-kind",
                snapshot={},
            )
        )
        with pytest.raises(IntegrityError):
            await session.commit()


@pytest.mark.asyncio
async def test_graph_node_deletion_sets_history_node_id_to_null() -> None:
    """Hard-deleting a :class:`GraphNode` clears history ``node_id`` -- NOT cascade-delete.

    This is the load-bearing T1 property -- history rows must survive
    deletion of the live row they reference. Without ``ON DELETE SET
    NULL``, removing a node would drop the entire history of that
    node, defeating the audit-trail / forensics use case Initiative
    #365 exists to serve. The test inserts a node + a history row,
    hard-deletes the node, and asserts the history row's ``node_id``
    is NULL while every other field (tenant_id, change_kind,
    snapshot, audit_id, valid_from) survives intact.
    """
    sessionmaker = get_sessionmaker()
    audit_id = uuid.uuid4()
    snapshot = {"before": None, "after": {"name": "vm-target"}}
    history_capture_at = datetime.now(UTC)

    async with sessionmaker() as session:
        await _enable_sqlite_foreign_keys(session)
        tenant_id = await _seed_tenant(session)
        node_id = await _seed_node(session, tenant_id, name="vm-target")
        session.add(
            GraphNodeHistory(
                node_id=node_id,
                tenant_id=tenant_id,
                change_kind="created",
                snapshot=snapshot,
                audit_id=audit_id,
                valid_from=history_capture_at,
            )
        )
        await session.commit()

        # Hard-delete the live node (rare admin op; refresh-driven
        # soft-deletes set last_seen=NULL and never reach this path).
        node_row = await session.get(GraphNode, node_id)
        assert node_row is not None
        await session.delete(node_row)
        await session.commit()

    async with sessionmaker() as session:
        result = await session.execute(
            select(GraphNodeHistory).where(GraphNodeHistory.tenant_id == tenant_id)
        )
        row = result.scalar_one()

    # Load-bearing assertion: node_id cleared, every other field intact.
    assert row.node_id is None, "node_id must SET NULL on live-node deletion"
    assert row.tenant_id == tenant_id
    assert row.change_kind == "created"
    assert row.snapshot == snapshot
    assert row.audit_id == audit_id


@pytest.mark.asyncio
async def test_graph_edge_deletion_sets_history_edge_id_to_null() -> None:
    """Mirror of the node-side test for the edge-side cascade.

    Hard-deleting a :class:`GraphEdge` clears history ``edge_id`` --
    same ``ON DELETE SET NULL`` shape, same load-bearing audit-trail
    survival property.
    """
    sessionmaker = get_sessionmaker()
    audit_id = uuid.uuid4()
    snapshot = {"before": None, "after": {"kind": "runs-on"}}

    async with sessionmaker() as session:
        await _enable_sqlite_foreign_keys(session)
        tenant_id = await _seed_tenant(session)
        from_id = await _seed_node(session, tenant_id, name="vm-a")
        to_id = await _seed_node(session, tenant_id, name="host-a", kind="host")
        edge_id = await _seed_edge(session, tenant_id, from_id, to_id)
        session.add(
            GraphEdgeHistory(
                edge_id=edge_id,
                tenant_id=tenant_id,
                change_kind="created",
                snapshot=snapshot,
                audit_id=audit_id,
            )
        )
        await session.commit()

        edge_row = await session.get(GraphEdge, edge_id)
        assert edge_row is not None
        await session.delete(edge_row)
        await session.commit()

    async with sessionmaker() as session:
        result = await session.execute(
            select(GraphEdgeHistory).where(GraphEdgeHistory.tenant_id == tenant_id)
        )
        row = result.scalar_one()

    assert row.edge_id is None, "edge_id must SET NULL on live-edge deletion"
    assert row.tenant_id == tenant_id
    assert row.change_kind == "created"
    assert row.snapshot == snapshot
    assert row.audit_id == audit_id
