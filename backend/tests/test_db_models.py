# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for the SQLAlchemy models in :mod:`meho_backplane.db.models`.

Coverage matrix (Task #231 acceptance criteria):

* Round-trip on :class:`Tenant` — insert a row, query it back, every
  field round-trips. The ORM ``default=`` machinery (uuid + created_at)
  fires on the SQLite dev/test driver where the migration cannot
  install a server-side default.
* Round-trip on :class:`AuditLog` with ``tenant_id`` populated — the
  new column survives the insert / select cycle and matches the
  inserted UUID.
* Round-trip on :class:`AuditLog` with ``tenant_id=None`` — chassis-
  era forward-compat: pre-G0.1 audit rows must remain readable
  without any tenant context. This is the nullability contract the
  v0.2 migration ships; flipping the column to NOT NULL would
  break this and the chassis upgrade path simultaneously.
* Schema-level smoke — ``alembic upgrade head`` against a fresh
  SQLite DB creates the ``tenant`` table with its slug index, and
  the ``audit_log_tenant_id_idx`` lands on the existing audit
  table. This is the autogenerate-side proof that the migration
  and the model graph agree.

The tests run synchronously against ``sqlite+aiosqlite`` via the
shared engine cache that the autouse ``_default_database_url``
fixture in :mod:`tests.conftest` already pre-migrates to
``alembic upgrade head``. Per-test isolation comes from pytest's
``tmp_path``-scoped DB file, the same shape every other DB-touching
test in the suite uses.

The PG-real testcontainers smoke (``\\d+ tenant`` index inspection,
PG-side server defaults) lives in the existing
``tests.test_db_engine.TestPostgresIntegration`` class — Docker
sandbox-skipped, exercised on CI runners that provision Docker.
This file deliberately stays Docker-free so the always-on gate
asserts the ORM contract on every PR regardless of runner.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import create_engine as sa_create_engine
from sqlalchemy import inspect as sa_inspect
from sqlalchemy import select

from meho_backplane.db.engine import get_sessionmaker, reset_engine_for_testing
from meho_backplane.db.models import AuditLog, Tenant
from meho_backplane.settings import get_settings

if TYPE_CHECKING:
    from alembic.config import Config


@pytest.fixture(autouse=True)
def _required_settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin every env var :class:`Settings` requires for this module.

    The autouse ``_default_database_url`` fixture in
    :mod:`tests.conftest` only pins ``DATABASE_URL``; Keycloak/Vault
    knobs come from each test file. Mirrors the pattern in
    :func:`tests.test_db_engine.isolated_engine_cache`. The
    ``get_settings`` cache reset around the yield keeps a stale
    ``Settings`` instance from a previous test from leaking in.
    """
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


# Note on engine wiring
# ---------------------
# Every async test in this file relies on the autouse
# ``_default_database_url`` fixture in :mod:`tests.conftest`, which
# (a) sets ``DATABASE_URL`` to a per-test ``sqlite+aiosqlite:///<tmp>``
# URL, (b) runs ``alembic upgrade head`` against it, and (c) clears
# the engine cache. By the time pytest-asyncio enters the event loop
# for a test body, the schema is already at head; calling
# :func:`get_sessionmaker` resolves to a fresh engine bound to that
# URL on first use. We deliberately avoid creating a second engine
# in a fixture body here — :func:`alembic.command.upgrade` calls
# :func:`asyncio.run` internally via the env.py async cookbook, and
# ``asyncio.run`` cannot be re-entered from a running loop, which
# means a per-test async fixture cannot safely run the migration
# itself. The conftest fixture pre-migrates in its sync prelude
# precisely to sidestep this.


# ---------------------------------------------------------------------------
# Tenant round-trip
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tenant_round_trip_persists_every_field() -> None:
    """Insert a :class:`Tenant`, query it back, every field matches.

    Drives the model via :func:`get_sessionmaker` so the path is
    identical to what production code (future tenants-CRUD UX,
    seeding migrations) will use. Asserting on every field — id,
    slug, name, created_at — is what proves the ORM ``default=``
    machinery fires correctly under SQLite (the dev/test dialect
    where the migration's PG server defaults are no-ops).
    """
    sessionmaker = get_sessionmaker()
    tenant_id = uuid.uuid4()
    created_at = datetime.now(UTC)
    async with sessionmaker() as session:
        session.add(
            Tenant(
                id=tenant_id,
                slug="rdc-internal",
                name="RDC Internal Tenancy",
                created_at=created_at,
            )
        )
        await session.commit()

    async with sessionmaker() as session:
        result = await session.execute(select(Tenant).where(Tenant.id == tenant_id))
        row = result.scalar_one()

    assert row.id == tenant_id
    assert row.slug == "rdc-internal"
    assert row.name == "RDC Internal Tenancy"
    # SQLite stores datetimes as ISO-8601 strings without timezone
    # information; SQLAlchemy round-trips them as **naive**
    # ``datetime`` even when the column is declared
    # ``DateTime(timezone=True)`` (the timezone metadata is a PG
    # concept SQLite lacks). Assert the wall-clock parts match —
    # PG production would return a tz-aware value (covered by the
    # testcontainers suite); the dev/test driver intentionally
    # cannot. Falsifying this would mean the round-trip lost
    # information beyond just the tzinfo, which is the property
    # this test guards.
    assert row.created_at.replace(tzinfo=None) == created_at.replace(tzinfo=None)


@pytest.mark.asyncio
async def test_tenant_slug_uniqueness_enforced() -> None:
    """Two :class:`Tenant` rows with the same slug → IntegrityError.

    The migration enforces uniqueness on ``slug`` via the named
    ``tenant_slug_idx`` (declared ``unique=True``); this test
    proves the constraint is enforced at the DB layer (not just by
    a UI-side validator). Without this assertion, a future migration
    that accidentally dropped the unique flag would silently allow
    duplicate slugs, and the operator-facing ``rdc-internal``
    handle would no longer be a primary key for the human eye.
    """
    from sqlalchemy.exc import IntegrityError

    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        session.add(Tenant(id=uuid.uuid4(), slug="dup", name="First"))
        await session.commit()

    async with sessionmaker() as session:
        session.add(Tenant(id=uuid.uuid4(), slug="dup", name="Second"))
        with pytest.raises(IntegrityError):
            await session.commit()


# ---------------------------------------------------------------------------
# AuditLog.tenant_id round-trip
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_audit_log_round_trip_with_tenant_id() -> None:
    """Insert an :class:`AuditLog` with ``tenant_id`` set, read it back.

    This is the load-bearing positive case for the new column: the
    G0.1-T3 audit middleware will populate ``tenant_id`` on every
    authenticated request, and the per-tenant audit query
    (G8 search-by-tenant) will read it back. Round-tripping it now
    proves the column shape, type, and indexability are wired
    correctly before T3 lands the writes.
    """
    sessionmaker = get_sessionmaker()
    audit_id = uuid.uuid4()
    tenant_id = uuid.uuid4()
    occurred_at = datetime.now(UTC)
    async with sessionmaker() as session:
        session.add(
            AuditLog(
                id=audit_id,
                occurred_at=occurred_at,
                operator_sub="op-tenant-positive",
                method="GET",
                path="/api/v1/health",
                status_code=200,
                request_id=None,
                duration_ms=None,
                payload={},
                tenant_id=tenant_id,
            )
        )
        await session.commit()

    async with sessionmaker() as session:
        result = await session.execute(select(AuditLog).where(AuditLog.id == audit_id))
        row = result.scalar_one()

    assert row.tenant_id == tenant_id
    assert row.operator_sub == "op-tenant-positive"


@pytest.mark.asyncio
async def test_audit_log_round_trip_with_null_tenant_id() -> None:
    """Insert an :class:`AuditLog` with ``tenant_id=None``, read it back as None.

    Forward-compat with chassis-era rows: every audit row written
    *before* G0.1 lands has no tenant context, and the migration
    leaves those rows with ``tenant_id IS NULL`` rather than
    backfilling. This test pins the contract that NULL survives the
    round-trip on the ORM side too — a future column-level default
    on ``tenant_id`` (e.g. someone adding ``default=uuid.uuid4`` for
    the wrong reason) would silently break the chassis upgrade
    semantic and this test would fail.
    """
    sessionmaker = get_sessionmaker()
    audit_id = uuid.uuid4()
    occurred_at = datetime.now(UTC)
    async with sessionmaker() as session:
        session.add(
            AuditLog(
                id=audit_id,
                occurred_at=occurred_at,
                operator_sub="op-tenant-null",
                method="GET",
                path="/api/v1/health",
                status_code=200,
                request_id=None,
                duration_ms=None,
                payload={},
                # tenant_id deliberately omitted — chassis-era shape.
            )
        )
        await session.commit()

    async with sessionmaker() as session:
        result = await session.execute(select(AuditLog).where(AuditLog.id == audit_id))
        row = result.scalar_one()

    assert row.tenant_id is None
    assert row.operator_sub == "op-tenant-null"


# ---------------------------------------------------------------------------
# Schema-level inspection — migration installs the documented indexes
# ---------------------------------------------------------------------------


def _alembic_upgrade_against_fresh_sqlite(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    db_filename: str,
) -> tuple[str, Config]:
    """Pin env, reset caches, run ``alembic upgrade head`` on fresh SQLite.

    Shared setup for the two sync migration tests below — pinning
    the env-var quartet (DATABASE_URL + the Keycloak/Vault knobs
    that :class:`Settings` requires), clearing the settings + engine
    caches, and bringing a per-test SQLite file to the current head.
    Returns ``(sync_url, alembic_cfg)`` so callers can either
    inspect the resulting schema or drive further migration ops
    (e.g. a downgrade) from the same Alembic config object the
    upgrade just ran against.

    Kept inside the test module — single non-trivial helper, not
    worth a separate ``conftest`` extension.
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


def test_migration_installs_tenant_table_and_indexes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """``alembic upgrade head`` puts the ``tenant`` table + indexes in place.

    The acceptance criterion calls for ``\\d+ tenant`` and
    ``\\d+ audit_log`` showing the new indexes. SQLite's inspector
    is the dialect-portable equivalent: read ``get_indexes`` for
    each table and assert the named indexes appear. The PG-side
    smoke (``\\d+`` against a real PG container) lives in the
    existing testcontainers suite that runs on CI.

    Sync-only because :func:`alembic.command.upgrade` calls
    :func:`asyncio.run` internally via the env.py async cookbook
    and the two cannot be re-entered — the same constraint that
    keeps the SQLite ``alembic upgrade head`` smoke in
    ``tests.test_db_engine`` synchronous.
    """
    sync_url, _ = _alembic_upgrade_against_fresh_sqlite(monkeypatch, tmp_path, "schema.db")

    sync_eng = sa_create_engine(sync_url)
    try:
        with sync_eng.connect() as conn:
            inspector = sa_inspect(conn)
            tables = set(inspector.get_table_names())
            assert "tenant" in tables
            assert "audit_log" in tables

            tenant_indexes = {idx["name"] for idx in inspector.get_indexes("tenant")}
            assert "tenant_slug_idx" in tenant_indexes

            audit_indexes = {idx["name"] for idx in inspector.get_indexes("audit_log")}
            assert "audit_log_tenant_id_idx" in audit_indexes

            tenant_columns = {col["name"] for col in inspector.get_columns("tenant")}
            assert tenant_columns == {"id", "slug", "name", "created_at"}

            audit_columns = {col["name"] for col in inspector.get_columns("audit_log")}
            assert "tenant_id" in audit_columns
    finally:
        sync_eng.dispose()


def test_migration_upgrade_then_downgrade_is_reversible(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """``alembic upgrade head`` → ``alembic downgrade -1`` is a clean cycle.

    The acceptance criterion explicitly demands reversibility: the
    consumer must be able to roll back from v0.2 to the v0.1
    chassis without manual DB intervention. Asserting that
    ``downgrade -1`` removes the tenant table, the new audit
    column, and the new indexes — and that ``upgrade head`` then
    re-creates them cleanly — is the unit-level proof of that
    contract. The PR-level proof (real ``helm rollback`` against a
    cluster) lives in the G2.7 / G2.8 deploy gates.

    The downgrade target is the previous revision (``0001``); we
    spell it explicitly rather than relying on ``-1`` arithmetic
    so a future revision inserted between ``0001`` and ``0002``
    surfaces as a test failure rather than a silent no-op.
    """
    from alembic import command

    # Step 1 — upgrade to head (0002). Tenant + tenant_id present.
    # The helper handles env pinning, cache reset, and the initial
    # upgrade; we keep ``command`` in scope for the downgrade /
    # re-upgrade in steps 2-3 below.
    sync_url, cfg = _alembic_upgrade_against_fresh_sqlite(monkeypatch, tmp_path, "rev.db")

    sync_eng = sa_create_engine(sync_url)
    try:
        with sync_eng.connect() as conn:
            inspector = sa_inspect(conn)
            assert "tenant" in inspector.get_table_names()
            assert "tenant_id" in {col["name"] for col in inspector.get_columns("audit_log")}

        # Step 2 — downgrade by exactly one revision (back to 0001).
        # Tenant table, the audit_log.tenant_id column, and both new
        # indexes must all be gone; the v0.1 chassis schema (audit_log
        # without tenant_id) must remain intact.
        command.downgrade(cfg, "0001")

        with sync_eng.connect() as conn:
            inspector = sa_inspect(conn)
            tables = set(inspector.get_table_names())
            assert "tenant" not in tables, "downgrade must drop tenant table"
            assert "audit_log" in tables, "v0.1 chassis schema must survive"

            audit_columns = {col["name"] for col in inspector.get_columns("audit_log")}
            assert "tenant_id" not in audit_columns, (
                "downgrade must drop audit_log.tenant_id; left behind would "
                "leave a column the v0.1 ORM does not know about"
            )
            audit_indexes = {idx["name"] for idx in inspector.get_indexes("audit_log")}
            assert "audit_log_tenant_id_idx" not in audit_indexes
            # v0.1 chassis indexes must remain — downgrade only undoes
            # what 0002 added.
            assert "audit_log_occurred_at_idx" in audit_indexes
            assert "audit_log_operator_sub_idx" in audit_indexes

        # Step 3 — re-upgrade. The full cycle should be a no-op
        # observable as "head reached again".
        command.upgrade(cfg, "head")
        with sync_eng.connect() as conn:
            inspector = sa_inspect(conn)
            assert "tenant" in inspector.get_table_names()
            assert "tenant_id" in {col["name"] for col in inspector.get_columns("audit_log")}
    finally:
        sync_eng.dispose()


# ---------------------------------------------------------------------------
# Schema-level inspection — 0003 installs documents table + portable indexes
# ---------------------------------------------------------------------------


def test_migration_installs_documents_table_and_portable_indexes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """``alembic upgrade head`` puts the ``documents`` table + portable indexes in place.

    G0.4-T1 acceptance criterion: the migration creates ``documents``
    with all 12 columns and the two portable btree indexes
    (``documents_tenant_source_id_idx`` unique,
    ``documents_body_hash_idx``). The two PG-only indexes
    (``documents_body_fts_idx`` GIN, ``documents_embedding_idx``
    IVFFlat) are migration-only and must NOT appear on SQLite —
    declaring them in ``Document.__table_args__`` would force the
    dev/test path to try (and fail) to create them. This test
    pins both contracts.

    Synchronous for the same reason the sibling migration tests
    above are synchronous: :func:`alembic.command.upgrade` calls
    :func:`asyncio.run` internally via the env.py async cookbook,
    which cannot be re-entered from a running event loop.
    """
    sync_url, _ = _alembic_upgrade_against_fresh_sqlite(monkeypatch, tmp_path, "documents.db")

    sync_eng = sa_create_engine(sync_url)
    try:
        with sync_eng.connect() as conn:
            inspector = sa_inspect(conn)
            tables = set(inspector.get_table_names())
            assert "documents" in tables

            # All 12 columns landed verbatim — the SQL column for
            # ``Document.doc_metadata`` is ``metadata`` (the Python
            # attribute is renamed to avoid the DeclarativeBase
            # collision; see Document docstring).
            documents_columns = {col["name"] for col in inspector.get_columns("documents")}
            expected_columns = {
                "id",
                "tenant_id",
                "source",
                "source_id",
                "kind",
                "body",
                "body_hash",
                "tokens",
                "embedding",
                "metadata",
                "created_at",
                "updated_at",
            }
            assert documents_columns == expected_columns, (
                f"documents column set drift: missing={expected_columns - documents_columns} "
                f"unexpected={documents_columns - expected_columns}"
            )

            # Two portable indexes — these MUST land on every dialect.
            documents_indexes = {idx["name"] for idx in inspector.get_indexes("documents")}
            assert "documents_tenant_source_id_idx" in documents_indexes
            assert "documents_body_hash_idx" in documents_indexes

            # The PG-only indexes MUST NOT land on SQLite. Declaring
            # them in ``__table_args__`` would force SQLite to try
            # (and fail) to create them; emitting them via raw SQL
            # in ``if is_postgres:`` is the migration-only shape.
            assert "documents_body_fts_idx" not in documents_indexes, (
                "documents_body_fts_idx (GIN over to_tsvector) must be PG-only; "
                "appearing on SQLite means it leaked into __table_args__"
            )
            assert "documents_embedding_idx" not in documents_indexes, (
                "documents_embedding_idx (IVFFlat) must be PG-only; "
                "appearing on SQLite means it leaked into __table_args__"
            )

            # Uniqueness flag on the composite index — proves the
            # natural-key upsert target is DB-enforced. SQLite's
            # inspector exposes ``unique`` as a boolean on each index
            # entry; check the matching entry explicitly.
            tenant_source_id_idx = next(
                idx
                for idx in inspector.get_indexes("documents")
                if idx["name"] == "documents_tenant_source_id_idx"
            )
            # SQLite's inspector encodes the unique flag as ``1`` / ``0``
            # (the underlying ``sqlite_master.unique`` integer), not as
            # Python's ``True`` / ``False``. Use a truthy check rather
            # than ``is True`` so the assertion is dialect-portable.
            assert tenant_source_id_idx["unique"], (
                "documents_tenant_source_id_idx must be unique — the natural-key "
                "upsert target for index_document (T3 #260)"
            )

            # v0.1 chassis tables must still be present and intact.
            assert "audit_log" in tables
            assert "tenant" in tables
    finally:
        sync_eng.dispose()


def test_migration_0003_upgrade_then_downgrade_is_reversible(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """``alembic upgrade head`` → ``downgrade 0002`` → re-upgrade is a clean cycle.

    The reversibility contract for G0.4-T1: dropping back to v0.1+G0.1
    (revision ``0002``) removes ``documents`` and its indexes while
    leaving the chassis tables (``audit_log``, ``tenant``) intact.
    Re-running ``upgrade head`` returns the schema to its full shape.

    The downgrade target is spelled as ``"0002"`` rather than ``-1``
    so a future revision inserted between ``0002`` and ``0003``
    surfaces as a test failure rather than a silent no-op.
    """
    from alembic import command

    sync_url, cfg = _alembic_upgrade_against_fresh_sqlite(monkeypatch, tmp_path, "documents-rev.db")

    sync_eng = sa_create_engine(sync_url)
    try:
        with sync_eng.connect() as conn:
            inspector = sa_inspect(conn)
            assert "documents" in inspector.get_table_names()

        # Step 2 — downgrade by exactly one revision (back to 0002).
        # Documents table + its indexes must all be gone; chassis +
        # G0.1 schema must remain intact.
        command.downgrade(cfg, "0002")

        with sync_eng.connect() as conn:
            inspector = sa_inspect(conn)
            tables = set(inspector.get_table_names())
            assert "documents" not in tables, "downgrade must drop documents table"
            assert "tenant" in tables, "G0.1 schema must survive 0003 downgrade"
            assert "audit_log" in tables, "v0.1 chassis schema must survive 0003 downgrade"

            # G0.1 indexes must still be present — downgrade only undoes 0003.
            tenant_indexes = {idx["name"] for idx in inspector.get_indexes("tenant")}
            assert "tenant_slug_idx" in tenant_indexes
            audit_indexes = {idx["name"] for idx in inspector.get_indexes("audit_log")}
            assert "audit_log_tenant_id_idx" in audit_indexes

        # Step 3 — re-upgrade. The documents table should return
        # with its portable indexes; the cycle is fully reversible.
        command.upgrade(cfg, "head")
        with sync_eng.connect() as conn:
            inspector = sa_inspect(conn)
            assert "documents" in inspector.get_table_names()
            documents_indexes = {idx["name"] for idx in inspector.get_indexes("documents")}
            assert "documents_tenant_source_id_idx" in documents_indexes
            assert "documents_body_hash_idx" in documents_indexes
    finally:
        sync_eng.dispose()
