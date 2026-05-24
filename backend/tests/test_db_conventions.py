# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for :class:`TenantConvention` and :class:`TenantConventionHistory`.

Coverage matrix (Task #313 acceptance criteria):

* **Round-trip on :class:`TenantConvention`** -- insert a row, query
  it back, every field survives the ORM round-trip. Drives the ORM
  ``default=`` machinery (uuid, created_at, updated_at, priority)
  against the SQLite dev/test driver where the migration's PG
  server-side defaults are no-ops.
* **Round-trip on :class:`TenantConventionHistory`** -- same shape,
  plus the nullable ``body_before`` semantics for the CREATE event.
* **``priority`` default and explicit value** -- omitted at insert
  defaults to 0; an explicit value (positive or negative) persists.
  Locks the T4 preamble-packing contract: priority is the ranking key.
* **Unique ``(tenant_id, slug)``** -- two rows with the same
  ``(tenant_id, slug)`` pair raise :class:`IntegrityError`. The named
  ``tenant_conventions_tenant_slug_idx`` enforces uniqueness at the
  DB layer, not just at a future UI validator.
* **Cross-tenant: same ``slug`` in two tenants** -- the uniqueness
  constraint is on ``(tenant_id, slug)``, not just ``slug``; two
  tenants must be able to each have a ``rbac-canonical`` convention.
* **Nullable ``body_before``** -- the CREATE event row's
  ``body_before`` round-trips as ``None``.
* **Schema-level smoke** -- ``alembic upgrade head`` against a fresh
  SQLite DB creates the ``tenant_conventions`` + ``tenant_convention_history``
  tables with the documented columns and indexes; the named indexes
  are present and the unique constraint is materialised.
* **Reversibility round-trip** -- ``alembic downgrade "0014"`` (the
  ``down_revision``) drops both tables + indexes; a subsequent
  ``upgrade head`` restores them.

The tests run against ``sqlite+aiosqlite`` via the shared engine
cache that the autouse ``_default_database_url`` fixture in
:mod:`tests.conftest` already pre-migrates to ``alembic upgrade
head``. Per-test isolation comes from the per-test ``tmp_path`` DB
file, the same shape every other DB-touching test in the suite uses.

SQLite datetime caveats -- identical to :mod:`tests.test_db_targets`:
SQLite stores datetimes as ISO-8601 strings without timezone
information; SQLAlchemy round-trips them as naive ``datetime`` even
when the column is ``DateTime(timezone=True)``. All datetime
assertions strip tzinfo before comparing the wall-clock parts.

The schema-level migration tests follow the synchronous pattern from
:mod:`tests.test_migration_0014_agent_session_id`:
:func:`alembic.command.upgrade` calls :func:`asyncio.run` internally
via env.py's async cookbook, so the test function itself must be
sync.
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
from meho_backplane.db.models import TenantConvention, TenantConventionHistory
from meho_backplane.settings import get_settings


@pytest.fixture(autouse=True)
def _required_settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin every env var :class:`Settings` requires for this module.

    Mirrors the pattern in :mod:`tests.test_db_targets`: the autouse
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
# TenantConvention round-trip
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tenant_convention_round_trip_persists_every_field() -> None:
    """Insert a :class:`TenantConvention`, query it back, every field matches.

    Exercises the ORM ``default=`` machinery (uuid, created_at,
    updated_at, priority) against the SQLite driver where the
    migration's PG server-side defaults are no-ops. Asserting on every
    field is what proves the column shape, type mapping, and default
    machinery are wired correctly before T2's API CRUD layer starts
    writing these rows in earnest.
    """
    sessionmaker = get_sessionmaker()
    convention_id = uuid.uuid4()
    tenant_id = uuid.uuid4()
    now = datetime.now(UTC)

    async with sessionmaker() as session:
        session.add(
            TenantConvention(
                id=convention_id,
                tenant_id=tenant_id,
                slug="rbac-canonical",
                title="RBAC is canonical",
                body="Every operation runs through MEHO's RBAC layer.",
                kind="operational",
                priority=10,
                created_by_sub="user:alice",
                created_at=now,
                updated_at=now,
            )
        )
        await session.commit()

    async with sessionmaker() as session:
        result = await session.execute(
            select(TenantConvention).where(TenantConvention.id == convention_id)
        )
        row = result.scalar_one()

    assert row.id == convention_id
    assert row.tenant_id == tenant_id
    assert row.slug == "rbac-canonical"
    assert row.title == "RBAC is canonical"
    assert row.body == "Every operation runs through MEHO's RBAC layer."
    assert row.kind == "operational"
    assert row.priority == 10
    assert row.created_by_sub == "user:alice"
    # SQLite strips tzinfo -- compare wall-clock parts only.
    assert row.created_at.replace(tzinfo=None) == now.replace(tzinfo=None)
    assert row.updated_at.replace(tzinfo=None) == now.replace(tzinfo=None)


@pytest.mark.asyncio
async def test_tenant_convention_round_trip_with_optional_fields_omitted() -> None:
    """Insert a minimal :class:`TenantConvention`, ORM defaults fire.

    Proves that ``created_by_sub=None`` survives the round-trip as NULL
    and the ORM column defaults (``priority=0``, ``created_at``,
    ``updated_at``) fill in without explicit values. Locks the contract
    that T2's ``ConventionCreate`` Pydantic model can omit ``priority``
    (defaults to 0) and ``created_by_sub`` (resolved server-side from
    the JWT) without violating any DB-level NOT NULL.
    """
    sessionmaker = get_sessionmaker()
    convention_id = uuid.uuid4()
    tenant_id = uuid.uuid4()

    async with sessionmaker() as session:
        session.add(
            TenantConvention(
                id=convention_id,
                tenant_id=tenant_id,
                slug="minimal-convention",
                title="Minimal",
                body="Body text.",
                kind="reference",
            )
        )
        await session.commit()

    async with sessionmaker() as session:
        result = await session.execute(
            select(TenantConvention).where(TenantConvention.id == convention_id)
        )
        row = result.scalar_one()

    assert row.id == convention_id
    assert row.created_by_sub is None
    # The Python-side ORM default for priority is 0 (mirrors the PG
    # server default that doesn't fire on SQLite).
    assert row.priority == 0
    # ORM defaults populated the timestamps even without explicit values.
    assert row.created_at is not None
    assert row.updated_at is not None


# ---------------------------------------------------------------------------
# priority semantics
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tenant_convention_priority_default_is_zero_when_unspecified() -> None:
    """Omitting ``priority`` defaults to 0 (the T4 preamble-packing baseline).

    The PG server default of ``0`` and the ORM-side Python default of
    ``0`` are intentionally redundant: PG production gets the server
    default for out-of-band inserts, the ORM provides it for the SQLite
    dev/test path and for ORM inserts that don't include the column.
    The acceptance criterion explicitly calls out the round-trip:
    "round-trip test asserts default = 0 when unspecified".
    """
    sessionmaker = get_sessionmaker()
    convention_id = uuid.uuid4()

    async with sessionmaker() as session:
        session.add(
            TenantConvention(
                id=convention_id,
                tenant_id=uuid.uuid4(),
                slug="no-priority",
                title="Default priority",
                body="Body.",
                kind="reference",
                # priority deliberately omitted to exercise the default.
            )
        )
        await session.commit()

    async with sessionmaker() as session:
        result = await session.execute(
            select(TenantConvention).where(TenantConvention.id == convention_id)
        )
        row = result.scalar_one()

    assert row.priority == 0


@pytest.mark.asyncio
async def test_tenant_convention_priority_explicit_value_persists() -> None:
    """An explicit non-default ``priority`` survives the round-trip.

    Confirms the acceptance criterion bullet: "an explicit value
    persists". Uses a deliberately non-trivial value (250) that no
    silent ORM coercion (boolean to 0/1, string-to-int fail, etc.)
    would happen to land on.
    """
    sessionmaker = get_sessionmaker()
    convention_id = uuid.uuid4()

    async with sessionmaker() as session:
        session.add(
            TenantConvention(
                id=convention_id,
                tenant_id=uuid.uuid4(),
                slug="high-priority",
                title="High priority rule",
                body="Body.",
                kind="operational",
                priority=250,
            )
        )
        await session.commit()

    async with sessionmaker() as session:
        result = await session.execute(
            select(TenantConvention).where(TenantConvention.id == convention_id)
        )
        row = result.scalar_one()

    assert row.priority == 250


# ---------------------------------------------------------------------------
# (tenant_id, slug) unique constraint
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tenant_convention_tenant_slug_uniqueness_enforced() -> None:
    """Two rows with the same ``(tenant_id, slug)`` raise :class:`IntegrityError`.

    The migration enforces uniqueness via the named
    ``tenant_conventions_tenant_slug_idx`` (declared ``unique=True``);
    this test proves the constraint fires at the DB layer, not just at
    a future UI validator. Without it a regression that accidentally
    dropped the unique flag would silently allow two conventions with
    the same slug per tenant -- which would break T4's preamble
    assembly (duplicate slugs in the preamble) and T2's ``GET /{slug}``
    route (ambiguous row).
    """
    from sqlalchemy.exc import IntegrityError

    sessionmaker = get_sessionmaker()
    tenant_id = uuid.uuid4()

    async with sessionmaker() as session:
        session.add(
            TenantConvention(
                id=uuid.uuid4(),
                tenant_id=tenant_id,
                slug="dup-slug",
                title="First",
                body="Body.",
                kind="operational",
            )
        )
        await session.commit()

    async with sessionmaker() as session:
        session.add(
            TenantConvention(
                id=uuid.uuid4(),
                tenant_id=tenant_id,
                slug="dup-slug",
                title="Second",
                body="Body.",
                kind="workflow",
            )
        )
        with pytest.raises(IntegrityError):
            await session.commit()


@pytest.mark.asyncio
async def test_tenant_convention_same_slug_allowed_across_different_tenants() -> None:
    """Same slug in two different tenants does not raise.

    The uniqueness constraint is on ``(tenant_id, slug)``, not just
    ``slug``. Two tenants must be able to each have a ``rbac-canonical``
    convention; denying them would force awkward global namespacing and
    break the per-tenant authorship model the issue body specifies
    ("Cross-tenant: two tenants can have the same slug; no conflict").
    """
    sessionmaker = get_sessionmaker()
    tenant_a = uuid.uuid4()
    tenant_b = uuid.uuid4()

    async with sessionmaker() as session:
        session.add(
            TenantConvention(
                id=uuid.uuid4(),
                tenant_id=tenant_a,
                slug="rbac-canonical",
                title="Tenant A rule",
                body="Body A.",
                kind="operational",
            )
        )
        session.add(
            TenantConvention(
                id=uuid.uuid4(),
                tenant_id=tenant_b,
                slug="rbac-canonical",
                title="Tenant B rule",
                body="Body B.",
                kind="operational",
            )
        )
        # Must not raise -- different tenants, same slug is allowed.
        await session.commit()

    async with sessionmaker() as session:
        result = await session.execute(
            select(TenantConvention).where(TenantConvention.slug == "rbac-canonical")
        )
        rows = result.scalars().all()

    assert len(rows) == 2
    assert {r.tenant_id for r in rows} == {tenant_a, tenant_b}


# ---------------------------------------------------------------------------
# TenantConventionHistory round-trip
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tenant_convention_history_round_trip_persists_every_field() -> None:
    """Insert a :class:`TenantConventionHistory` row, query it back, fields match.

    Positive case for a PATCH-style history row: both ``body_before``
    (the prior state) and ``body_after`` (the new state) are populated.
    The ``audit_id`` soft-FK is set to a synthetic value to confirm the
    round-trip without requiring a real audit_log row.
    """
    sessionmaker = get_sessionmaker()
    history_id = uuid.uuid4()
    convention_id = uuid.uuid4()
    audit_id = uuid.uuid4()
    now = datetime.now(UTC)

    async with sessionmaker() as session:
        session.add(
            TenantConventionHistory(
                id=history_id,
                convention_id=convention_id,
                body_before="Old body.",
                body_after="New body.",
                actor_sub="user:bob",
                ts=now,
                audit_id=audit_id,
            )
        )
        await session.commit()

    async with sessionmaker() as session:
        result = await session.execute(
            select(TenantConventionHistory).where(TenantConventionHistory.id == history_id)
        )
        row = result.scalar_one()

    assert row.id == history_id
    assert row.convention_id == convention_id
    assert row.body_before == "Old body."
    assert row.body_after == "New body."
    assert row.actor_sub == "user:bob"
    assert row.audit_id == audit_id
    assert row.ts.replace(tzinfo=None) == now.replace(tzinfo=None)


@pytest.mark.asyncio
async def test_tenant_convention_history_create_event_has_null_body_before() -> None:
    """The CREATE event row's ``body_before`` round-trips as ``None``.

    Locks the contract that the first history row (CREATE) has no
    prior state. T2's POST route inserts a history row with
    ``body_before=NULL``; subsequent PATCHes shift the previous body
    into ``body_before``. A regression that made ``body_before`` NOT
    NULL would force T2 to invent a sentinel ("") which is a silent
    semantic shift.
    """
    sessionmaker = get_sessionmaker()
    history_id = uuid.uuid4()

    async with sessionmaker() as session:
        session.add(
            TenantConventionHistory(
                id=history_id,
                convention_id=uuid.uuid4(),
                body_before=None,
                body_after="Initial body.",
                actor_sub="user:carol",
                # audit_id deliberately omitted -- exercises the nullable default.
            )
        )
        await session.commit()

    async with sessionmaker() as session:
        result = await session.execute(
            select(TenantConventionHistory).where(TenantConventionHistory.id == history_id)
        )
        row = result.scalar_one()

    assert row.body_before is None
    assert row.body_after == "Initial body."
    assert row.audit_id is None


# ---------------------------------------------------------------------------
# Schema-level inspection -- migration installs the documented tables + indexes
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

    Mirrors :func:`tests.test_db_targets._alembic_upgrade_against_fresh_sqlite`
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


def test_migration_installs_conventions_tables_and_indexes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """``alembic upgrade head`` puts both tables + indexes in place.

    Asserts via SQLite's schema inspector (the dialect-portable
    equivalent of ``\\d+ tenant_conventions``):

    * The ``tenant_conventions`` table exists with all documented columns.
    * The ``tenant_convention_history`` table exists with all documented columns.
    * The unique composite ``tenant_conventions_tenant_slug_idx`` is present
      and marked unique.
    * The composite ``tenant_convention_history_convention_idx`` is present.
    * ``priority`` is NOT NULL on ``tenant_conventions``.

    PG-side verification (``\\d+`` against a real container) lives in
    the existing testcontainers suite that runs on CI.
    """
    sync_url, _ = _alembic_upgrade_against_fresh_sqlite(
        monkeypatch, tmp_path, "conventions_schema.db"
    )

    sync_eng = sa_create_engine(sync_url)
    try:
        with sync_eng.connect() as conn:
            inspector = sa_inspect(conn)
            tables = set(inspector.get_table_names())
            assert "tenant_conventions" in tables
            assert "tenant_convention_history" in tables

            # Column-set check for ``tenant_conventions``.
            conventions_cols = {col["name"] for col in inspector.get_columns("tenant_conventions")}
            expected_conventions_cols = {
                "id",
                "tenant_id",
                "slug",
                "title",
                "body",
                "kind",
                "priority",
                "created_by_sub",
                "created_at",
                "updated_at",
            }
            assert expected_conventions_cols <= conventions_cols, (
                f"Missing columns in tenant_conventions: "
                f"{expected_conventions_cols - conventions_cols}"
            )

            # ``priority`` must be NOT NULL (the v0.6 invariant T4 depends on).
            priority_col = next(
                col
                for col in inspector.get_columns("tenant_conventions")
                if col["name"] == "priority"
            )
            assert priority_col["nullable"] is False, (
                "tenant_conventions.priority must be NOT NULL "
                "so T4's preamble-packing has a well-defined ordering key"
            )

            # Column-set check for ``tenant_convention_history``.
            history_cols = {
                col["name"] for col in inspector.get_columns("tenant_convention_history")
            }
            expected_history_cols = {
                "id",
                "convention_id",
                "body_before",
                "body_after",
                "actor_sub",
                "ts",
                "audit_id",
            }
            assert expected_history_cols <= history_cols, (
                f"Missing columns in tenant_convention_history: "
                f"{expected_history_cols - history_cols}"
            )

            # ``body_before`` must be nullable (CREATE event has no prior state).
            body_before_col = next(
                col
                for col in inspector.get_columns("tenant_convention_history")
                if col["name"] == "body_before"
            )
            assert body_before_col["nullable"] is True, (
                "tenant_convention_history.body_before must be nullable "
                "so the CREATE event can record NULL"
            )

            # Index presence on ``tenant_conventions``.
            conventions_indexes = inspector.get_indexes("tenant_conventions")
            conventions_index_names = {idx["name"] for idx in conventions_indexes}
            assert "tenant_conventions_tenant_slug_idx" in conventions_index_names
            slug_index = next(
                idx
                for idx in conventions_indexes
                if idx["name"] == "tenant_conventions_tenant_slug_idx"
            )
            # SQLite returns ``1`` for unique flag; PG returns ``True``. Truthy
            # comparison covers both dialects -- mirrors the pattern in
            # :mod:`tests.test_db_models` / :mod:`tests.test_db_broadcast_override`.
            assert slug_index["unique"], (
                "tenant_conventions_tenant_slug_idx must enforce uniqueness on (tenant_id, slug)"
            )
            assert slug_index["column_names"] == ["tenant_id", "slug"]

            # Index presence on ``tenant_convention_history``.
            history_indexes = inspector.get_indexes("tenant_convention_history")
            history_index_names = {idx["name"] for idx in history_indexes}
            assert "tenant_convention_history_convention_idx" in history_index_names
            history_index = next(
                idx
                for idx in history_indexes
                if idx["name"] == "tenant_convention_history_convention_idx"
            )
            assert history_index["column_names"] == ["convention_id", "ts"]
    finally:
        sync_eng.dispose()


def test_migration_upgrade_then_downgrade_is_reversible(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """``alembic upgrade head`` -> ``alembic downgrade "0014"`` is a clean cycle.

    Proves that migration ``0015`` is fully reversible: after
    downgrading by exactly one revision (back to 0014, the
    ``agent_session_id`` column add), both new tables and their indexes
    must be gone while the rest of the schema (``tenant`` table,
    ``audit_log.agent_session_id``) remains intact. Re-upgrading to
    head must restore everything cleanly.

    The downgrade target is the explicit revision ``"0014"`` (0015's
    ``down_revision``) rather than head-relative ``"-1"``. ``"-1"``
    reverts whatever sits at head, so the moment a later migration
    (0016+) lands it would silently stop exercising 0015's reverse;
    anchoring to ``"0014"`` keeps this test pinned to 0015 and matches
    the repo convention (``test_migration_0014_agent_session_id``).
    """
    from alembic import command

    sync_url, cfg = _alembic_upgrade_against_fresh_sqlite(
        monkeypatch, tmp_path, "conventions_rev.db"
    )

    # Sanity -- upgrade landed both tables before we reverse.
    sync_eng = sa_create_engine(sync_url)
    try:
        with sync_eng.connect() as conn:
            tables_before = set(sa_inspect(conn).get_table_names())
        assert "tenant_conventions" in tables_before
        assert "tenant_convention_history" in tables_before
    finally:
        sync_eng.dispose()

    # Downgrade by exactly one revision (back to 0014).
    command.downgrade(cfg, "0014")

    sync_eng = sa_create_engine(sync_url)
    try:
        with sync_eng.connect() as conn:
            inspector = sa_inspect(conn)
            tables_after_down = set(inspector.get_table_names())
            # Both new tables must be gone.
            assert "tenant_conventions" not in tables_after_down
            assert "tenant_convention_history" not in tables_after_down
            # The rest of the schema must survive.
            assert "tenant" in tables_after_down
            assert "audit_log" in tables_after_down
            # 0014's column must still exist on audit_log.
            audit_cols = {col["name"] for col in inspector.get_columns("audit_log")}
            assert "agent_session_id" in audit_cols
    finally:
        sync_eng.dispose()

    # Re-upgrade -- both tables come back, proving the round-trip.
    command.upgrade(cfg, "head")

    sync_eng = sa_create_engine(sync_url)
    try:
        with sync_eng.connect() as conn:
            inspector = sa_inspect(conn)
            tables_after_up = set(inspector.get_table_names())
            assert "tenant_conventions" in tables_after_up
            assert "tenant_convention_history" in tables_after_up
            # Indexes restored too.
            conventions_indexes = {
                idx["name"] for idx in inspector.get_indexes("tenant_conventions")
            }
            assert "tenant_conventions_tenant_slug_idx" in conventions_indexes
            history_indexes = {
                idx["name"] for idx in inspector.get_indexes("tenant_convention_history")
            }
            assert "tenant_convention_history_convention_idx" in history_indexes
    finally:
        sync_eng.dispose()
