# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for :class:`meho_backplane.db.models.Document`.

Coverage matrix (Task #258 / G0.4-T1 acceptance criteria):

* Round-trip on :class:`Document` — insert a row, query it back,
  every field round-trips through the SQLite dev/test driver. The
  ORM ``default=`` machinery (uuid, created_at, doc_metadata)
  fires on the SQLite path where the migration's PG server
  defaults are no-ops.
* Unique composite index — two documents sharing
  ``(tenant_id, source, source_id)`` raise :class:`IntegrityError`
  on commit. Proves migration ``0003``'s
  ``documents_tenant_source_id_idx`` is enforced at the DB layer
  (not just a UI-side validator).
* Cross-tenant ``body_hash`` repetition — two documents with the
  same ``body_hash`` in *different* tenants commit successfully.
  Pins the contract that the body_hash index is **not** unique;
  the change-detection short-circuit in :func:`index_document`
  (G0.4-T3, #260) compares per-document-row, not globally.
* ``doc_metadata`` Python-attribute / ``metadata`` SQL-column
  rename — the underlying SQL column is named ``metadata`` per
  the migration; the ORM attribute is ``doc_metadata`` because
  ``metadata`` is reserved on :class:`DeclarativeBase`. The test
  inspects the on-disk schema to lock in the column name.
* ``updated_at`` ORM ``onupdate`` — modifying a row through the
  ORM bumps ``updated_at``. Raw-SQL UPDATEs against PG would
  *not* fire this hook; the substrate's only writer in v0.2 is
  ORM-backed (T3's :func:`index_document`), so the ORM contract
  is the load-bearing one.

The tests run synchronously against ``sqlite+aiosqlite`` via the
shared engine cache that the autouse ``_default_database_url``
fixture in :mod:`tests.conftest` already pre-migrates to
``alembic upgrade head``. The PG-real assertions (column type =
``vector``, IVFFlat + GIN indexes installed, ``pg_extension``
contains ``vector``) live in
:class:`tests.test_db_engine.TestPostgresIntegration` — Docker
sandbox-skipped, exercised on CI runners that provision Docker.

SQLite embedding-column shape
-----------------------------

The ``embedding`` column is :class:`Vector(384)` on PostgreSQL and
a JSON-encoded :class:`Text` column on SQLite, wrapped by the
``_PortableVector384`` :class:`TypeDecorator` (see
:mod:`meho_backplane.db.models`). The Python contract is
``list[float]`` on **both** dialects: on PG via pgvector's bind /
result processors, on SQLite via the decorator's
``json.dumps`` / ``json.loads`` round-trip. Test rows therefore
pass a real ``list[float]`` and get one back — no stringified
placeholders, no ``# type: ignore`` on the call sites.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from sqlalchemy import inspect as sa_inspect
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

from meho_backplane.db.engine import get_engine, get_sessionmaker
from meho_backplane.db.models import Document
from meho_backplane.settings import get_settings


@pytest.fixture(autouse=True)
def _required_settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin every env var :class:`Settings` requires for this module.

    Mirrors :func:`tests.test_db_models._required_settings_env` — the
    autouse ``_default_database_url`` fixture in
    :mod:`tests.conftest` only pins ``DATABASE_URL``; Keycloak / Vault
    knobs come from each test file. The ``get_settings`` cache reset
    around the yield keeps a stale ``Settings`` instance from a
    previous test from leaking in.
    """
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


# A 384-element placeholder vector. The ``_PortableVector384``
# TypeDecorator (see ``meho_backplane.db.models``) JSON-encodes
# ``list[float]`` on SQLite and lets pgvector's own bind processor
# handle the PG path, so the same value works against both dialects.
# Keeping it as a module-level constant avoids repeating the literal
# across tests.
_PLACEHOLDER_EMBEDDING: list[float] = [0.0] * 384


# ---------------------------------------------------------------------------
# Round-trip
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_document_round_trip_persists_every_field() -> None:
    """Insert a :class:`Document`, query it back, every field matches.

    Drives the model via :func:`get_sessionmaker` so the path matches
    what production callers (T3's :func:`index_document`, T4's
    :func:`retrieve`) will use. Asserts every column round-trips —
    catches a regression where a future column rename / type swap
    silently drops data.
    """
    sessionmaker = get_sessionmaker()
    doc_id = uuid.uuid4()
    tenant_id = uuid.uuid4()
    created_at = datetime.now(UTC)
    async with sessionmaker() as session:
        session.add(
            Document(
                id=doc_id,
                tenant_id=tenant_id,
                source="kb",
                source_id="kubernetes-ingress",
                kind="kb-entry",
                body="Some kb entry body about Kubernetes ingress troubleshooting.",
                body_hash="abc123def456",
                tokens=12,
                embedding=_PLACEHOLDER_EMBEDDING,
                doc_metadata={"author": "ops", "tags": ["k8s", "ingress"]},
                created_at=created_at,
                updated_at=created_at,
            )
        )
        await session.commit()

    async with sessionmaker() as session:
        result = await session.execute(select(Document).where(Document.id == doc_id))
        row = result.scalar_one()

    assert row.id == doc_id
    assert row.tenant_id == tenant_id
    assert row.source == "kb"
    assert row.source_id == "kubernetes-ingress"
    assert row.kind == "kb-entry"
    assert row.body.startswith("Some kb entry body")
    assert row.body_hash == "abc123def456"
    assert row.tokens == 12
    # Embedding round-trips as ``list[float]`` on **both** dialects via
    # the ``_PortableVector384`` TypeDecorator (JSON-encoded ``Text`` on
    # SQLite, native ``vector(384)`` on PG). PG-side tests in
    # ``TestPostgresIntegration`` separately assert the ``vector(384)``
    # column type via ``pg_attribute``.
    assert row.embedding == _PLACEHOLDER_EMBEDDING
    assert row.doc_metadata == {"author": "ops", "tags": ["k8s", "ingress"]}
    # SQLite drops tzinfo on round-trip; compare wall-clock parts. The
    # PG production driver returns tz-aware values (covered by
    # TestPostgresIntegration).
    assert row.created_at.replace(tzinfo=None) == created_at.replace(tzinfo=None)
    assert row.updated_at.replace(tzinfo=None) == created_at.replace(tzinfo=None)


@pytest.mark.asyncio
async def test_document_orm_defaults_fire_on_sqlite() -> None:
    """``id``, ``created_at``, ``updated_at``, ``doc_metadata`` get populated by ORM.

    The migration's PG-side server defaults (``gen_random_uuid()``,
    ``now()``, ``'{}'::jsonb``) are no-ops on SQLite. The ORM
    ``default=`` / ``default=dict`` / ``default=lambda: datetime.now(UTC)``
    must fill the column Python-side. A regression where someone drops
    the ORM default in favour of relying on the migration would surface
    here as a NOT NULL violation on SQLite.
    """
    sessionmaker = get_sessionmaker()
    before = datetime.now(UTC)
    async with sessionmaker() as session:
        doc = Document(
            tenant_id=uuid.uuid4(),
            source="kb",
            source_id="orm-defaults-probe",
            kind="kb-entry",
            body="orm-defaults probe body",
            body_hash="orm-defaults-hash",
            embedding=_PLACEHOLDER_EMBEDDING,
        )
        session.add(doc)
        await session.commit()
        # Capture in-session after the commit so onupdate / defaults
        # are observable on the instance.
        seen_id = doc.id
        seen_metadata = doc.doc_metadata
        seen_created_at = doc.created_at
        seen_updated_at = doc.updated_at

    assert isinstance(seen_id, uuid.UUID)
    assert seen_metadata == {}
    # Bracket the wall-clock check to absorb minor clock drift between
    # ``before`` and the ORM's default-callable firing.
    assert seen_created_at.replace(tzinfo=None) >= before.replace(tzinfo=None)
    assert seen_updated_at.replace(tzinfo=None) >= before.replace(tzinfo=None)


# ---------------------------------------------------------------------------
# Unique composite index
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_document_unique_tenant_source_id_index_rejects_duplicates() -> None:
    """Two docs sharing ``(tenant_id, source, source_id)`` → IntegrityError.

    Locks in that migration ``0003``'s
    ``documents_tenant_source_id_idx`` is the natural-key upsert
    target T3's :func:`index_document` will use. Without DB-layer
    uniqueness, two concurrent ``index_document`` calls could both
    create rows for the same logical document, splitting writes and
    breaking the body_hash short-circuit's invariant.
    """
    sessionmaker = get_sessionmaker()
    shared_tenant = uuid.uuid4()
    async with sessionmaker() as session:
        session.add(
            Document(
                tenant_id=shared_tenant,
                source="kb",
                source_id="duplicate-target",
                kind="kb-entry",
                body="first body",
                body_hash="hash-one",
                embedding=_PLACEHOLDER_EMBEDDING,
            )
        )
        await session.commit()

    async with sessionmaker() as session:
        session.add(
            Document(
                tenant_id=shared_tenant,
                source="kb",
                source_id="duplicate-target",
                kind="kb-entry",
                body="second body — should not commit",
                body_hash="hash-two",
                embedding=_PLACEHOLDER_EMBEDDING,
            )
        )
        with pytest.raises(IntegrityError):
            await session.commit()


@pytest.mark.asyncio
async def test_document_body_hash_repeats_across_tenants_are_allowed() -> None:
    """Same ``body_hash`` in two tenants commits cleanly.

    Pins that the ``documents_body_hash_idx`` is **not** unique —
    body_hash is only a change-detection probe per-row, not a global
    identifier. Two tenants ingesting identical kb content (same
    upstream doc, different tenants) must each store their own row;
    making body_hash globally unique would fail the second tenant's
    insert silently and break tenant isolation guarantees.
    """
    sessionmaker = get_sessionmaker()
    tenant_a = uuid.uuid4()
    tenant_b = uuid.uuid4()
    shared_hash = "identical-content-sha256"
    async with sessionmaker() as session:
        session.add(
            Document(
                tenant_id=tenant_a,
                source="kb",
                source_id="shared-content",
                kind="kb-entry",
                body="The same body in both tenants.",
                body_hash=shared_hash,
                embedding=_PLACEHOLDER_EMBEDDING,
            )
        )
        session.add(
            Document(
                tenant_id=tenant_b,
                source="kb",
                source_id="shared-content",
                kind="kb-entry",
                body="The same body in both tenants.",
                body_hash=shared_hash,
                embedding=_PLACEHOLDER_EMBEDDING,
            )
        )
        # Both rows must commit cleanly — same body_hash, different tenant.
        await session.commit()

    async with sessionmaker() as session:
        result = await session.execute(select(Document).where(Document.body_hash == shared_hash))
        rows = result.scalars().all()

    assert len(rows) == 2
    assert {row.tenant_id for row in rows} == {tenant_a, tenant_b}


# ---------------------------------------------------------------------------
# Column-name rename (``metadata`` reserved on DeclarativeBase)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_document_doc_metadata_attribute_maps_to_metadata_column() -> None:
    """Inspect the schema: SQL column is ``metadata`` even though ORM attribute is ``doc_metadata``.

    The rename is a hard requirement — ``DeclarativeBase.metadata``
    is the reserved table-registry attribute, so the Python side
    must call it something else (``doc_metadata`` here). The SQL
    side must keep the migration's column name verbatim so the
    table identifier stays stable across schema-shape evolution
    and so ``alembic revision --autogenerate`` doesn't drift.

    This test catches the regression where someone "fixes" the
    rename by also renaming the SQL column — at which point the
    next migration would diff-add a new ``doc_metadata`` column
    and drop ``metadata``, breaking forward-compat.
    """
    engine: AsyncEngine = get_engine()
    async with engine.connect() as conn:
        columns = await conn.run_sync(
            lambda sync_conn: sa_inspect(sync_conn).get_columns("documents")
        )

    column_names = {col["name"] for col in columns}
    assert "metadata" in column_names, (
        "SQL column must be named 'metadata' (matches migration 0003); "
        f"got columns: {sorted(column_names)}"
    )
    assert "doc_metadata" not in column_names, (
        "SQL column must NOT be named 'doc_metadata' — that's the Python "
        "attribute only; renaming the column would drift from migration 0003"
    )


# ---------------------------------------------------------------------------
# ``updated_at`` ORM ``onupdate``
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_document_updated_at_refreshes_on_orm_update() -> None:
    """Modifying a row via the ORM bumps ``updated_at``.

    The ``onupdate=lambda: datetime.now(UTC)`` on
    :attr:`Document.updated_at` is the ORM-level trigger that
    keeps the timestamp fresh. T3's :func:`index_document` relies
    on this: when a document is re-indexed with a changed body, the
    helper expects ``updated_at`` to advance so the audit /
    observability surface can answer "when was this doc last
    refreshed?".

    A regression where someone drops ``onupdate`` (e.g. by moving
    to a PG-side trigger and forgetting to keep the ORM hook for
    SQLite) would silently freeze the column on every UPDATE.
    """
    sessionmaker = get_sessionmaker()
    doc_id = uuid.uuid4()
    async with sessionmaker() as session:
        session.add(
            Document(
                id=doc_id,
                tenant_id=uuid.uuid4(),
                source="kb",
                source_id="onupdate-probe",
                kind="kb-entry",
                body="initial body",
                body_hash="initial-hash",
                embedding=_PLACEHOLDER_EMBEDDING,
            )
        )
        await session.commit()
        original_updated_at = (
            await session.execute(select(Document.updated_at).where(Document.id == doc_id))
        ).scalar_one()

    # Brief pause so the next ``datetime.now(UTC)`` lands a measurable
    # delta on systems where the ORM ``onupdate`` callable resolves in
    # the same microsecond as the insert. SQLite stores datetimes as
    # ISO-8601 strings with microsecond precision; 10 ms is well above
    # the resolution floor. Async sleep keeps the pytest-asyncio event
    # loop responsive — a blocking ``time.sleep`` would stall every
    # in-flight coroutine on the same loop and turn this test flaky in
    # ``asyncio_mode = "auto"`` suites.
    await asyncio.sleep(0.01)

    async with sessionmaker() as session:
        result = await session.execute(select(Document).where(Document.id == doc_id))
        row = result.scalar_one()
        row.body = "updated body"
        row.body_hash = "updated-hash"
        await session.commit()
        post_commit_updated_at = row.updated_at

    # In-session: ORM ``onupdate`` fires during flush, mutates the
    # attribute on the instance, commits. With ``expire_on_commit=False``
    # (see ``meho_backplane.db.engine.get_sessionmaker``) the value
    # stays accessible. SQLite re-reads timestamps as tz-naive while
    # the ORM ``onupdate`` lambda mints tz-aware values; normalise both
    # to naive (.replace(tzinfo=None)) so the comparison is safe across
    # the SQLite round-trip / fresh-mint boundary.
    # Strict ``>`` (not ``>=``): the 10 ms ``asyncio.sleep`` above buys
    # a guaranteed delta, so an equal timestamp means ``onupdate`` did
    # not fire — which is exactly the regression this test claims to
    # catch. ``>=`` would let that regression pass silently.
    assert post_commit_updated_at.replace(tzinfo=None) > original_updated_at.replace(tzinfo=None), (
        "ORM onupdate must advance updated_at on every UPDATE; "
        f"original={original_updated_at} post={post_commit_updated_at}"
    )

    # Cross-session: re-fetch and confirm the persisted value matches
    # the post-commit attribute (catches a regression where the
    # ``onupdate`` callable fires on the instance but the column is
    # not actually written to the DB). Bind ``refetched`` to
    # ``post_commit_updated_at`` to lock in that the DB persisted the
    # in-session value verbatim, then re-prove the strict ``>`` gap
    # against ``original_updated_at`` on the cross-session read.
    async with sessionmaker() as session:
        refetched = (
            await session.execute(select(Document.updated_at).where(Document.id == doc_id))
        ).scalar_one()
    assert refetched.replace(tzinfo=None) == post_commit_updated_at.replace(tzinfo=None)
    assert refetched.replace(tzinfo=None) > original_updated_at.replace(tzinfo=None)
