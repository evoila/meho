# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Create the documents table backed by the pgvector extension.

Revision ID: 0003
Revises: 0002
Create Date: 2026-05-12

This is the schema foundation of Initiative #225 (G0.4 Retrieval
substrate), Task #258 (T1). The migration adds three structural
pieces that every G4 (#215, knowledge) and G5 (#216, memory)
ingestion path will rely on:

* The PostgreSQL ``vector`` extension (``CREATE EXTENSION IF NOT
  EXISTS vector``), enabled on PG only. The extension ships with
  ``pgvector/pgvector:pg16`` (and similar managed-PG offerings:
  RDS, Cloud SQL, Azure DB). On a stock ``postgres:16-alpine``
  the extension is absent and the migration fails fast with
  ``extension "vector" is not available`` — surfacing the deploy
  prerequisite at migration time rather than at first retrieval
  request.
* The ``documents`` table — a shared per-tenant retrievable
  document store. Both G4 (kb entries) and G5 (memory entries)
  write rows here via the shared ``index_document`` helper that
  G0.4-T3 (#260) will land; the ``retrieve`` helper (T4, #261)
  reads them back via hybrid BM25 + cosine RRF.
* Four indexes: a unique composite (``tenant_id``, ``source``,
  ``source_id``) for upsert-by-natural-key, a btree on
  ``body_hash`` for change-detection during refresh, a GIN
  index over ``to_tsvector('english', body)`` for BM25, and an
  IVFFlat index over ``embedding`` for cosine similarity.

Numbering note
--------------

The Initiative body in issue #225 / Task #258 refers to this file
as ``0004_create_documents_with_pgvector.py`` revising ``0003``.
That was the issue author's draft assumption — at the time the
issue was filed, an intermediate ``0003`` migration was anticipated
between G0.1-T1 (the tenant migration, ``0002``) and the
retrieval substrate. The intermediate never landed: G0.1-T3
(contextvar binding, #233) and G0.1-T4 (RBAC primitive, #264)
both shipped without schema changes. So the next sequential
revision on ``main`` is ``0003``, which is what this file is.

Dialect-portability decisions
-----------------------------

Mirrors the discipline ``0001_create_audit_log.py`` and
``0002_create_tenant_and_audit_tenant_id.py`` established. The
migration runs cleanly against both PostgreSQL (production /
pgvector image) and SQLite (dev/test via aiosqlite). SQLite
exercises the schema *shape* — column names, NOT NULL constraints,
the two portable indexes — but never sees real embeddings; the
embedding column is :class:`TEXT` on SQLite via
``with_variant``, which accepts any string placeholder.

* **``vector`` extension** — PG only. Wrapped in ``CREATE
  EXTENSION IF NOT EXISTS`` so re-running the migration against a
  cluster that already has the extension installed is a no-op.
  The downgrade deliberately leaves the extension installed —
  other tenants of the same DB cluster (future MEHO-adjacent
  services, operator-side tooling) may share it; ``DROP
  EXTENSION vector CASCADE`` would silently drop their tables'
  vector columns too. Reversibility is at the table level, not
  the cluster-extension level.
* **``id`` server default** — PG 13+ ships ``gen_random_uuid()``
  built-in (same assumption ``0001`` and ``0002`` already make);
  SQLite leaves the column without a server default and lets the
  ORM ``default=uuid.uuid4`` populate it Python-side.
* **``created_at`` / ``updated_at`` server defaults** — PG gets
  ``now()``; SQLite leaves it to ORM-side
  ``default=lambda: datetime.now(UTC)``. The ORM also declares
  ``onupdate=lambda: datetime.now(UTC)`` on ``updated_at`` so
  re-indexing through the ORM bumps the timestamp; raw-SQL
  UPDATEs against PG would *not* fire the ORM hook, which is
  acceptable in v0.2 because the substrate's only writer is the
  ORM-backed ``index_document`` helper. A future PG-side trigger
  is an additive change, out of scope here.
* **``embedding`` column type** — PG ``vector(384)`` via the
  ``with_variant`` override; SQLite generic ``TEXT``. The 384
  dimensionality matches ``BAAI/bge-small-en-v1.5`` (the default
  the EmbeddingService in T2 will load). A future model swap
  with different dimensionality requires a re-embed-everything
  migration; not in scope for v0.2.
* **``metadata`` column** — Portable :class:`JSON` →
  :class:`JSONB` via ``with_variant``, same pattern as
  ``audit_log.payload``. Server default ``'{}'::jsonb`` on PG
  matches the value the ORM uses (``default=dict``) so out-of-
  band PG inserts that omit ``metadata`` still satisfy NOT NULL.
* **Indexes** — Two portable indexes (``documents_tenant_source_id_idx``
  unique, ``documents_body_hash_idx``) are declared via
  :func:`op.create_index` with ``postgresql_using="btree"`` so
  SQLite gets a btree no-op and PG gets an explicit btree. The
  two PG-only indexes (``documents_body_fts_idx`` GIN over a
  ``to_tsvector`` expression, ``documents_embedding_idx`` IVFFlat
  with ``vector_cosine_ops`` and ``lists = 100``) are emitted via
  raw ``op.execute`` inside ``if is_postgres:`` because Alembic
  has no ergonomic API for expression-based GIN or for IVFFlat
  with operator-class + ``WITH`` parameters. They are
  intentionally **not** declared on ``Document.__table_args__``:
  doing so would force SQLite's autogenerate / ``create_all`` to
  attempt to create them and fail. The trade-off is that
  ``alembic revision --autogenerate`` must always run against
  PG, never SQLite — running against SQLite would diff-add the
  two missing indexes.

IVFFlat empty-table caveat
--------------------------

IVFFlat assigns vectors to centroids computed at index-build
time. Building the index against an empty table produces an
index with zero centroids, which the planner correctly treats as
non-useful — every query falls back to a sequential scan until
the index is rebuilt against actual data. T1 ships the index
against the empty table because the alternative (deferring index
creation to T3/T4 backfill) splits the schema across migrations
and complicates rollback semantics. The expected operator
remediation, documented in the substrate's runbook (filed
alongside T3/T4 #260/#261), is to run
``REINDEX INDEX documents_embedding_idx`` after the initial
backfill batch — typically the first ``meho kb refresh`` post-
upgrade. For v0.2 corpus sizes (single-tenant, hundreds to low
thousands of documents) this is a one-off operation; v0.2.next
may switch to HNSW (``USING hnsw (embedding vector_cosine_ops)
WITH (m = 16, ef_construction = 64)``) which does not suffer
from the empty-table centroid problem — the Initiative body
defers that swap to a v0.2.next ticket gated on G4 corpus recall
numbers.

Reversibility contract
----------------------

``downgrade()`` removes everything the table-level upgrade
creates, in reverse order: drop the two PG-only indexes (if
applicable) → drop the two portable indexes → drop the
``documents`` table. The ``vector`` extension stays installed
(see above). The CI guard
(``scripts/ci/check_migration_compat.py``) inspects only
``upgrade()``; the destructive operations in ``downgrade()`` are
allowed by design.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Enable pgvector, create ``documents`` + four indexes."""
    bind = op.get_bind()
    is_postgres = bind.dialect.name == "postgresql"

    # ``vector`` extension — PG only; absent extension on the target
    # cluster surfaces here as ``extension "vector" is not available``,
    # which is the intended fail-fast contract (see module docstring).
    if is_postgres:
        op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    # ``embedding`` column type — ``vector(384)`` on PG via the
    # pgvector SQLAlchemy adapter; ``TEXT`` on SQLite so the dev/test
    # driver can run the migration without the extension. The 384
    # dimensionality matches the default ``BAAI/bge-small-en-v1.5``
    # model T2 (#259) will wire in. The local import keeps the
    # ``pgvector`` package off the SQLite-only call path; the
    # dependency is in ``[project.dependencies]`` so every install
    # already has it, but the local import avoids paying the import
    # cost on the non-PG branch.
    if is_postgres:
        from pgvector.sqlalchemy import Vector

        embedding_type: sa.types.TypeEngine[object] = Vector(384)
    else:
        embedding_type = sa.Text()

    # ``metadata`` column type — same JSONB-on-PG / JSON-on-SQLite
    # variant the audit_log.payload column uses. The PG-side server
    # default mirrors the ORM ``default=dict``.
    metadata_type = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")

    op.create_table(
        "documents",
        sa.Column(
            "id",
            sa.Uuid(),
            primary_key=True,
            nullable=False,
            server_default=sa.text("gen_random_uuid()") if is_postgres else None,
        ),
        # ``tenant_id`` — real ``REFERENCES tenant(id)`` FK. Unlike
        # ``audit_log.tenant_id`` (chassis-era rows with no real
        # tenant to point at, FK deferred to v0.2.next backfill;
        # see 0002's docstring), ``documents`` is a brand-new table
        # with no pre-existing rows and a downgrade that drops the
        # whole table — there is no backfill or cascade decision to
        # defer. Enforcing the FK at the DB layer makes orphan-row
        # insertion (typo / deleted tenant / replayed contextvar)
        # impossible at the substrate boundary rather than relying
        # on app-layer validation in T3's ``index_document``.
        sa.Column(
            "tenant_id",
            sa.Uuid(),
            sa.ForeignKey("tenant.id"),
            nullable=False,
        ),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("source_id", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("body_hash", sa.Text(), nullable=False),
        sa.Column("tokens", sa.Integer(), nullable=True),
        sa.Column("embedding", embedding_type, nullable=False),
        sa.Column(
            "metadata",
            metadata_type,
            nullable=False,
            server_default=sa.text("'{}'::jsonb") if is_postgres else sa.text("'{}'"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()") if is_postgres else None,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()") if is_postgres else None,
        ),
    )

    # Unique composite index — the natural-key for upsert. The
    # ``index_document`` helper (T3 #260) uses ``(tenant_id, source,
    # source_id)`` as its on-conflict target; uniqueness is enforced
    # exclusively by this named index so PG does not auto-create a
    # second unique index alongside it (same discipline as
    # ``tenant_slug_idx`` in 0002).
    op.create_index(
        "documents_tenant_source_id_idx",
        "documents",
        ["tenant_id", "source", "source_id"],
        unique=True,
        postgresql_using="btree",
    )
    # Btree on ``body_hash`` for change-detection on kb refresh:
    # T3 short-circuits the embed step when ``body_hash`` matches the
    # existing row's. Indexed because the lookup is per-document and
    # would otherwise scan the table on every refresh.
    op.create_index(
        "documents_body_hash_idx",
        "documents",
        ["body_hash"],
        postgresql_using="btree",
    )

    # PG-only: GIN over the FTS expression for BM25, IVFFlat for
    # cosine. Both emitted via raw ``op.execute`` because Alembic
    # has no clean API for expression-based GIN or for IVFFlat with
    # ``vector_cosine_ops`` + ``WITH (lists = ...)``. See module
    # docstring on why these indexes are NOT in
    # ``Document.__table_args__``.
    if is_postgres:
        op.execute(
            "CREATE INDEX documents_body_fts_idx ON documents "
            "USING GIN (to_tsvector('english', body))"
        )
        op.execute(
            "CREATE INDEX documents_embedding_idx ON documents "
            "USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100)"
        )


def downgrade() -> None:
    """Drop the ``documents`` table and its indexes, leave the extension.

    Symmetric inverse of :func:`upgrade` at the table level; the
    ``vector`` extension is intentionally left installed (see
    module docstring on cluster-shared extension lifecycle). Indexes
    drop explicitly so the reversal is clean on SQLite as well as PG.
    """
    bind = op.get_bind()
    is_postgres = bind.dialect.name == "postgresql"

    if is_postgres:
        # ``IF EXISTS`` because the two PG-only indexes were never
        # created on SQLite — the downgrade path is dialect-aware on
        # the same axis as the upgrade.
        op.execute("DROP INDEX IF EXISTS documents_embedding_idx")
        op.execute("DROP INDEX IF EXISTS documents_body_fts_idx")
    op.drop_index("documents_body_hash_idx", table_name="documents")
    op.drop_index("documents_tenant_source_id_idx", table_name="documents")
    op.drop_table("documents")
    # Deliberately DO NOT ``DROP EXTENSION vector``; see module docstring.
