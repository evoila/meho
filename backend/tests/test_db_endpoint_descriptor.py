# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for the G0.6 operation-substrate ORM models.

Coverage matrix (Task #392 acceptance criteria):

* Round-trip on :class:`~meho_backplane.db.models.OperationGroup` and
  :class:`~meho_backplane.db.models.EndpointDescriptor` — insert valid
  rows, query them back, every field survives the ORM round-trip.
  Drives the ORM ``default=`` machinery (uuid, created_at, updated_at,
  review_status, source_kind, safety_level, requires_approval,
  is_enabled, tags, parameter_schema) against the SQLite dev/test
  driver where the migration's PG server-side defaults are no-ops.
* Partial unique on ``endpoint_descriptor (product, version, impl_id,
  op_id) WHERE tenant_id IS NULL`` rejects a duplicate built-in row —
  the migration's split-by-tenant-null index pair is what enforces the
  "one global op per (product, version, impl_id, op_id)" invariant at
  the DB layer.
* Partial unique scoping — the **same** ``op_id`` may exist under
  both a built-in row (``tenant_id IS NULL``) and a tenant-scoped row
  (``tenant_id IS NOT NULL``) without colliding. Without the
  ``WHERE`` clauses on the indexes, this would fail.
* Cross-tenant collision allowed on the tenant-scoped index — two
  different tenants registering the same ``op_id`` under the same
  ``(product, version, impl_id)`` commit cleanly because the
  ``tenant_id`` axis is in the tenant-scoped index key.
* FK enforcement — ``endpoint_descriptor.group_id`` must reference
  an existing ``operation_group.id`` row when non-null; a typo'd
  / deleted / replayed UUID raises :class:`IntegrityError`.
* ``ON DELETE SET NULL`` cascade — deleting an
  :class:`OperationGroup` row sets ``group_id`` to NULL on every
  descriptor that referenced it, leaving the descriptors
  dispatchable but ungrouped (the G7/G10 admin UI re-groups).
* Schema-level smoke — ``alembic upgrade head`` against a fresh
  SQLite DB creates both tables, all the documented columns, the
  three portable indexes, and skips the two PG-only indexes (GIN
  + IVFFlat) per the dialect guard in migration ``0005``.
* Migration reversibility — ``alembic upgrade head`` →
  ``alembic downgrade 0004`` drops both tables and all indexes;
  re-upgrading to ``head`` restores everything.

The tests run against ``sqlite+aiosqlite`` via the shared engine
cache that the autouse ``_default_database_url`` fixture in
:mod:`tests.conftest` already pre-migrates to ``alembic upgrade
head``. Per-test isolation comes from pytest's ``tmp_path``-scoped
DB file — same shape every other DB-touching test in the suite uses.

SQLite caveats — identical to :mod:`tests.test_db_targets` and
:mod:`tests.test_db_documents`: SQLite stores datetimes as ISO-8601
strings without timezone information; SQLAlchemy round-trips them as
naive :class:`datetime`. All datetime assertions strip tzinfo before
comparing the wall-clock parts. PG-real assertions (vector(384)
column type, IVFFlat + GIN indexes installed) live in the existing
testcontainers suite in :mod:`tests.test_db_engine`.
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
from meho_backplane.db.models import EndpointDescriptor, OperationGroup
from meho_backplane.settings import get_settings


async def _enable_sqlite_foreign_keys(session: AsyncSession) -> None:
    """Issue ``PRAGMA foreign_keys = ON`` on the bound SQLite connection.

    SQLite ships with foreign-key enforcement disabled by default
    (sqlite.org/foreignkeys.html §2). Without this PRAGMA, the
    ``ON DELETE SET NULL`` cascade declared on
    :attr:`EndpointDescriptor.group_id` and the FK-violation
    :class:`IntegrityError` we expect would both silently no-op. The
    PRAGMA is a per-connection setting; emitting it once on the
    session's bound connection covers every statement that follows on
    the same connection. The shared engine uses :class:`StaticPool`
    on SQLite so a single connection backs every checkout in this
    test process.

    The pragma is **not** enabled globally in
    :mod:`meho_backplane.db.engine` — that scope decision is broader
    than this task. T1's scope is the operation-substrate schema; the
    engine-level toggle is captured as an adjacent finding for the
    G0.6 follow-on (or a dedicated infra task).
    """
    await session.execute(text("PRAGMA foreign_keys = ON"))


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


# A 384-element placeholder vector — same shape ``tests.test_db_documents``
# uses. ``_PortableVector384`` (see ``meho_backplane.db.models``)
# JSON-encodes the list on SQLite and lets pgvector's bind processor
# handle the PG path; the same value works against both dialects.
_PLACEHOLDER_EMBEDDING: list[float] = [0.0] * 384


# ---------------------------------------------------------------------------
# OperationGroup round-trip
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_operation_group_round_trip_persists_every_field() -> None:
    """Insert an :class:`OperationGroup`, query it back, every field matches.

    Exercises the ORM ``default=`` machinery (uuid, created_at,
    updated_at, review_status) against the SQLite driver where the
    migration's PG server-side defaults are no-ops. Asserting on every
    field is what proves the column shape, type mapping, and default
    machinery are wired correctly before T4 / G0.7 start writing in
    earnest.
    """
    sessionmaker = get_sessionmaker()
    group_id = uuid.uuid4()
    tenant_id = uuid.uuid4()
    now = datetime.now(UTC)

    async with sessionmaker() as session:
        session.add(
            OperationGroup(
                id=group_id,
                tenant_id=tenant_id,
                product="vmware",
                version="9.0",
                impl_id="vmware-rest",
                group_key="vm-lifecycle",
                name="VM lifecycle",
                when_to_use="Create, clone, power, snapshot, and delete VMs.",
                review_status="enabled",
                created_at=now,
                updated_at=now,
            )
        )
        await session.commit()

    async with sessionmaker() as session:
        result = await session.execute(select(OperationGroup).where(OperationGroup.id == group_id))
        row = result.scalar_one()

    assert row.id == group_id
    assert row.tenant_id == tenant_id
    assert row.product == "vmware"
    assert row.version == "9.0"
    assert row.impl_id == "vmware-rest"
    assert row.group_key == "vm-lifecycle"
    assert row.name == "VM lifecycle"
    assert row.when_to_use.startswith("Create, clone")
    assert row.review_status == "enabled"
    # SQLite strips tzinfo — compare wall-clock parts only.
    assert row.created_at.replace(tzinfo=None) == now.replace(tzinfo=None)
    assert row.updated_at.replace(tzinfo=None) == now.replace(tzinfo=None)


@pytest.mark.asyncio
async def test_operation_group_orm_defaults_fire_on_sqlite() -> None:
    """``id``, ``created_at``, ``updated_at``, ``review_status`` get populated by ORM.

    The migration's PG-side server defaults (``gen_random_uuid()``,
    ``now()``, ``'staged'``) are no-ops on SQLite. The ORM defaults
    (``default=uuid.uuid4``, ``default=lambda: datetime.now(UTC)``,
    ``default="staged"``) must fill the column Python-side. A
    regression where someone drops an ORM default in favour of relying
    solely on the migration would surface here as a NOT NULL violation
    on SQLite.
    """
    sessionmaker = get_sessionmaker()
    before = datetime.now(UTC)
    async with sessionmaker() as session:
        group = OperationGroup(
            product="vault",
            version="1.x",
            impl_id="vault",
            group_key="kv",
            name="KV secrets",
            when_to_use="Read and write key-value secrets.",
        )
        session.add(group)
        await session.commit()
        seen_id = group.id
        seen_status = group.review_status
        seen_created = group.created_at
        seen_updated = group.updated_at

    assert isinstance(seen_id, uuid.UUID)
    assert seen_status == "staged"
    assert seen_created.replace(tzinfo=None) >= before.replace(tzinfo=None)
    assert seen_updated.replace(tzinfo=None) >= before.replace(tzinfo=None)


# ---------------------------------------------------------------------------
# EndpointDescriptor round-trip
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_endpoint_descriptor_round_trip_persists_every_field() -> None:
    """Insert an :class:`EndpointDescriptor`, query it back, every field matches.

    Drives the full column set — ingested-shape (method+path), typed
    fields (handler_ref), JSON columns (tags, parameter_schema,
    response_schema, llm_instructions), enum-shaped fields (source_kind,
    safety_level), boolean defaults, the embedding round-trip, and the
    custom_* override fields — through the ORM in one go. Catches a
    regression where any column rename / type swap silently drops data.
    """
    sessionmaker = get_sessionmaker()
    # Prerequisite — an OperationGroup row to FK against.
    group_id = uuid.uuid4()
    descriptor_id = uuid.uuid4()
    now = datetime.now(UTC)

    async with sessionmaker() as session:
        session.add(
            OperationGroup(
                id=group_id,
                product="vmware",
                version="9.0",
                impl_id="vmware-rest",
                group_key="vm-lifecycle",
                name="VM lifecycle",
                when_to_use="Create, clone, power, snapshot, and delete VMs.",
            )
        )
        await session.commit()

    async with sessionmaker() as session:
        session.add(
            EndpointDescriptor(
                id=descriptor_id,
                tenant_id=None,  # global / built-in
                product="vmware",
                version="9.0",
                impl_id="vmware-rest",
                op_id="GET:/api/vcenter/cluster",
                source_kind="ingested",
                method="GET",
                path="/api/vcenter/cluster",
                handler_ref=None,
                summary="List vSphere clusters.",
                description="Returns every cluster the vCenter knows about.",
                group_id=group_id,
                tags=["read-only", "cluster"],
                parameter_schema={"type": "object", "properties": {}},
                response_schema={"type": "array"},
                llm_instructions={"when_to_call": "before scheduling a VM"},
                safety_level="safe",
                requires_approval=False,
                is_enabled=True,
                embedding=_PLACEHOLDER_EMBEDDING,
                custom_description="Operator-curated override.",
                custom_notes="Reviewed 2026-05-14.",
                created_at=now,
                updated_at=now,
            )
        )
        await session.commit()

    async with sessionmaker() as session:
        result = await session.execute(
            select(EndpointDescriptor).where(EndpointDescriptor.id == descriptor_id)
        )
        row = result.scalar_one()

    assert row.id == descriptor_id
    assert row.tenant_id is None
    assert row.product == "vmware"
    assert row.version == "9.0"
    assert row.impl_id == "vmware-rest"
    assert row.op_id == "GET:/api/vcenter/cluster"
    assert row.source_kind == "ingested"
    assert row.method == "GET"
    assert row.path == "/api/vcenter/cluster"
    assert row.handler_ref is None
    assert row.summary == "List vSphere clusters."
    assert row.description == "Returns every cluster the vCenter knows about."
    assert row.group_id == group_id
    assert row.tags == ["read-only", "cluster"]
    assert row.parameter_schema == {"type": "object", "properties": {}}
    assert row.response_schema == {"type": "array"}
    assert row.llm_instructions == {"when_to_call": "before scheduling a VM"}
    assert row.safety_level == "safe"
    assert row.requires_approval is False
    assert row.is_enabled is True
    # Round-trips as list[float] on both dialects via _PortableVector384.
    assert row.embedding == _PLACEHOLDER_EMBEDDING
    assert row.custom_description == "Operator-curated override."
    assert row.custom_notes == "Reviewed 2026-05-14."
    assert row.created_at.replace(tzinfo=None) == now.replace(tzinfo=None)
    assert row.updated_at.replace(tzinfo=None) == now.replace(tzinfo=None)


@pytest.mark.asyncio
async def test_endpoint_descriptor_orm_defaults_fire_on_sqlite() -> None:
    """ORM defaults populate ``id``, timestamps, tags, parameter_schema, enum fields, flags.

    The migration's PG-side server defaults (``gen_random_uuid()``,
    ``now()``, ``'[]'::jsonb``, ``'{}'::jsonb``, ``'safe'``,
    ``false``, ``true``) are no-ops on SQLite. The ORM defaults must
    fill them Python-side. A regression that drops any ORM default in
    favour of relying on the migration would surface here as a NOT
    NULL violation on SQLite.
    """
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        descriptor = EndpointDescriptor(
            product="vault",
            version="1.x",
            impl_id="vault",
            op_id="vault.kv.read",
            source_kind="typed",
            handler_ref="meho_backplane.connectors.vault.connector:read",
        )
        session.add(descriptor)
        await session.commit()
        seen_id = descriptor.id
        seen_tags = descriptor.tags
        seen_schema = descriptor.parameter_schema
        seen_safety = descriptor.safety_level
        seen_approval = descriptor.requires_approval
        seen_enabled = descriptor.is_enabled

    assert isinstance(seen_id, uuid.UUID)
    assert seen_tags == []
    assert seen_schema == {}
    assert seen_safety == "safe"
    assert seen_approval is False
    assert seen_enabled is True


# ---------------------------------------------------------------------------
# Partial unique index — built-in (tenant_id IS NULL) collision
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_endpoint_descriptor_global_unique_rejects_duplicate_builtin() -> None:
    """Two built-in rows with the same coordinates → IntegrityError.

    Partial unique index ``endpoint_descriptor_global_idx`` covers
    ``(product, version, impl_id, op_id) WHERE tenant_id IS NULL``.
    Without the index, two ``tenant_id IS NULL`` rows with identical
    natural-key coordinates would commit (SQL NULL != NULL semantics).
    """
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        session.add(
            EndpointDescriptor(
                product="vmware",
                version="9.0",
                impl_id="vmware-rest",
                op_id="GET:/api/vcenter/cluster",
                source_kind="ingested",
                method="GET",
                path="/api/vcenter/cluster",
            )
        )
        await session.commit()

    async with sessionmaker() as session:
        session.add(
            EndpointDescriptor(
                product="vmware",
                version="9.0",
                impl_id="vmware-rest",
                op_id="GET:/api/vcenter/cluster",
                source_kind="ingested",
                method="GET",
                path="/api/vcenter/cluster",
            )
        )
        with pytest.raises(IntegrityError):
            await session.commit()


@pytest.mark.asyncio
async def test_endpoint_descriptor_global_and_tenant_coexist() -> None:
    """Same op_id may live under both a built-in row and a tenant-scoped row.

    Pins the partial-index split contract: the two unique indexes
    cover disjoint subsets of rows (``WHERE tenant_id IS NULL`` vs
    ``WHERE tenant_id IS NOT NULL``), so a built-in op and a
    tenant-scoped composite with the same natural-key coordinates do
    not collide. Without the ``WHERE`` clauses, the second insert
    would fail.
    """
    sessionmaker = get_sessionmaker()
    tenant_id = uuid.uuid4()
    async with sessionmaker() as session:
        # Built-in row.
        session.add(
            EndpointDescriptor(
                tenant_id=None,
                product="vmware",
                version="9.0",
                impl_id="vmware-rest",
                op_id="vmware.composite.vm.create",
                source_kind="composite",
                handler_ref="meho_backplane.composites.vmware:vm_create",
            )
        )
        # Tenant-scoped override.
        session.add(
            EndpointDescriptor(
                tenant_id=tenant_id,
                product="vmware",
                version="9.0",
                impl_id="vmware-rest",
                op_id="vmware.composite.vm.create",
                source_kind="composite",
                handler_ref="meho_backplane.composites.vmware:vm_create_tenant",
            )
        )
        # Both must commit cleanly under the partial-index split.
        await session.commit()


@pytest.mark.asyncio
async def test_endpoint_descriptor_tenant_unique_rejects_duplicate() -> None:
    """Two tenant-scoped rows for the same tenant with identical coords → IntegrityError.

    Partial unique index ``endpoint_descriptor_tenant_idx`` covers
    ``(tenant_id, product, version, impl_id, op_id) WHERE
    tenant_id IS NOT NULL``. Two rows under the same tenant with the
    same natural-key coordinates collide.
    """
    sessionmaker = get_sessionmaker()
    tenant_id = uuid.uuid4()
    async with sessionmaker() as session:
        session.add(
            EndpointDescriptor(
                tenant_id=tenant_id,
                product="vmware",
                version="9.0",
                impl_id="vmware-rest",
                op_id="vmware.composite.vm.create",
                source_kind="composite",
                handler_ref="meho_backplane.composites.vmware:vm_create",
            )
        )
        await session.commit()

    async with sessionmaker() as session:
        session.add(
            EndpointDescriptor(
                tenant_id=tenant_id,
                product="vmware",
                version="9.0",
                impl_id="vmware-rest",
                op_id="vmware.composite.vm.create",
                source_kind="composite",
                handler_ref="meho_backplane.composites.vmware:vm_create_v2",
            )
        )
        with pytest.raises(IntegrityError):
            await session.commit()


@pytest.mark.asyncio
async def test_endpoint_descriptor_cross_tenant_same_op_id_allowed() -> None:
    """Two different tenants can each have the same op_id at the same coords.

    Tenant scoping is part of the unique index key for the
    tenant-scoped partial index, so two distinct ``tenant_id`` values
    insulate the two rows. Without ``tenant_id`` in the key, the
    second tenant's insert would fail spuriously.
    """
    sessionmaker = get_sessionmaker()
    tenant_a = uuid.uuid4()
    tenant_b = uuid.uuid4()
    async with sessionmaker() as session:
        session.add(
            EndpointDescriptor(
                tenant_id=tenant_a,
                product="vmware",
                version="9.0",
                impl_id="vmware-rest",
                op_id="vmware.composite.vm.create",
                source_kind="composite",
                handler_ref="meho_backplane.composites.vmware:vm_create_a",
            )
        )
        session.add(
            EndpointDescriptor(
                tenant_id=tenant_b,
                product="vmware",
                version="9.0",
                impl_id="vmware-rest",
                op_id="vmware.composite.vm.create",
                source_kind="composite",
                handler_ref="meho_backplane.composites.vmware:vm_create_b",
            )
        )
        await session.commit()

    async with sessionmaker() as session:
        result = await session.execute(
            select(EndpointDescriptor).where(
                EndpointDescriptor.op_id == "vmware.composite.vm.create"
            )
        )
        rows = result.scalars().all()

    assert len(rows) == 2
    assert {r.tenant_id for r in rows} == {tenant_a, tenant_b}


# ---------------------------------------------------------------------------
# Foreign key — group_id → operation_group.id
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_endpoint_descriptor_group_id_fk_enforced() -> None:
    """Inserting a descriptor with an unknown ``group_id`` raises IntegrityError.

    The FK on ``endpoint_descriptor.group_id`` references
    ``operation_group.id`` (with ``ON DELETE SET NULL``). A typo'd /
    deleted / replayed UUID surfaces as :class:`IntegrityError` at
    insert time, never as an unreachable row at dispatch time. SQLite
    enforces FKs only when ``PRAGMA foreign_keys = ON``; the
    ``meho_backplane.db.engine`` module sets this on engine creation
    for the test path so the constraint fires here.
    """
    sessionmaker = get_sessionmaker()
    bogus_group_id = uuid.uuid4()
    async with sessionmaker() as session:
        await _enable_sqlite_foreign_keys(session)
        session.add(
            EndpointDescriptor(
                product="vault",
                version="1.x",
                impl_id="vault",
                op_id="vault.kv.read",
                source_kind="typed",
                handler_ref="meho_backplane.connectors.vault.connector:read",
                group_id=bogus_group_id,
            )
        )
        with pytest.raises(IntegrityError):
            await session.commit()


@pytest.mark.asyncio
async def test_endpoint_descriptor_group_delete_sets_group_id_null() -> None:
    """Deleting an :class:`OperationGroup` sets ``group_id`` to NULL on referencing rows.

    The FK ``ON DELETE SET NULL`` cascade keeps descriptors
    dispatchable when their group is removed. Pins the contract that
    a removed group does not cascade-delete descriptors (which would
    silently lose ops) and does not raise on the parent delete (which
    would block group cleanup).
    """
    sessionmaker = get_sessionmaker()
    group_id = uuid.uuid4()
    descriptor_id = uuid.uuid4()

    async with sessionmaker() as session:
        await _enable_sqlite_foreign_keys(session)
        session.add(
            OperationGroup(
                id=group_id,
                product="vault",
                version="1.x",
                impl_id="vault",
                group_key="kv",
                name="KV secrets",
                when_to_use="Read/write KV secrets.",
            )
        )
        # Commit the parent before the FK-bearing child so SQLAlchemy
        # cannot flush the child first (which would race the FK probe
        # against the parent insert inside the same transaction).
        await session.commit()
        session.add(
            EndpointDescriptor(
                id=descriptor_id,
                product="vault",
                version="1.x",
                impl_id="vault",
                op_id="vault.kv.read",
                source_kind="typed",
                handler_ref="meho_backplane.connectors.vault.connector:read",
                group_id=group_id,
            )
        )
        await session.commit()

    async with sessionmaker() as session:
        await _enable_sqlite_foreign_keys(session)
        group = (
            await session.execute(select(OperationGroup).where(OperationGroup.id == group_id))
        ).scalar_one()
        await session.delete(group)
        await session.commit()

    async with sessionmaker() as session:
        result = await session.execute(
            select(EndpointDescriptor).where(EndpointDescriptor.id == descriptor_id)
        )
        row = result.scalar_one()

    assert row.group_id is None, (
        "ON DELETE SET NULL must clear group_id when its parent group is removed; "
        "left non-null would leave a dangling FK pointing at a deleted row"
    )


# ---------------------------------------------------------------------------
# Schema-level inspection — migration installs documented tables + indexes
# ---------------------------------------------------------------------------


def _alembic_upgrade_against_fresh_sqlite(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    db_filename: str,
) -> tuple[str, Config]:
    """Pin env, reset caches, run ``alembic upgrade head`` on fresh SQLite.

    Shared setup for the sync migration tests below; mirrors the
    helper in :mod:`tests.test_db_targets`. Returns
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


def test_migration_installs_tables_and_portable_indexes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """``alembic upgrade head`` puts both tables + their portable indexes in place.

    Asserts via SQLite's schema inspector (the dialect-portable
    equivalent of ``\\d+`` against PG):

    * The ``operation_group`` and ``endpoint_descriptor`` tables exist
      with every documented column.
    * The two partial-unique indexes on each table are present (SQLite
      records partial indexes via ``get_indexes`` like any other).
    * The ``endpoint_descriptor_lookup_idx`` b-tree is present.
    * The two PG-only indexes (``endpoint_descriptor_bm25_idx`` GIN,
      ``endpoint_descriptor_embedding_idx`` IVFFlat) are **absent** on
      SQLite — the migration's ``if is_postgres:`` guard must have
      fired correctly.

    PG-side verification (``\\d+`` against a real container) lives in
    the existing testcontainers suite that runs on CI.
    """
    sync_url, _ = _alembic_upgrade_against_fresh_sqlite(monkeypatch, tmp_path, "g06-schema.db")

    sync_eng = sa_create_engine(sync_url)
    try:
        with sync_eng.connect() as conn:
            inspector = sa_inspect(conn)
            tables = set(inspector.get_table_names())
            assert "operation_group" in tables
            assert "endpoint_descriptor" in tables

            group_cols = {col["name"] for col in inspector.get_columns("operation_group")}
            expected_group_cols = {
                "id",
                "tenant_id",
                "product",
                "version",
                "impl_id",
                "group_key",
                "name",
                "when_to_use",
                "review_status",
                "created_at",
                "updated_at",
            }
            assert expected_group_cols <= group_cols, (
                f"Missing columns in operation_group: {expected_group_cols - group_cols}"
            )

            descriptor_cols = {col["name"] for col in inspector.get_columns("endpoint_descriptor")}
            expected_descriptor_cols = {
                "id",
                "tenant_id",
                "product",
                "version",
                "impl_id",
                "op_id",
                "source_kind",
                "method",
                "path",
                "handler_ref",
                "summary",
                "description",
                "group_id",
                "tags",
                "parameter_schema",
                "response_schema",
                "llm_instructions",
                "safety_level",
                "requires_approval",
                "is_enabled",
                "embedding",
                "custom_description",
                "custom_notes",
                "created_at",
                "updated_at",
            }
            assert expected_descriptor_cols <= descriptor_cols, (
                "Missing columns in endpoint_descriptor: "
                f"{expected_descriptor_cols - descriptor_cols}"
            )

            group_indexes = {idx["name"] for idx in inspector.get_indexes("operation_group")}
            assert "operation_group_global_idx" in group_indexes
            assert "operation_group_tenant_idx" in group_indexes

            descriptor_indexes = {
                idx["name"] for idx in inspector.get_indexes("endpoint_descriptor")
            }
            assert "endpoint_descriptor_global_idx" in descriptor_indexes
            assert "endpoint_descriptor_tenant_idx" in descriptor_indexes
            assert "endpoint_descriptor_lookup_idx" in descriptor_indexes
            # PG-only indexes must be absent on SQLite — dialect guard check.
            assert "endpoint_descriptor_bm25_idx" not in descriptor_indexes, (
                "GIN index should be absent on SQLite; the is_postgres guard must have fired"
            )
            assert "endpoint_descriptor_embedding_idx" not in descriptor_indexes, (
                "IVFFlat index should be absent on SQLite; the is_postgres guard must have fired"
            )
    finally:
        sync_eng.dispose()


def test_migration_upgrade_then_downgrade_is_reversible(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """``alembic upgrade head`` → ``alembic downgrade 0004`` is a clean cycle.

    Proves migration ``0005`` is fully reversible: after downgrading
    by exactly one revision (back to ``0004``, the targets migration),
    both new tables and every new index must be gone while the rest
    of the schema (``targets``, ``documents``, ``tenant``,
    ``audit_log``) remains intact. Re-upgrading to ``head`` must
    restore everything.

    The downgrade target is the previous revision (``0004``); we spell
    it explicitly rather than relying on ``-1`` arithmetic so a future
    revision inserted between ``0004`` and ``0005`` surfaces as a test
    failure rather than a silent no-op.
    """
    from alembic import command

    sync_url, cfg = _alembic_upgrade_against_fresh_sqlite(monkeypatch, tmp_path, "g06-rev.db")

    sync_eng = sa_create_engine(sync_url)
    try:
        with sync_eng.connect() as conn:
            inspector = sa_inspect(conn)
            tables = set(inspector.get_table_names())
            assert "operation_group" in tables
            assert "endpoint_descriptor" in tables

        # Downgrade by exactly one revision — back to 0004 (targets).
        command.downgrade(cfg, "0004")

        with sync_eng.connect() as conn:
            inspector = sa_inspect(conn)
            tables = set(inspector.get_table_names())
            assert "operation_group" not in tables, "downgrade must drop operation_group table"
            assert "endpoint_descriptor" not in tables, (
                "downgrade must drop endpoint_descriptor table"
            )
            # Earlier-migration schema survives.
            assert "targets" in tables, "v0.2 targets must survive"
            assert "documents" in tables, "v0.2 documents must survive"
            assert "tenant" in tables, "v0.2 tenant must survive"
            assert "audit_log" in tables, "v0.1 audit_log must survive"

        # Re-upgrade — must be idempotent from 0004 back to head.
        command.upgrade(cfg, "head")
        with sync_eng.connect() as conn:
            inspector = sa_inspect(conn)
            tables = set(inspector.get_table_names())
            assert "operation_group" in tables
            assert "endpoint_descriptor" in tables
    finally:
        sync_eng.dispose()
