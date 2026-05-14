# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for :class:`meho_backplane.db.models.BroadcastOverride`.

Coverage matrix (Task #378 / G6.3-T1 acceptance criteria):

* Round-trip on :class:`BroadcastOverride` -- insert a row, query it
  back, every field round-trips through the SQLite dev/test driver.
  The ORM ``default=`` machinery (uuid, created_at, updated_at) fires
  on the SQLite path where the migration's PG server defaults are
  no-ops.
* ORM defaults fire on SQLite -- a minimal insert that omits
  ``id`` / ``created_at`` / ``updated_at`` still commits with those
  fields populated Python-side, catching a regression where someone
  drops the ORM default in favour of relying on the migration.
* Composite uniqueness -- two inserts with identical
  ``(tenant_id, op_id_pattern, scope_field, scope_value)`` raise
  :class:`IntegrityError` on the second commit. Pins the
  ``broadcast_override_tenant_unique_idx`` contract that T4's CRUD
  upserts will rely on.
* Foreign key enforcement -- ``tenant_id`` references ``tenant.id``;
  inserting with a non-existent tenant id raises :class:`IntegrityError`
  when SQLite has ``PRAGMA foreign_keys = ON``. The PG production path
  enforces this unconditionally.
* ``NULL`` scope -- both ``scope_field`` and ``scope_value`` ``NULL``
  is allowed (op-wide rule); the round-trip preserves them as ``None``.
* ``onupdate`` -- modifying a row via the ORM bumps ``updated_at``.
* Migration installs the table + both indexes; the dialect-portable
  shape (b-tree indexes only, no PG-only GIN/IVFFlat additions) survives
  on SQLite.
* Migration ``0007`` is fully reversible: ``alembic upgrade head`` →
  ``alembic downgrade 0006`` drops the table and both indexes; the
  earlier schema (audit_log, tenant, documents, targets, operation
  substrate) survives intact; re-upgrading to ``head`` restores
  everything.

The tests run against ``sqlite+aiosqlite`` via the shared engine cache
that the autouse ``_default_database_url`` fixture in
:mod:`tests.conftest` already pre-migrates to ``alembic upgrade head``.
Per-test isolation comes from pytest's ``tmp_path``-scoped DB file --
same shape every other DB-touching test in the suite uses. PG-real
assertions (CHECK absence, b-tree DDL spelling on
``pg_index.indexdef``) live in the existing testcontainers suite when
that lane runs against this migration.
"""

from __future__ import annotations

import asyncio
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
from meho_backplane.db.models import BroadcastOverride, Tenant
from meho_backplane.settings import get_settings


async def _enable_sqlite_foreign_keys(session: AsyncSession) -> None:
    """Issue ``PRAGMA foreign_keys = ON`` on the bound SQLite connection.

    SQLite ships with foreign-key enforcement disabled by default
    (sqlite.org/foreignkeys.html §2). Without this PRAGMA, the
    FK on :attr:`BroadcastOverride.tenant_id` is silently a soft
    constraint -- the row commits even when ``tenant_id`` references
    nothing. The pragma is per-connection; emitting it once on the
    session's bound connection covers every statement that follows.
    The engine in the test suite uses :class:`StaticPool` on SQLite so
    a single connection backs every checkout in this process. Same
    pattern as :func:`tests.test_db_endpoint_descriptor._enable_sqlite_foreign_keys`.
    """
    await session.execute(text("PRAGMA foreign_keys = ON"))


@pytest.fixture(autouse=True)
def _required_settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin every env var :class:`Settings` requires for this module.

    Mirrors :func:`tests.test_db_documents._required_settings_env`: the
    autouse ``_default_database_url`` fixture in :mod:`tests.conftest`
    only pins ``DATABASE_URL``; Keycloak / Vault knobs come from each
    test file. The ``get_settings.cache_clear()`` brackets prevent a
    stale ``Settings`` instance from a previous test from leaking.
    """
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


async def _seed_tenant(session: AsyncSession, slug: str = "broadcast-override-test") -> uuid.UUID:
    """Insert a :class:`Tenant` row and return its id.

    Every override row carries a real ``REFERENCES tenant(id)`` FK; the
    tests that exercise insert/round-trip / unique / NULL paths need a
    real parent tenant row when FKs are enforced. Returns the new id so
    the caller can attach overrides to it.
    """
    tenant_id = uuid.uuid4()
    session.add(Tenant(id=tenant_id, slug=slug, name=f"Tenant for {slug}"))
    await session.commit()
    return tenant_id


# ---------------------------------------------------------------------------
# Round-trip
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_broadcast_override_round_trip_persists_every_field() -> None:
    """Insert a :class:`BroadcastOverride`, query it back, every field matches.

    Drives the model via :func:`get_sessionmaker` so the path matches
    what production callers (T4's CRUD verbs, T2's resolver) will use.
    Asserts every column round-trips -- catches a regression where a
    future column rename / type swap silently drops data.
    """
    sessionmaker = get_sessionmaker()
    override_id = uuid.uuid4()
    created_at = datetime.now(UTC)
    async with sessionmaker() as session:
        tenant_id = await _seed_tenant(session)
        session.add(
            BroadcastOverride(
                id=override_id,
                tenant_id=tenant_id,
                op_id_pattern="vault.kv.*",
                scope_field="target_name",
                scope_value="prod-vault",
                detail="aggregate",
                created_by_sub="op-42",
                created_at=created_at,
                updated_at=created_at,
            )
        )
        await session.commit()

    async with sessionmaker() as session:
        result = await session.execute(
            select(BroadcastOverride).where(BroadcastOverride.id == override_id)
        )
        row = result.scalar_one()

    assert row.id == override_id
    assert row.tenant_id == tenant_id
    assert row.op_id_pattern == "vault.kv.*"
    assert row.scope_field == "target_name"
    assert row.scope_value == "prod-vault"
    assert row.detail == "aggregate"
    assert row.created_by_sub == "op-42"
    # SQLite drops tzinfo on round-trip; compare wall-clock parts. The
    # PG production driver returns tz-aware values (covered by the
    # testcontainers suite).
    assert row.created_at.replace(tzinfo=None) == created_at.replace(tzinfo=None)
    assert row.updated_at.replace(tzinfo=None) == created_at.replace(tzinfo=None)


@pytest.mark.asyncio
async def test_broadcast_override_orm_defaults_fire_on_sqlite() -> None:
    """``id``, ``created_at``, ``updated_at`` get populated by ORM.

    The migration's PG-side server defaults (``gen_random_uuid()``,
    ``now()``) are no-ops on SQLite. The ORM
    ``default=uuid.uuid4`` / ``default=lambda: datetime.now(UTC)``
    must fill the columns Python-side. A regression where someone
    drops an ORM default in favour of relying on the migration would
    surface here as a NOT NULL violation on SQLite.
    """
    sessionmaker = get_sessionmaker()
    before = datetime.now(UTC)
    async with sessionmaker() as session:
        tenant_id = await _seed_tenant(session, slug="orm-defaults-probe")
        override = BroadcastOverride(
            tenant_id=tenant_id,
            op_id_pattern="audit.*",
            detail="aggregate",
            created_by_sub="op-orm-defaults",
        )
        session.add(override)
        await session.commit()
        # Capture in-session after commit so defaults are observable.
        seen_id = override.id
        seen_created_at = override.created_at
        seen_updated_at = override.updated_at
        seen_scope_field = override.scope_field
        seen_scope_value = override.scope_value

    assert isinstance(seen_id, uuid.UUID)
    # Bracket the wall-clock check to absorb minor clock drift between
    # ``before`` and the ORM's default-callable firing.
    assert seen_created_at.replace(tzinfo=None) >= before.replace(tzinfo=None)
    assert seen_updated_at.replace(tzinfo=None) >= before.replace(tzinfo=None)
    # Pydantic-shaped optional fields default to None on insert when
    # the caller doesn't pass them -- the ``scope_field=NULL`` case is
    # how op-wide rules are written. Pin the None-default contract so
    # a future regression that defaults them to "" surfaces here.
    assert seen_scope_field is None
    assert seen_scope_value is None


# ---------------------------------------------------------------------------
# Composite unique index
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_broadcast_override_composite_unique_rejects_duplicates() -> None:
    """Two rows sharing (tenant_id, op_id_pattern, scope_field, scope_value) → IntegrityError.

    Locks in that migration ``0007``'s
    ``broadcast_override_tenant_unique_idx`` is the natural-key target
    T4's upsert will use. Without DB-layer uniqueness, two concurrent
    tenant-admin CRUD calls could land duplicate rules for the same
    scope, leaving the resolver in T2 to disambiguate ambiguously
    overlapping rows at publish time.
    """
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        tenant_id = await _seed_tenant(session, slug="composite-unique")
        session.add(
            BroadcastOverride(
                tenant_id=tenant_id,
                op_id_pattern="k8s.configmap.info",
                scope_field="namespace",
                scope_value="kube-system",
                detail="aggregate",
                created_by_sub="op-1",
            )
        )
        await session.commit()

    async with sessionmaker() as session:
        session.add(
            BroadcastOverride(
                tenant_id=tenant_id,
                op_id_pattern="k8s.configmap.info",
                scope_field="namespace",
                scope_value="kube-system",
                detail="full",  # Different detail; still a duplicate.
                created_by_sub="op-2",
            )
        )
        with pytest.raises(IntegrityError):
            await session.commit()


@pytest.mark.asyncio
async def test_broadcast_override_distinct_scopes_under_same_tenant_allowed() -> None:
    """Same tenant_id, different scopes commit cleanly.

    Pins that the composite uniqueness key includes every scope axis
    (op_id_pattern + scope_field + scope_value), not just tenant + op.
    A tenant admin can configure multiple per-namespace overrides for
    the same op pattern in the same tenant, and an op-wide override
    can coexist with a scoped one for the same pattern.
    """
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        tenant_id = await _seed_tenant(session, slug="distinct-scopes")
        session.add_all(
            [
                # Op-wide rule for the pattern.
                BroadcastOverride(
                    tenant_id=tenant_id,
                    op_id_pattern="k8s.configmap.info",
                    scope_field=None,
                    scope_value=None,
                    detail="aggregate",
                    created_by_sub="op-1",
                ),
                # Scoped exception #1.
                BroadcastOverride(
                    tenant_id=tenant_id,
                    op_id_pattern="k8s.configmap.info",
                    scope_field="namespace",
                    scope_value="kube-system",
                    detail="aggregate",
                    created_by_sub="op-1",
                ),
                # Scoped exception #2 (different value).
                BroadcastOverride(
                    tenant_id=tenant_id,
                    op_id_pattern="k8s.configmap.info",
                    scope_field="namespace",
                    scope_value="ingress-nginx",
                    detail="aggregate",
                    created_by_sub="op-1",
                ),
            ]
        )
        # All three rows must commit cleanly -- their composite keys differ.
        await session.commit()

    async with sessionmaker() as session:
        result = await session.execute(
            select(BroadcastOverride).where(BroadcastOverride.tenant_id == tenant_id)
        )
        rows = result.scalars().all()

    assert len(rows) == 3


@pytest.mark.asyncio
async def test_broadcast_override_cross_tenant_same_scope_allowed() -> None:
    """Two tenants with identical (op_id_pattern, scope_field, scope_value) commit.

    The composite uniqueness key includes ``tenant_id`` as its first
    column, so two distinct tenants writing the same logical rule do
    not collide. Tenant boundaries hold per the v0.2 cross-tenant
    invariant.
    """
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        tenant_a = await _seed_tenant(session, slug="cross-tenant-a")
        tenant_b = await _seed_tenant(session, slug="cross-tenant-b")
        session.add_all(
            [
                BroadcastOverride(
                    tenant_id=tenant_a,
                    op_id_pattern="vault.kv.*",
                    scope_field="target_name",
                    scope_value="prod-vault",
                    detail="aggregate",
                    created_by_sub="op-a",
                ),
                BroadcastOverride(
                    tenant_id=tenant_b,
                    op_id_pattern="vault.kv.*",
                    scope_field="target_name",
                    scope_value="prod-vault",
                    detail="aggregate",
                    created_by_sub="op-b",
                ),
            ]
        )
        await session.commit()

    async with sessionmaker() as session:
        result = await session.execute(
            select(BroadcastOverride).where(
                BroadcastOverride.op_id_pattern == "vault.kv.*",
            )
        )
        rows = result.scalars().all()

    assert {row.tenant_id for row in rows} == {tenant_a, tenant_b}


# ---------------------------------------------------------------------------
# Foreign key — tenant_id → tenant.id
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_broadcast_override_tenant_id_fk_enforced() -> None:
    """Insert with an unknown ``tenant_id`` raises :class:`IntegrityError`.

    The FK ``REFERENCES tenant(id)`` is enforced unconditionally on PG
    and conditionally on SQLite (only when ``PRAGMA foreign_keys = ON``
    is issued on the connection -- SQLite's default is OFF per
    sqlite.org/foreignkeys.html §2). Locks in the substrate-boundary
    invariant the resolver in T2 will rely on: every override row
    points at a real tenant.
    """
    sessionmaker = get_sessionmaker()
    bogus_tenant_id = uuid.uuid4()
    async with sessionmaker() as session:
        await _enable_sqlite_foreign_keys(session)
        session.add(
            BroadcastOverride(
                tenant_id=bogus_tenant_id,
                op_id_pattern="vault.kv.read",
                detail="aggregate",
                created_by_sub="op-bogus",
            )
        )
        with pytest.raises(IntegrityError):
            await session.commit()


# ---------------------------------------------------------------------------
# NULL scope (op-wide rule)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_broadcast_override_null_scope_field_and_value_round_trip() -> None:
    """``scope_field=NULL, scope_value=NULL`` (op-wide rule) round-trips cleanly.

    The op-wide rule shape is the most common form -- T4's CLI default
    omits the scope flags. The model must store and read back the
    ``(None, None)`` pair without collapsing it to an empty string or
    a JSON null. A regression that swaps ``Mapped[str | None]`` for
    ``Mapped[str]`` would surface here as a NOT NULL violation.
    """
    sessionmaker = get_sessionmaker()
    override_id = uuid.uuid4()
    async with sessionmaker() as session:
        tenant_id = await _seed_tenant(session, slug="null-scope")
        session.add(
            BroadcastOverride(
                id=override_id,
                tenant_id=tenant_id,
                op_id_pattern="audit.query",
                scope_field=None,
                scope_value=None,
                detail="aggregate",
                created_by_sub="op-null-scope",
            )
        )
        await session.commit()

    async with sessionmaker() as session:
        result = await session.execute(
            select(BroadcastOverride).where(BroadcastOverride.id == override_id)
        )
        row = result.scalar_one()

    assert row.scope_field is None
    assert row.scope_value is None
    assert row.op_id_pattern == "audit.query"
    assert row.detail == "aggregate"


# ---------------------------------------------------------------------------
# updated_at ORM onupdate
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_broadcast_override_updated_at_refreshes_on_orm_update() -> None:
    """Modifying a row via the ORM bumps ``updated_at``.

    The ``onupdate=lambda: datetime.now(UTC)`` on
    :attr:`BroadcastOverride.updated_at` is the ORM-level trigger that
    keeps the timestamp fresh. T4's PATCH verb will rely on this to
    answer "when was this rule last edited?" in the admin UI / audit
    surface. A regression that drops the ``onupdate`` (e.g. by moving
    to a PG-side trigger and forgetting to keep the ORM hook for
    SQLite) would silently freeze the column on every UPDATE.
    """
    sessionmaker = get_sessionmaker()
    override_id = uuid.uuid4()
    async with sessionmaker() as session:
        tenant_id = await _seed_tenant(session, slug="onupdate-probe")
        session.add(
            BroadcastOverride(
                id=override_id,
                tenant_id=tenant_id,
                op_id_pattern="vault.kv.read",
                detail="aggregate",
                created_by_sub="op-onupdate",
            )
        )
        await session.commit()
        original_updated_at = (
            await session.execute(
                select(BroadcastOverride.updated_at).where(BroadcastOverride.id == override_id)
            )
        ).scalar_one()

    # Brief async pause so the next ``datetime.now(UTC)`` lands a
    # measurable delta on systems where the ORM ``onupdate`` callable
    # resolves in the same microsecond as the insert. Same shape
    # ``test_db_documents.test_document_updated_at_refreshes_on_orm_update``
    # uses; a blocking ``time.sleep`` would stall every in-flight
    # coroutine on the asyncio_mode="auto" event loop.
    await asyncio.sleep(0.01)

    async with sessionmaker() as session:
        row = (
            await session.execute(
                select(BroadcastOverride).where(BroadcastOverride.id == override_id)
            )
        ).scalar_one()
        row.detail = "full"
        await session.commit()
        post_commit_updated_at = row.updated_at

    # Strict ``>`` (not ``>=``): the 10 ms sleep buys a guaranteed
    # delta, so an equal timestamp means ``onupdate`` did not fire --
    # which is exactly the regression this test claims to catch.
    assert post_commit_updated_at.replace(tzinfo=None) > original_updated_at.replace(tzinfo=None), (
        "ORM onupdate must advance updated_at on every UPDATE; "
        f"original={original_updated_at} post={post_commit_updated_at}"
    )


# ---------------------------------------------------------------------------
# Schema-level inspection — migration installs the documented table + indexes
# ---------------------------------------------------------------------------


def _alembic_upgrade_against_fresh_sqlite(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    db_filename: str,
) -> tuple[str, Config]:
    """Pin env, reset caches, run ``alembic upgrade head`` on fresh SQLite.

    Shared setup for the sync migration tests below; mirrors the helper
    in :mod:`tests.test_db_endpoint_descriptor`. Returns
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


def test_migration_installs_broadcast_override_table_and_indexes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """``alembic upgrade head`` puts the table + both indexes in place.

    Asserts via SQLite's schema inspector (the dialect-portable
    equivalent of ``\\d+`` against PG):

    * The ``broadcast_override`` table exists with every documented
      column.
    * Both indexes are present (``broadcast_override_tenant_unique_idx``
      unique composite, ``broadcast_override_tenant_idx`` b-tree).

    PG-side verification (``\\d+`` against a real container) lives in
    the existing testcontainers suite that runs on CI.
    """
    sync_url, _ = _alembic_upgrade_against_fresh_sqlite(monkeypatch, tmp_path, "g063-schema.db")

    sync_eng = sa_create_engine(sync_url)
    try:
        with sync_eng.connect() as conn:
            inspector = sa_inspect(conn)
            tables = set(inspector.get_table_names())
            assert "broadcast_override" in tables, (
                "alembic upgrade head must create the broadcast_override table"
            )

            columns = {col["name"] for col in inspector.get_columns("broadcast_override")}
            expected_columns = {
                "id",
                "tenant_id",
                "op_id_pattern",
                "scope_field",
                "scope_value",
                "detail",
                "created_by_sub",
                "created_at",
                "updated_at",
            }
            assert expected_columns <= columns, (
                f"Missing columns in broadcast_override: {expected_columns - columns}"
            )

            indexes = {idx["name"] for idx in inspector.get_indexes("broadcast_override")}
            assert "broadcast_override_tenant_unique_idx" in indexes, (
                "composite uniqueness index missing -- T4's upsert contract would break"
            )
            assert "broadcast_override_tenant_idx" in indexes, (
                "tenant lookup index missing -- T2's resolver would scan the table"
            )

            # The unique composite index must actually be unique --
            # SQLite records ``unique`` on the index spec.
            unique_idx = next(
                idx
                for idx in inspector.get_indexes("broadcast_override")
                if idx["name"] == "broadcast_override_tenant_unique_idx"
            )
            # Truthy check (not ``is True``): SQLite's inspector returns
            # the unique flag as an ``int`` (1 / 0) while PG returns a
            # real ``bool``. Truthiness covers both dialects.
            assert unique_idx["unique"], (
                "broadcast_override_tenant_unique_idx must be a UNIQUE index"
            )
            assert unique_idx["column_names"] == [
                "tenant_id",
                "op_id_pattern",
                "scope_field",
                "scope_value",
            ], "composite index must cover all four scope-key columns in order"
    finally:
        sync_eng.dispose()


def test_migration_upgrade_then_downgrade_is_reversible(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """``alembic upgrade head`` → ``alembic downgrade 0006`` is a clean cycle.

    Proves migration ``0007`` is fully reversible: after downgrading by
    exactly one revision (back to ``0006``), the new table and both
    indexes must be gone while the rest of the schema (``targets``,
    ``documents``, ``tenant``, ``audit_log``, ``operation_group``,
    ``endpoint_descriptor``) remains intact. Re-upgrading to ``head``
    must restore everything.

    The downgrade target is the previous revision (``0006``); we spell
    it explicitly rather than relying on ``-1`` arithmetic so a future
    revision inserted between ``0006`` and ``0007`` surfaces as a test
    failure rather than a silent no-op.
    """
    from alembic import command

    sync_url, cfg = _alembic_upgrade_against_fresh_sqlite(monkeypatch, tmp_path, "g063-rev.db")

    sync_eng = sa_create_engine(sync_url)
    try:
        with sync_eng.connect() as conn:
            inspector = sa_inspect(conn)
            assert "broadcast_override" in set(inspector.get_table_names())

        # Downgrade by exactly one revision -- back to 0006 (audit_log
        # parent_audit_id).
        command.downgrade(cfg, "0006")

        with sync_eng.connect() as conn:
            inspector = sa_inspect(conn)
            tables = set(inspector.get_table_names())
            assert "broadcast_override" not in tables, (
                "downgrade must drop broadcast_override table"
            )
            # Earlier-migration schema survives.
            assert "operation_group" in tables, "G0.6 operation_group must survive"
            assert "endpoint_descriptor" in tables, "G0.6 endpoint_descriptor must survive"
            assert "targets" in tables, "v0.2 targets must survive"
            assert "documents" in tables, "v0.2 documents must survive"
            assert "tenant" in tables, "v0.2 tenant must survive"
            assert "audit_log" in tables, "v0.1 audit_log must survive"

        # Re-upgrade -- must be idempotent from 0006 back to head.
        command.upgrade(cfg, "head")
        with sync_eng.connect() as conn:
            inspector = sa_inspect(conn)
            tables = set(inspector.get_table_names())
            assert "broadcast_override" in tables, (
                "re-upgrade must restore broadcast_override after downgrade"
            )
    finally:
        sync_eng.dispose()
