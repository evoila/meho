# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for the :class:`~meho_backplane.db.models.Target` ORM model.

Coverage matrix (Task #252 acceptance criteria):

* Round-trip on :class:`Target` — insert a valid row, query it back,
  every field survives the ORM round-trip. Drives the ORM
  ``default=`` machinery (uuid, created_at, updated_at, auth_model,
  vpn_required, extras) against the SQLite dev/test driver where the
  migration cannot install PG server-side defaults.
* ``(tenant_id, name)`` unique constraint rejects a duplicate — the
  named ``targets_tenant_name_idx`` unique b-tree is the DB-layer
  enforcement of the one-name-per-tenant invariant; this test proves
  it fires at the DB layer, not just at a UI validator.
* ``audit_log.target_id`` round-trips — insert an :class:`AuditLog`
  row with ``target_id`` populated and verify it survives. This is
  the positive case for the new column; the G0.3 CRUD layer will
  write it on target-scoped requests.
* ``aliases`` store/retrieve — NULL (no aliases) and a populated list
  both round-trip correctly. On SQLite the column is stored as a JSON
  array; on PostgreSQL it is a native ``TEXT[]``. The test exercises
  the SQLite path (always-on); the PG path is covered by the existing
  testcontainers suite in ``tests.test_db_engine``.
* Schema-level smoke — ``alembic upgrade head`` against a fresh SQLite
  DB creates the ``targets`` table with its indexes, and the
  ``audit_log_target_id_idx`` lands on the existing audit table. The
  migration's dialect-branch (GIN skipped on SQLite) is also verified
  by asserting that the two b-tree indexes exist while the GIN index
  name is absent on SQLite.

The tests run against ``sqlite+aiosqlite`` via the shared engine cache
that the autouse ``_default_database_url`` fixture in
:mod:`tests.conftest` already pre-migrates to ``alembic upgrade head``.
Per-test isolation comes from pytest's ``tmp_path``-scoped DB file, the
same shape every other DB-touching test in the suite uses.

SQLite datetime caveats — identical to :mod:`tests.test_db_models`:
SQLite stores datetimes as ISO-8601 strings without timezone
information; SQLAlchemy round-trips them as naive ``datetime`` even
when the column is ``DateTime(timezone=True)``. All datetime assertions
strip tzinfo before comparing the wall-clock parts.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import create_engine as sa_create_engine
from sqlalchemy import inspect as sa_inspect
from sqlalchemy import select

from meho_backplane.db.engine import get_sessionmaker, reset_engine_for_testing
from meho_backplane.db.models import AuditLog, Target
from meho_backplane.settings import get_settings


@pytest.fixture(autouse=True)
def _required_settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin every env var :class:`Settings` requires for this module.

    Mirrors the pattern in :mod:`tests.test_db_models`: the autouse
    ``_default_database_url`` fixture only pins ``DATABASE_URL``;
    Keycloak/Vault knobs come from each test file. The
    ``get_settings.cache_clear()`` brackets prevent a stale
    ``Settings`` instance from a previous test from leaking.
    """
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


# ---------------------------------------------------------------------------
# Target round-trip
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_target_round_trip_persists_every_field() -> None:
    """Insert a :class:`Target`, query it back, every field matches.

    Exercises the ORM ``default=`` machinery (uuid, created_at,
    updated_at, auth_model, vpn_required, extras) against the SQLite
    driver where the migration's PG server-side defaults are no-ops.
    Asserting on every field is what proves the column shape, type
    mapping, and default machinery are wired correctly before the G0.3
    CRUD layer (T2+) starts writing these rows in earnest.
    """
    sessionmaker = get_sessionmaker()
    target_id = uuid.uuid4()
    tenant_id = uuid.uuid4()
    now = datetime.now(UTC)

    async with sessionmaker() as session:
        session.add(
            Target(
                id=target_id,
                tenant_id=tenant_id,
                name="prod-k8s-cluster",
                aliases=["k8s-prod", "k8s.internal"],
                product="kubernetes",
                host="10.0.0.1",
                port=6443,
                fqdn="k8s.prod.example.com",
                secret_ref="secret/meho/targets/prod-k8s",
                auth_model="shared_service_account",
                vpn_required=True,
                extras={"cluster_version": "1.29"},
                notes="Production Kubernetes cluster",
                created_at=now,
                updated_at=now,
            )
        )
        await session.commit()

    async with sessionmaker() as session:
        result = await session.execute(select(Target).where(Target.id == target_id))
        row = result.scalar_one()

    assert row.id == target_id
    assert row.tenant_id == tenant_id
    assert row.name == "prod-k8s-cluster"
    assert row.aliases == ["k8s-prod", "k8s.internal"]
    assert row.product == "kubernetes"
    assert row.host == "10.0.0.1"
    assert row.port == 6443
    assert row.fqdn == "k8s.prod.example.com"
    assert row.secret_ref == "secret/meho/targets/prod-k8s"
    assert row.auth_model == "shared_service_account"
    assert row.vpn_required is True
    assert row.extras == {"cluster_version": "1.29"}
    assert row.notes == "Production Kubernetes cluster"
    # SQLite strips tzinfo — compare wall-clock parts only.
    assert row.created_at.replace(tzinfo=None) == now.replace(tzinfo=None)
    assert row.updated_at.replace(tzinfo=None) == now.replace(tzinfo=None)


@pytest.mark.asyncio
async def test_target_round_trip_with_nullable_fields_omitted() -> None:
    """Insert a :class:`Target` with only required fields, verify ORM defaults fire.

    Proves that ``port=None``, ``fqdn=None``, ``secret_ref=None``,
    ``notes=None`` survive the round-trip as NULL; ``aliases`` defaults
    to ``[]`` (NOT NULL, empty-list default); and the ORM column defaults
    (``auth_model``, ``vpn_required``, ``extras``) fill in without
    explicit values.
    """
    sessionmaker = get_sessionmaker()
    target_id = uuid.uuid4()
    tenant_id = uuid.uuid4()

    async with sessionmaker() as session:
        session.add(
            Target(
                id=target_id,
                tenant_id=tenant_id,
                name="minimal-target",
                product="ssh",
                host="192.168.1.10",
            )
        )
        await session.commit()

    async with sessionmaker() as session:
        result = await session.execute(select(Target).where(Target.id == target_id))
        row = result.scalar_one()

    assert row.id == target_id
    assert row.aliases == []
    assert row.port is None
    assert row.fqdn is None
    assert row.secret_ref is None
    assert row.notes is None
    # ORM defaults should have fired.
    assert row.auth_model == "shared_service_account"
    assert row.vpn_required is False
    assert row.extras == {}
    # G0.14-T4 #1145: deleted_at column defaults to NULL for live rows.
    assert row.deleted_at is None


@pytest.mark.asyncio
async def test_target_deleted_at_round_trips() -> None:
    """A :class:`Target` row with ``deleted_at`` set survives the round-trip.

    G0.14-T4 (#1145): the soft-delete handler stamps ``deleted_at``;
    audit-history readers (G8) need the column to round-trip with the
    expected wall-clock semantics for forensic queries against
    retired targets.
    """
    sessionmaker = get_sessionmaker()
    target_id = uuid.uuid4()
    tenant_id = uuid.uuid4()
    now = datetime.now(UTC)

    async with sessionmaker() as session:
        session.add(
            Target(
                id=target_id,
                tenant_id=tenant_id,
                name="retired",
                product="ssh",
                host="10.0.0.1",
                deleted_at=now,
            )
        )
        await session.commit()

    async with sessionmaker() as session:
        result = await session.execute(select(Target).where(Target.id == target_id))
        row = result.scalar_one()

    # SQLite strips tzinfo — compare wall-clock parts only.
    assert row.deleted_at is not None
    assert row.deleted_at.replace(tzinfo=None) == now.replace(tzinfo=None)


# ---------------------------------------------------------------------------
# (tenant_id, name) unique constraint
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_target_tenant_name_uniqueness_enforced() -> None:
    """Two :class:`Target` rows with the same (tenant_id, name) → IntegrityError.

    The migration enforces uniqueness on ``(tenant_id, name)`` via the
    named ``targets_tenant_name_idx`` (declared ``unique=True``); this
    test proves the constraint fires at the DB layer. Without it, a
    future migration that accidentally dropped the unique flag would
    silently allow duplicate target names per tenant.
    """
    from sqlalchemy.exc import IntegrityError

    sessionmaker = get_sessionmaker()
    tenant_id = uuid.uuid4()

    async with sessionmaker() as session:
        session.add(
            Target(
                id=uuid.uuid4(),
                tenant_id=tenant_id,
                name="dup-name",
                product="ssh",
                host="10.0.0.1",
            )
        )
        await session.commit()

    async with sessionmaker() as session:
        session.add(
            Target(
                id=uuid.uuid4(),
                tenant_id=tenant_id,
                name="dup-name",
                product="kubernetes",
                host="10.0.0.2",
            )
        )
        with pytest.raises(IntegrityError):
            await session.commit()


@pytest.mark.asyncio
async def test_target_same_name_allowed_across_different_tenants() -> None:
    """Same target name in two different tenants does not raise.

    The uniqueness constraint is on ``(tenant_id, name)``, not just
    ``name``. Two tenants must be able to each have a ``prod-k8s``
    target; denying them would force awkward global namespacing.
    """
    sessionmaker = get_sessionmaker()
    tenant_a = uuid.uuid4()
    tenant_b = uuid.uuid4()

    async with sessionmaker() as session:
        session.add(
            Target(
                id=uuid.uuid4(),
                tenant_id=tenant_a,
                name="shared-name",
                product="ssh",
                host="10.0.0.1",
            )
        )
        session.add(
            Target(
                id=uuid.uuid4(),
                tenant_id=tenant_b,
                name="shared-name",
                product="ssh",
                host="10.0.0.2",
            )
        )
        # Must not raise — different tenants, same name is allowed.
        await session.commit()

    async with sessionmaker() as session:
        result = await session.execute(select(Target).where(Target.name == "shared-name"))
        rows = result.scalars().all()

    assert len(rows) == 2
    assert {r.tenant_id for r in rows} == {tenant_a, tenant_b}


# ---------------------------------------------------------------------------
# audit_log.target_id round-trip
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_audit_log_round_trip_with_target_id() -> None:
    """Insert an :class:`AuditLog` with ``target_id`` set, read it back.

    Positive case for the new column: the G0.3 CRUD layer will populate
    ``target_id`` on every target-scoped request. Round-tripping it now
    proves the column shape, type, and indexability are wired correctly
    before the CRUD layer writes land.
    """
    sessionmaker = get_sessionmaker()
    audit_id = uuid.uuid4()
    target_id = uuid.uuid4()
    occurred_at = datetime.now(UTC)

    async with sessionmaker() as session:
        session.add(
            AuditLog(
                id=audit_id,
                occurred_at=occurred_at,
                operator_sub="op-target-positive",
                method="POST",
                path="/api/v1/targets",
                status_code=201,
                request_id=None,
                duration_ms=None,
                payload={},
                target_id=target_id,
            )
        )
        await session.commit()

    async with sessionmaker() as session:
        result = await session.execute(select(AuditLog).where(AuditLog.id == audit_id))
        row = result.scalar_one()

    assert row.target_id == target_id
    assert row.operator_sub == "op-target-positive"


@pytest.mark.asyncio
async def test_audit_log_round_trip_with_null_target_id() -> None:
    """Insert an :class:`AuditLog` without ``target_id``, read it back as None.

    Generic requests (health, policy listing) leave ``target_id`` NULL.
    This test pins the contract that NULL survives the round-trip —
    a future default on ``target_id`` (e.g. ``default=uuid.uuid4`` for
    the wrong reason) would break the generic-request shape and this
    test would fail.
    """
    sessionmaker = get_sessionmaker()
    audit_id = uuid.uuid4()
    occurred_at = datetime.now(UTC)

    async with sessionmaker() as session:
        session.add(
            AuditLog(
                id=audit_id,
                occurred_at=occurred_at,
                operator_sub="op-target-null",
                method="GET",
                path="/api/v1/health",
                status_code=200,
                request_id=None,
                duration_ms=None,
                payload={},
                # target_id deliberately omitted — generic request shape.
            )
        )
        await session.commit()

    async with sessionmaker() as session:
        result = await session.execute(select(AuditLog).where(AuditLog.id == audit_id))
        row = result.scalar_one()

    assert row.target_id is None
    assert row.operator_sub == "op-target-null"


# ---------------------------------------------------------------------------
# aliases store/retrieve
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_target_aliases_empty_round_trips() -> None:
    """``aliases`` defaults to ``[]`` when omitted; empty list survives round-trip.

    The column is NOT NULL with an empty-list default so there is no
    NULL vs [] ambiguity and = ANY(aliases) queries on PG always work.
    """
    sessionmaker = get_sessionmaker()
    target_id = uuid.uuid4()

    async with sessionmaker() as session:
        session.add(
            Target(
                id=target_id,
                tenant_id=uuid.uuid4(),
                name="no-alias-target",
                product="ssh",
                host="10.0.0.3",
            )
        )
        await session.commit()

    async with sessionmaker() as session:
        result = await session.execute(select(Target).where(Target.id == target_id))
        row = result.scalar_one()

    assert row.aliases == []


@pytest.mark.asyncio
async def test_target_aliases_populated_round_trips() -> None:
    """A non-empty ``aliases`` list survives the ORM round-trip intact.

    On SQLite the list is stored as a JSON array and deserialized back
    on read; on PG it uses a native TEXT[] column. Both paths must
    return the same Python list with the elements in insertion order.
    """
    sessionmaker = get_sessionmaker()
    target_id = uuid.uuid4()
    aliases = ["legacy-host", "host.corp.internal", "10.0.0.4"]

    async with sessionmaker() as session:
        session.add(
            Target(
                id=target_id,
                tenant_id=uuid.uuid4(),
                name="alias-target",
                product="kubernetes",
                host="10.0.0.4",
                aliases=aliases,
            )
        )
        await session.commit()

    async with sessionmaker() as session:
        result = await session.execute(select(Target).where(Target.id == target_id))
        row = result.scalar_one()

    assert row.aliases == aliases


# ---------------------------------------------------------------------------
# Schema-level inspection — migration installs the documented indexes
# ---------------------------------------------------------------------------


def _alembic_upgrade_against_fresh_sqlite(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    db_filename: str,
) -> tuple[str, object]:
    """Pin env, reset caches, run ``alembic upgrade head`` on fresh SQLite.

    Shared setup for the sync migration tests below. Returns
    ``(sync_url, alembic_cfg)`` so callers can inspect the resulting
    schema or run further migration ops.

    Mirrors :func:`tests.test_db_models._alembic_upgrade_against_fresh_sqlite`
    exactly; kept local rather than hoisted to conftest because neither
    module needs the other's helper.
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


def test_migration_installs_targets_table_and_indexes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """``alembic upgrade head`` puts the ``targets`` table + indexes in place.

    Asserts via SQLite's schema inspector (the dialect-portable
    equivalent of ``\\d+ targets``):

    * The ``targets`` table exists with all documented columns.
    * The b-tree indexes ``targets_tenant_name_idx`` and
      ``targets_tenant_product_idx`` are present.
    * The GIN index ``targets_aliases_gin_idx`` is **absent** on
      SQLite — the migration's ``if is_postgres:`` guard must have
      fired correctly; presence on SQLite would indicate the guard
      broke.
    * The ``audit_log_target_id_idx`` landed on ``audit_log``.
    * The ``audit_log.target_id`` column is present and nullable.

    PG-side verification (``\\d+`` against a real container) lives in
    the existing testcontainers suite that runs on CI.
    """
    sync_url, _ = _alembic_upgrade_against_fresh_sqlite(monkeypatch, tmp_path, "schema.db")

    sync_eng = sa_create_engine(sync_url)
    try:
        with sync_eng.connect() as conn:
            inspector = sa_inspect(conn)
            tables = set(inspector.get_table_names())
            assert "targets" in tables
            assert "audit_log" in tables

            # Column-set check for ``targets``.
            targets_cols = {col["name"] for col in inspector.get_columns("targets")}
            expected_cols = {
                "id",
                "tenant_id",
                "name",
                "aliases",
                "product",
                "host",
                "port",
                "fqdn",
                "secret_ref",
                "auth_model",
                "vpn_required",
                "extras",
                "notes",
                "created_at",
                "updated_at",
            }
            assert expected_cols <= targets_cols, (
                f"Missing columns in targets: {expected_cols - targets_cols}"
            )

            # Index presence/absence on ``targets``.
            targets_indexes = {idx["name"] for idx in inspector.get_indexes("targets")}
            assert "targets_tenant_name_idx" in targets_indexes
            assert "targets_tenant_product_idx" in targets_indexes
            # GIN index must be absent on SQLite — dialect guard check.
            assert "targets_aliases_gin_idx" not in targets_indexes, (
                "GIN index should be absent on SQLite; the is_postgres guard must have fired"
            )

            # ``audit_log`` gets its new index and column.
            audit_indexes = {idx["name"] for idx in inspector.get_indexes("audit_log")}
            assert "audit_log_target_id_idx" in audit_indexes

            audit_cols = {col["name"] for col in inspector.get_columns("audit_log")}
            assert "target_id" in audit_cols

            # Verify target_id is nullable.
            target_id_col = next(
                col for col in inspector.get_columns("audit_log") if col["name"] == "target_id"
            )
            assert target_id_col["nullable"] is True
    finally:
        sync_eng.dispose()


def test_migration_upgrade_then_downgrade_is_reversible(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """``alembic upgrade head`` → ``alembic downgrade 0003`` is a clean cycle.

    Proves that migration ``0004`` is fully reversible: after
    downgrading by exactly one revision (back to 0003, the documents
    migration), the ``targets`` table and ``audit_log.target_id`` column
    + index must be gone while the rest of the schema (``tenant`` table,
    ``audit_log.tenant_id``) remains intact. Re-upgrading to head must
    restore everything cleanly.
    """
    from alembic import command

    sync_url, cfg = _alembic_upgrade_against_fresh_sqlite(monkeypatch, tmp_path, "rev.db")

    sync_eng = sa_create_engine(sync_url)
    try:
        with sync_eng.connect() as conn:
            inspector = sa_inspect(conn)
            assert "targets" in inspector.get_table_names()
            assert "target_id" in {col["name"] for col in inspector.get_columns("audit_log")}

        # Downgrade by exactly one revision (back to 0003 — documents migration).
        command.downgrade(cfg, "0003")

        with sync_eng.connect() as conn:
            inspector = sa_inspect(conn)
            tables = set(inspector.get_table_names())
            assert "targets" not in tables, "downgrade must drop targets table"
            assert "audit_log" in tables, "v0.2 chassis schema must survive"
            assert "tenant" in tables, "v0.2 tenant table must survive"

            audit_cols = {col["name"] for col in inspector.get_columns("audit_log")}
            assert "target_id" not in audit_cols, (
                "downgrade must drop audit_log.target_id; leaving it would expose "
                "a column the v0.2 ORM does not know about"
            )
            audit_indexes = {idx["name"] for idx in inspector.get_indexes("audit_log")}
            assert "audit_log_target_id_idx" not in audit_indexes
            # v0.2 indexes must remain — downgrade only undoes what 0004 added.
            assert "audit_log_tenant_id_idx" in audit_indexes

        # Re-upgrade — must be idempotent from 0003 back to head.
        command.upgrade(cfg, "head")
        with sync_eng.connect() as conn:
            inspector = sa_inspect(conn)
            assert "targets" in inspector.get_table_names()
            assert "target_id" in {col["name"] for col in inspector.get_columns("audit_log")}
    finally:
        sync_eng.dispose()
