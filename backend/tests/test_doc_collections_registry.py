# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for the doc-collections registry (G4.6 T1 #1550).

Coverage matrix (Task #1550 acceptance criteria):

* Round-trip on :class:`DocCollection` — insert a valid row, query it
  back, every field survives the ORM round-trip including
  ``backend.{type,ref}``, ``products``, ``status``,
  ``last_ingested_at``, ``doc_count``, and the probe-written
  ``readiness`` JSON. Drives the ORM ``default=`` machinery (uuid,
  created_at, updated_at, status, extras) against the SQLite dev/test
  driver where the migration's PG server-side defaults are no-ops.
  ``backend`` is NOT NULL with no default — a row that omits it is
  rejected, not silently ``{}``-filled.
* Dual partial unique indexes — a global row (``tenant_id=NULL``) and a
  tenant-curated row with the same ``collection_key`` coexist; a second
  global row with the same key collides; a second tenant row with the
  same ``(tenant_id, collection_key)`` collides; the same key under two
  different tenants is allowed.
* ``resolve_doc_collection`` — returns the tenant row when present else
  the global row; an unknown key raises the typed
  :exc:`DocCollectionNotFoundError`.
* ``project_doc_collection_to_summary`` — the single ORM→wire
  projection produces the frozen summary with ``products`` coerced to a
  tuple and the backend record omitted.
* Schema-level smoke — ``alembic upgrade head`` creates the
  ``doc_collections`` table with its partial-unique indexes (GIN absent
  on SQLite), and ``upgrade head`` → ``downgrade -1`` is clean.

Runs against ``sqlite+aiosqlite`` via the shared engine cache the
autouse ``_default_database_url`` fixture in :mod:`tests.conftest`
pre-migrates to ``alembic upgrade head``. SQLite strips tzinfo, so
datetime assertions compare wall-clock parts only — identical to
:mod:`tests.test_db_targets`.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import create_engine as sa_create_engine
from sqlalchemy import select
from sqlalchemy import text as sa_text
from sqlalchemy.exc import IntegrityError

from meho_backplane.db.engine import get_sessionmaker, reset_engine_for_testing
from meho_backplane.db.models import DocCollection
from meho_backplane.docs_collections import (
    DocCollectionNotFoundError,
    DocCollectionSummary,
    project_doc_collection_to_summary,
    resolve_doc_collection,
)
from meho_backplane.settings import get_settings


@pytest.fixture(autouse=True)
def _required_settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin every env var :class:`Settings` requires for this module.

    Mirrors :mod:`tests.test_db_targets`: the autouse
    ``_default_database_url`` fixture only pins ``DATABASE_URL``;
    Keycloak/Vault knobs come from each test file. The
    ``get_settings.cache_clear()`` brackets prevent a stale ``Settings``
    instance from a previous test from leaking.
    """
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


# A valid ``{type, ref}`` routing record for tests where the backend value
# is incidental (uniqueness / resolution / CHECK coverage). ``backend`` is
# NOT NULL with no default, so every inserted row must supply one.
_BACKEND: dict[str, object] = {"type": "vertex-rag", "ref": "corpora/c"}


# ---------------------------------------------------------------------------
# DocCollection round-trip
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_doc_collection_round_trips_every_field() -> None:
    """Insert a fully-populated :class:`DocCollection`, read it back, all fields match.

    Exercises the operator-set fields (``collection_key`` / ``vendor`` /
    ``products`` / ``backend`` / ``when_to_use``) and the probe-written
    liveness fields (``status`` / ``last_ingested_at`` / ``doc_count`` /
    ``readiness``) round-tripping through the ORM against the SQLite
    driver where the migration's PG server-side defaults are no-ops.
    """
    sessionmaker = get_sessionmaker()
    collection_id = uuid.uuid4()
    tenant_id = uuid.uuid4()
    ingested = datetime.now(UTC)

    async with sessionmaker() as session:
        session.add(
            DocCollection(
                id=collection_id,
                tenant_id=tenant_id,
                collection_key="vmware",
                vendor="VMware by Broadcom",
                products=["vsphere", "nsx"],
                description="Complete VMware vendor doc set.",
                when_to_use="Use for vSphere / NSX / VCF product questions.",
                backend={"type": "vertex-rag", "ref": "projects/p/locations/l/corpora/c"},
                status="ready",
                last_ingested_at=ingested,
                doc_count=16942,
                readiness={"state": "ready", "checked_at": ingested.isoformat()},
                extras={"engagement": "acme"},
            )
        )
        await session.commit()

    async with sessionmaker() as session:
        result = await session.execute(
            select(DocCollection).where(DocCollection.id == collection_id)
        )
        row = result.scalar_one()

    assert row.id == collection_id
    assert row.tenant_id == tenant_id
    assert row.collection_key == "vmware"
    assert row.vendor == "VMware by Broadcom"
    assert row.products == ["vsphere", "nsx"]
    assert row.description == "Complete VMware vendor doc set."
    assert row.when_to_use == "Use for vSphere / NSX / VCF product questions."
    assert row.backend == {"type": "vertex-rag", "ref": "projects/p/locations/l/corpora/c"}
    assert row.status == "ready"
    assert row.doc_count == 16942
    assert row.readiness == {"state": "ready", "checked_at": ingested.isoformat()}
    assert row.extras == {"engagement": "acme"}
    # SQLite strips tzinfo — compare wall-clock parts only.
    assert row.last_ingested_at is not None
    assert row.last_ingested_at.replace(tzinfo=None) == ingested.replace(tzinfo=None)


@pytest.mark.asyncio
async def test_doc_collection_defaults_fire_when_optional_fields_omitted() -> None:
    """A minimal :class:`DocCollection` gets the ORM column defaults.

    ``tenant_id`` defaults to NULL (global), ``products`` to ``[]``,
    ``status`` to ``provisioning``, ``extras`` to ``{}``, and the
    probe-written liveness fields to NULL. ``backend`` is supplied
    explicitly: it is NOT NULL with no default (a routing record is
    required content, not an empty-meaningful escape hatch), so it is not
    part of the defaults that fire on omission.
    """
    sessionmaker = get_sessionmaker()
    collection_id = uuid.uuid4()

    async with sessionmaker() as session:
        session.add(
            DocCollection(
                id=collection_id,
                collection_key="minimal",
                vendor="Acme",
                backend={"type": "vertex-rag", "ref": "corpora/c"},
            )
        )
        await session.commit()

    async with sessionmaker() as session:
        result = await session.execute(
            select(DocCollection).where(DocCollection.id == collection_id)
        )
        row = result.scalar_one()

    assert row.tenant_id is None
    assert row.products == []
    assert row.description is None
    assert row.when_to_use is None
    assert row.backend == {"type": "vertex-rag", "ref": "corpora/c"}
    assert row.status == "provisioning"
    assert row.last_ingested_at is None
    assert row.doc_count is None
    assert row.readiness is None
    assert row.extras == {}


@pytest.mark.asyncio
async def test_doc_collection_backend_required_when_omitted() -> None:
    """Omitting ``backend`` is rejected — NOT NULL with no default.

    ``backend`` carries the ``{type, ref}`` routing record the T2 router
    (#1551) resolves server-side; an empty ``{}`` is a routing-broken
    row, so the column has no ORM ``default`` and no migration
    ``server_default``. A writer that omits it must hit the NOT NULL
    constraint rather than silently persist an empty object.
    """
    sessionmaker = get_sessionmaker()

    async with sessionmaker() as session:
        session.add(
            DocCollection(
                id=uuid.uuid4(),
                collection_key="no-backend",
                vendor="Acme",
            )
        )
        with pytest.raises(IntegrityError):
            await session.commit()


@pytest.mark.asyncio
async def test_doc_collection_status_check_rejects_unknown_value() -> None:
    """An out-of-enum ``status`` violates the DB-layer CHECK constraint."""
    sessionmaker = get_sessionmaker()

    async with sessionmaker() as session:
        session.add(
            DocCollection(
                id=uuid.uuid4(),
                collection_key="bad-status",
                vendor="Acme",
                backend=_BACKEND,
                status="archived",  # not in the CHECK IN(...) set
            )
        )
        with pytest.raises(IntegrityError):
            await session.commit()


# ---------------------------------------------------------------------------
# Dual partial unique indexes (global + tenant)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_global_and_tenant_row_same_key_coexist() -> None:
    """A global row and a tenant row with the same ``collection_key`` coexist.

    The dual partial unique indexes split uniqueness by
    ``tenant_id IS NULL`` / ``IS NOT NULL`` so a shared ``vmware`` and a
    tenant-curated ``vmware`` are not in conflict.
    """
    sessionmaker = get_sessionmaker()
    tenant_id = uuid.uuid4()

    async with sessionmaker() as session:
        session.add(
            DocCollection(
                id=uuid.uuid4(),
                collection_key="vmware",
                vendor="VMware (global)",
                backend=_BACKEND,
            )
        )
        session.add(
            DocCollection(
                id=uuid.uuid4(),
                tenant_id=tenant_id,
                collection_key="vmware",
                vendor="VMware (tenant)",
                backend=_BACKEND,
            )
        )
        # Must not raise — different uniqueness scopes.
        await session.commit()

    async with sessionmaker() as session:
        result = await session.execute(
            select(DocCollection).where(DocCollection.collection_key == "vmware")
        )
        rows = result.scalars().all()

    assert len(rows) == 2
    assert {r.tenant_id for r in rows} == {None, tenant_id}


@pytest.mark.asyncio
async def test_duplicate_global_key_rejected() -> None:
    """Two global rows with the same ``collection_key`` collide.

    The partial unique index ``WHERE tenant_id IS NULL`` enforces "one
    global row per key" — the property a plain ``UNIQUE (tenant_id,
    collection_key)`` would miss because NULL != NULL.
    """
    sessionmaker = get_sessionmaker()

    async with sessionmaker() as session:
        session.add(
            DocCollection(
                id=uuid.uuid4(),
                collection_key="dup-global",
                vendor="A",
                backend=_BACKEND,
            )
        )
        await session.commit()

    async with sessionmaker() as session:
        session.add(
            DocCollection(
                id=uuid.uuid4(),
                collection_key="dup-global",
                vendor="B",
                backend=_BACKEND,
            )
        )
        with pytest.raises(IntegrityError):
            await session.commit()


@pytest.mark.asyncio
async def test_duplicate_tenant_key_rejected() -> None:
    """Two rows with the same ``(tenant_id, collection_key)`` collide."""
    sessionmaker = get_sessionmaker()
    tenant_id = uuid.uuid4()

    async with sessionmaker() as session:
        session.add(
            DocCollection(
                id=uuid.uuid4(),
                tenant_id=tenant_id,
                collection_key="dup-tenant",
                vendor="A",
                backend=_BACKEND,
            )
        )
        await session.commit()

    async with sessionmaker() as session:
        session.add(
            DocCollection(
                id=uuid.uuid4(),
                tenant_id=tenant_id,
                collection_key="dup-tenant",
                vendor="B",
                backend=_BACKEND,
            )
        )
        with pytest.raises(IntegrityError):
            await session.commit()


@pytest.mark.asyncio
async def test_same_key_allowed_across_tenants() -> None:
    """The same ``collection_key`` under two different tenants is allowed."""
    sessionmaker = get_sessionmaker()
    tenant_a = uuid.uuid4()
    tenant_b = uuid.uuid4()

    async with sessionmaker() as session:
        session.add(
            DocCollection(
                id=uuid.uuid4(),
                tenant_id=tenant_a,
                collection_key="shared",
                vendor="A",
                backend=_BACKEND,
            )
        )
        session.add(
            DocCollection(
                id=uuid.uuid4(),
                tenant_id=tenant_b,
                collection_key="shared",
                vendor="B",
                backend=_BACKEND,
            )
        )
        await session.commit()

    async with sessionmaker() as session:
        result = await session.execute(
            select(DocCollection).where(DocCollection.collection_key == "shared")
        )
        rows = result.scalars().all()

    assert {r.tenant_id for r in rows} == {tenant_a, tenant_b}


# ---------------------------------------------------------------------------
# resolve_doc_collection
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolve_prefers_tenant_row_over_global() -> None:
    """When both a tenant and a global row exist, the tenant row wins."""
    sessionmaker = get_sessionmaker()
    tenant_id = uuid.uuid4()

    async with sessionmaker() as session:
        session.add(
            DocCollection(
                id=uuid.uuid4(),
                collection_key="vmware",
                vendor="VMware (global)",
                backend=_BACKEND,
            )
        )
        session.add(
            DocCollection(
                id=uuid.uuid4(),
                tenant_id=tenant_id,
                collection_key="vmware",
                vendor="VMware (tenant)",
                backend=_BACKEND,
            )
        )
        await session.commit()

    async with sessionmaker() as session:
        resolved = await resolve_doc_collection(session, "vmware", tenant_id)

    assert resolved.tenant_id == tenant_id
    assert resolved.vendor == "VMware (tenant)"


@pytest.mark.asyncio
async def test_resolve_falls_back_to_global_when_no_tenant_row() -> None:
    """With only a global row present, the resolver returns it."""
    sessionmaker = get_sessionmaker()
    tenant_id = uuid.uuid4()

    async with sessionmaker() as session:
        session.add(
            DocCollection(
                id=uuid.uuid4(),
                collection_key="vmware",
                vendor="VMware (global)",
                backend=_BACKEND,
            )
        )
        await session.commit()

    async with sessionmaker() as session:
        resolved = await resolve_doc_collection(session, "vmware", tenant_id)

    assert resolved.tenant_id is None
    assert resolved.vendor == "VMware (global)"


@pytest.mark.asyncio
async def test_resolve_ignores_other_tenants_row() -> None:
    """A row curated by another tenant is invisible (no cross-tenant leak)."""
    sessionmaker = get_sessionmaker()
    tenant_a = uuid.uuid4()
    tenant_b = uuid.uuid4()

    async with sessionmaker() as session:
        session.add(
            DocCollection(
                id=uuid.uuid4(),
                tenant_id=tenant_b,
                collection_key="private",
                vendor="B-only",
                backend=_BACKEND,
            )
        )
        await session.commit()

    async with sessionmaker() as session:
        with pytest.raises(DocCollectionNotFoundError):
            await resolve_doc_collection(session, "private", tenant_a)


@pytest.mark.asyncio
async def test_resolve_unknown_key_raises_typed_not_found() -> None:
    """An unknown key raises the typed not-found carrying the known keys."""
    sessionmaker = get_sessionmaker()
    tenant_id = uuid.uuid4()

    async with sessionmaker() as session:
        session.add(
            DocCollection(
                id=uuid.uuid4(),
                collection_key="vmware",
                vendor="VMware",
                backend=_BACKEND,
            )
        )
        await session.commit()

    async with sessionmaker() as session:
        with pytest.raises(DocCollectionNotFoundError) as exc_info:
            await resolve_doc_collection(session, "nonesuch", tenant_id)

    assert exc_info.value.status_code == 404
    detail = exc_info.value.detail
    assert isinstance(detail, dict)
    assert detail["error"] == "no_doc_collection"
    assert detail["collection_key"] == "nonesuch"
    assert detail["known_keys"] == ["vmware"]


# ---------------------------------------------------------------------------
# project_doc_collection_to_summary
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_projection_produces_frozen_summary_without_backend() -> None:
    """The single ORM→wire projection yields a frozen summary, backend omitted."""
    sessionmaker = get_sessionmaker()
    collection_id = uuid.uuid4()
    tenant_id = uuid.uuid4()

    async with sessionmaker() as session:
        session.add(
            DocCollection(
                id=collection_id,
                tenant_id=tenant_id,
                collection_key="vmware",
                vendor="VMware",
                products=["vsphere", "nsx"],
                when_to_use="vSphere questions.",
                backend={"type": "vertex-rag", "ref": "corpora/c"},
                status="ready",
                doc_count=100,
            )
        )
        await session.commit()

    async with sessionmaker() as session:
        result = await session.execute(
            select(DocCollection).where(DocCollection.id == collection_id)
        )
        row = result.scalar_one()
        summary = project_doc_collection_to_summary(row)

    assert isinstance(summary, DocCollectionSummary)
    assert summary.collection_key == "vmware"
    assert summary.vendor == "VMware"
    # products coerced from list to tuple for the frozen model.
    assert summary.products == ("vsphere", "nsx")
    assert summary.when_to_use == "vSphere questions."
    assert summary.status == "ready"
    assert summary.doc_count == 100
    # The backend record is server-side-only and must not appear on the
    # catalogue summary shape (#1548 backend-agnostic contract).
    assert not hasattr(summary, "backend")
    # Frozen — mutation attempts raise.
    with pytest.raises(Exception):  # noqa: B017 — pydantic ValidationError on frozen set
        summary.collection_key = "other"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Migration smoke — upgrade head + downgrade -1
# ---------------------------------------------------------------------------


def _alembic_cfg(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> tuple[object, str]:
    """Pin env, reset caches, return an Alembic config + sync URL.

    Mirrors :func:`tests.test_migration_0025_scheduled_trigger.alembic_cfg`:
    sync URL because Alembic's env.py runs ``asyncio.run`` internally;
    an isolated SQLite DB per call.
    """
    from meho_backplane.db.migrations import alembic_config

    db_path = tmp_path / "migration_0037.db"
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
    return cfg, sync_url


def _index_names(sync_url: str, table: str) -> set[str]:
    """Return the set of index names on *table* (SQLite schema inspector)."""
    sync_eng = sa_create_engine(sync_url)
    try:
        with sync_eng.connect() as conn:
            rows = conn.execute(sa_text(f"PRAGMA index_list({table})")).all()
    finally:
        sync_eng.dispose()
    return {str(row[1]) for row in rows}


def _table_names(sync_url: str) -> set[str]:
    """Return the set of table names in the SQLite schema."""
    sync_eng = sa_create_engine(sync_url)
    try:
        with sync_eng.connect() as conn:
            rows = conn.execute(
                sa_text("SELECT name FROM sqlite_master WHERE type = 'table'")
            ).all()
    finally:
        sync_eng.dispose()
    return {str(row[0]) for row in rows}


def test_migration_0037_creates_table_and_partial_indexes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """``alembic upgrade head`` installs ``doc_collections`` + its indexes.

    The two partial-unique indexes land; the PG-only GIN index is absent
    on SQLite (the ``if is_postgres:`` guard fired).
    """
    from alembic import command

    cfg, sync_url = _alembic_cfg(monkeypatch, tmp_path)
    try:
        command.upgrade(cfg, "head")

        assert "doc_collections" in _table_names(sync_url)
        indexes = _index_names(sync_url, "doc_collections")
        assert "doc_collections_global_idx" in indexes
        assert "doc_collections_tenant_idx" in indexes
        # GIN index is PG-only — must NOT be present on SQLite.
        assert "doc_collections_products_gin_idx" not in indexes
    finally:
        get_settings.cache_clear()
        reset_engine_for_testing()


def test_migration_0037_downgrade_drops_table(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """``upgrade head`` → ``downgrade -1`` is clean: the table is gone."""
    from alembic import command

    cfg, sync_url = _alembic_cfg(monkeypatch, tmp_path)
    try:
        command.upgrade(cfg, "head")
        assert "doc_collections" in _table_names(sync_url)

        command.downgrade(cfg, "-1")
        assert "doc_collections" not in _table_names(sync_url)
    finally:
        get_settings.cache_clear()
        reset_engine_for_testing()
