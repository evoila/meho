# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Create the endpoint_descriptor and operation_group tables.

Revision ID: 0005
Revises: 0004
Create Date: 2026-05-14

This migration is the schema foundation of Initiative #388 (G0.6
Operation registry + dispatcher substrate), Task #392 (T1). It adds
two structural pieces that every subsequent G0.6 task — and every G3
connector and the agent meta-tools (``search_operations``,
``list_operation_groups``, ``call_operation``) — read or write
against:

* ``operation_group`` — per-product/version/impl-id grouping of
  operations (e.g. ``vm-lifecycle``, ``kv``, ``zone``). Each row
  carries an LLM-summarised ``when_to_use`` blurb that the
  ``list_operation_groups`` meta-tool returns verbatim to drive
  agent intent routing. Per-tenant (``tenant_id IS NOT NULL``) rows
  hold tenant-curated grouping; ``tenant_id IS NULL`` rows are the
  built-in/global defaults shipped by spec ingestion (G0.7) or
  hand-coded by typed connectors (G3.x).
* ``endpoint_descriptor`` — the canonical per-operation row that the
  dispatcher (T5 #396) reads to validate, route, and execute. Every
  operation — whether auto-ingested from an OpenAPI spec (G0.7),
  registered by a typed connector (G3.x via T4
  ``register_typed_operation()``), or authored as a composite — gets
  exactly one row here. Columns mirror what the dispatcher needs:
  HTTP shape (``method``, ``path``) for ingested ops, ``handler_ref``
  for typed/composite ops, ``parameter_schema`` / ``response_schema``
  for validation, ``safety_level`` + ``requires_approval`` for the
  policy hook, ``embedding`` + ``summary`` + ``description`` for
  hybrid retrieval, ``llm_instructions`` for per-op agent guidance.

T1 ships **the tables and their indexes only**. Population is
deliberately out of scope:

* Typed-operation inserts are T4 (#395, ``register_typed_operation()``)
  territory; the helper computes embeddings + upserts rows.
* Spec-ingested inserts are G0.7 territory; the ingestion pipeline
  parses OpenAPI/SDL/WSDL/proto and writes rows in bulk.
* Read paths (the dispatcher, the meta-tools) are T5 / T8.

Unique-constraint shape — partial unique indexes
------------------------------------------------

The natural key for ``endpoint_descriptor`` is
``(product, version, impl_id, op_id)`` — but the
``(product, version, impl_id)`` axis identifies *which* connector
impl owns the op, and the same op_id may legitimately appear in
both a tenant-scoped composite (``tenant_id IS NOT NULL``) and a
built-in row (``tenant_id IS NULL``). Same shape for
``operation_group`` on ``(product, version, impl_id, group_key)``.

A single ``UNIQUE (tenant_id, product, version, impl_id, op_id)``
constraint would *not* enforce the invariant "two built-in rows
with the same coordinates collide" — NULL is not equal to NULL in
SQL UNIQUE semantics, so any number of ``tenant_id IS NULL`` rows
with identical product/version/impl_id/op_id would commit cleanly.

The fix is two **partial unique indexes** per table:

* one ``WHERE tenant_id IS NULL`` covering built-in/global rows
* one ``WHERE tenant_id IS NOT NULL`` covering tenant-scoped rows
  (including the ``tenant_id`` column in the index key so two tenants
  can each register the same op_id without collision)

PostgreSQL supports the syntax natively. SQLite has supported partial
indexes since 3.8.0 (we're on 3.45+) and SQLAlchemy emits the
``WHERE`` clause for both dialects via the ``postgresql_where`` /
``sqlite_where`` keyword pair on :func:`op.create_index`.

CHECK constraints — portable enum enforcement
---------------------------------------------

``operation_group.review_status``, ``endpoint_descriptor.source_kind``,
and ``endpoint_descriptor.safety_level`` are bounded enums. PostgreSQL
``ENUM`` types are dialect-specific and force migrations to manage a
named type lifecycle (CREATE TYPE / ALTER TYPE ADD VALUE / DROP TYPE)
that SQLite cannot mirror. The portable alternative — used by every
prior migration (see ``ck_targets_auth_model`` in 0004) — is
``TEXT NOT NULL`` with a ``CHECK (column IN (...))`` constraint:
identical enforcement on both dialects, no named type to track, no
``ALTER TYPE ADD VALUE`` migration friction when the enum widens.

Dialect-portability decisions
-----------------------------

Mirrors the discipline ``0001`` through ``0004`` established. The migration
runs cleanly against both PostgreSQL (production / pgvector image)
and SQLite (dev/test via aiosqlite).

* ``id`` server default — ``gen_random_uuid()`` on PG; SQLite leaves
  it to the ORM ``default=uuid.uuid4``.
* ``created_at`` / ``updated_at`` server defaults — ``now()`` on PG;
  SQLite leaves it to the ORM ``default=lambda: datetime.now(UTC)``.
* JSONB columns (``tags``, ``parameter_schema``, ``response_schema``,
  ``llm_instructions``) — :class:`JSONB` on PG, generic :class:`JSON`
  (text-stored) on SQLite via :func:`sqlalchemy.JSON.with_variant`.
* Boolean server defaults — ``false`` / ``true`` on PG, ``0`` / ``1``
  on SQLite (SQLite stores booleans as integers).
* ``embedding`` column type — ``vector(384)`` on PG (via pgvector's
  SQLAlchemy adapter), :class:`Text` on SQLite. Nullable on both
  dialects: T4 will populate it before the descriptor is enabled for
  retrieval, so the column tolerates a brief window between
  ``register_typed_operation()``'s insert and its embedding compute
  (or, in the operator-review queue model G0.7 lands, between
  ingestion and the reviewer enabling the row).
* GIN + IVFFlat indexes — PG only, emitted via raw ``op.execute``
  (Alembic has no ergonomic API for expression-based GIN nor for
  IVFFlat operator-class + ``WITH`` parameters). Same pattern as
  migration ``0003`` for ``documents``.

IVFFlat empty-table caveat
--------------------------

Building IVFFlat against an empty table produces zero centroids —
the planner correctly falls back to sequential scans until the index
is rebuilt with real data. Identical caveat to migration ``0003``'s
``documents_embedding_idx``. The remediation is the same: run
``REINDEX INDEX endpoint_descriptor_embedding_idx`` after the first
batch of operations is registered (typically the first connector
load post-upgrade). v0.2.next may switch to HNSW once corpus-recall
numbers are in.

FK on ``endpoint_descriptor.group_id``
--------------------------------------

``group_id UUID NULL REFERENCES operation_group(id) ON DELETE SET NULL``.
A descriptor may be group-less (an ingested op the LLM-summariser
declined to group, or a low-volume operation the operator
left uncategorised); NULL is the unassigned signal. When an
``operation_group`` row is deleted (operator removes a group via the
admin UI), descriptors pointing at it should not cascade-delete —
they should remain dispatchable, just ungrouped. ``ON DELETE SET
NULL`` enforces that.

Reversibility contract
----------------------

``downgrade()`` undoes everything ``upgrade()`` created in reverse
order: PG-only indexes (FTS + IVFFlat) → portable indexes →
``endpoint_descriptor`` table (drops the FK on ``group_id``
automatically) → ``operation_group`` partial-unique indexes →
``operation_group`` table. Explicit drops keep the inverse symmetric
on SQLite (which does not always cascade indexes on ``drop_table``).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create ``operation_group`` and ``endpoint_descriptor`` tables + indexes."""
    bind = op.get_bind()
    is_postgres = bind.dialect.name == "postgresql"

    # Portable JSONB → JSON variant; same pattern audit_log.payload and
    # documents.metadata use. PG gets binary JSONB (GIN-friendly,
    # indexable by ``@>``); SQLite gets text-stored JSON.
    json_type = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")

    # ``embedding`` column type — pgvector ``vector(384)`` on PG via the
    # SQLAlchemy adapter, plain Text on SQLite. Same dim as
    # documents.embedding (384 = BAAI/bge-small-en-v1.5 default). Nullable
    # because T1 ships the column shape only; T4 populates rows.
    if is_postgres:
        from pgvector.sqlalchemy import Vector

        embedding_type: sa.types.TypeEngine[object] = Vector(384)
    else:
        embedding_type = sa.Text()

    # ---------- operation_group ----------
    op.create_table(
        "operation_group",
        sa.Column(
            "id",
            sa.Uuid(),
            primary_key=True,
            nullable=False,
            server_default=sa.text("gen_random_uuid()") if is_postgres else None,
        ),
        # tenant_id NULL → built-in/global group; non-null → tenant-curated.
        # No FK to tenant.id in v0.2 by the same soft-FK discipline 0002
        # established for audit_log.tenant_id; the application layer
        # enforces referential integrity until a tightening migration adds
        # the FK.
        sa.Column("tenant_id", sa.Uuid(), nullable=True),
        sa.Column("product", sa.Text(), nullable=False),
        sa.Column("version", sa.Text(), nullable=False),
        sa.Column("impl_id", sa.Text(), nullable=False),
        sa.Column("group_key", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("when_to_use", sa.Text(), nullable=False),
        sa.Column(
            "review_status",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'staged'"),
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
        # Bounded-enum enforcement at the DB layer. Extend the IN(...)
        # list when the review-state machine gains new transitions.
        sa.CheckConstraint(
            "review_status IN ('staged', 'enabled', 'disabled')",
            name="ck_operation_group_review_status",
        ),
    )

    # Partial unique indexes on operation_group — see module docstring
    # for the rationale (NULL != NULL in SQL UNIQUE; built-in/global
    # rows need their own scope, tenant-scoped rows need theirs).
    op.create_index(
        "operation_group_global_idx",
        "operation_group",
        ["product", "version", "impl_id", "group_key"],
        unique=True,
        postgresql_where=sa.text("tenant_id IS NULL"),
        sqlite_where=sa.text("tenant_id IS NULL"),
    )
    op.create_index(
        "operation_group_tenant_idx",
        "operation_group",
        ["tenant_id", "product", "version", "impl_id", "group_key"],
        unique=True,
        postgresql_where=sa.text("tenant_id IS NOT NULL"),
        sqlite_where=sa.text("tenant_id IS NOT NULL"),
    )

    # ---------- endpoint_descriptor ----------
    op.create_table(
        "endpoint_descriptor",
        sa.Column(
            "id",
            sa.Uuid(),
            primary_key=True,
            nullable=False,
            server_default=sa.text("gen_random_uuid()") if is_postgres else None,
        ),
        sa.Column("tenant_id", sa.Uuid(), nullable=True),
        sa.Column("product", sa.Text(), nullable=False),
        sa.Column("version", sa.Text(), nullable=False),
        sa.Column("impl_id", sa.Text(), nullable=False),
        sa.Column("op_id", sa.Text(), nullable=False),
        sa.Column("source_kind", sa.Text(), nullable=False),
        sa.Column("method", sa.Text(), nullable=True),
        sa.Column("path", sa.Text(), nullable=True),
        sa.Column("handler_ref", sa.Text(), nullable=True),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        # FK to operation_group.id with ON DELETE SET NULL — see module
        # docstring. Group-less descriptors stay dispatchable.
        sa.Column(
            "group_id",
            sa.Uuid(),
            sa.ForeignKey("operation_group.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "tags",
            json_type,
            nullable=False,
            server_default=sa.text("'[]'::jsonb") if is_postgres else sa.text("'[]'"),
        ),
        sa.Column(
            "parameter_schema",
            json_type,
            nullable=False,
            server_default=sa.text("'{}'::jsonb") if is_postgres else sa.text("'{}'"),
        ),
        sa.Column("response_schema", json_type, nullable=True),
        sa.Column("llm_instructions", json_type, nullable=True),
        sa.Column(
            "safety_level",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'safe'"),
        ),
        sa.Column(
            "requires_approval",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false") if is_postgres else sa.text("0"),
        ),
        sa.Column(
            "is_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true") if is_postgres else sa.text("1"),
        ),
        # Nullable on both dialects — see module docstring. The ORM-side
        # type is :class:`_PortableVector384` so callers see list[float]
        # regardless of dialect (JSON-encoded on SQLite, native on PG).
        sa.Column("embedding", embedding_type, nullable=True),
        sa.Column("custom_description", sa.Text(), nullable=True),
        sa.Column("custom_notes", sa.Text(), nullable=True),
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
        sa.CheckConstraint(
            "source_kind IN ('ingested', 'typed', 'composite')",
            name="ck_endpoint_descriptor_source_kind",
        ),
        sa.CheckConstraint(
            "safety_level IN ('safe', 'caution', 'dangerous')",
            name="ck_endpoint_descriptor_safety_level",
        ),
    )

    # Partial unique indexes mirroring the operation_group split.
    op.create_index(
        "endpoint_descriptor_global_idx",
        "endpoint_descriptor",
        ["product", "version", "impl_id", "op_id"],
        unique=True,
        postgresql_where=sa.text("tenant_id IS NULL"),
        sqlite_where=sa.text("tenant_id IS NULL"),
    )
    op.create_index(
        "endpoint_descriptor_tenant_idx",
        "endpoint_descriptor",
        ["tenant_id", "product", "version", "impl_id", "op_id"],
        unique=True,
        postgresql_where=sa.text("tenant_id IS NOT NULL"),
        sqlite_where=sa.text("tenant_id IS NOT NULL"),
    )

    # Group-scoped lookup — drives "list every enabled op in group X for
    # connector (product, version, impl_id)" queries from the dispatcher
    # and the search_operations meta-tool.
    op.create_index(
        "endpoint_descriptor_lookup_idx",
        "endpoint_descriptor",
        ["product", "version", "impl_id", "group_id", "is_enabled"],
        postgresql_using="btree",
    )

    # PG-only: GIN over the FTS expression for BM25 (matches the
    # documents table pattern from migration 0003), IVFFlat for cosine.
    # Both emitted via raw op.execute — Alembic has no ergonomic API for
    # expression-based GIN nor for IVFFlat with vector_cosine_ops + WITH
    # parameters. These indexes are intentionally NOT declared on the
    # ORM's __table_args__: doing so would force SQLite create_all to
    # attempt creation and fail.
    if is_postgres:
        op.execute(
            "CREATE INDEX endpoint_descriptor_bm25_idx ON endpoint_descriptor "
            "USING GIN (to_tsvector('english', "
            "coalesce(summary, '') || ' ' || coalesce(description, '')))"
        )
        op.execute(
            "CREATE INDEX endpoint_descriptor_embedding_idx ON endpoint_descriptor "
            "USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100)"
        )


def downgrade() -> None:
    """Reverse the upgrade — drop everything in reverse order.

    Symmetric inverse of :func:`upgrade`. Indexes drop explicitly so
    the reversal is clean on SQLite as well as PG (SQLite does not
    always cascade indexes on ``drop_table``). The FK on
    ``endpoint_descriptor.group_id`` drops with the table; the
    ``operation_group`` table is dropped last so the FK's referenced
    side stays valid until the referring table is gone.
    """
    bind = op.get_bind()
    is_postgres = bind.dialect.name == "postgresql"

    if is_postgres:
        # IF EXISTS — these indexes were never created on SQLite, and a
        # cluster that lost the pgvector extension between upgrade and
        # downgrade would otherwise fail here.
        op.execute("DROP INDEX IF EXISTS endpoint_descriptor_embedding_idx")
        op.execute("DROP INDEX IF EXISTS endpoint_descriptor_bm25_idx")

    op.drop_index("endpoint_descriptor_lookup_idx", table_name="endpoint_descriptor")
    op.drop_index("endpoint_descriptor_tenant_idx", table_name="endpoint_descriptor")
    op.drop_index("endpoint_descriptor_global_idx", table_name="endpoint_descriptor")
    op.drop_table("endpoint_descriptor")

    op.drop_index("operation_group_tenant_idx", table_name="operation_group")
    op.drop_index("operation_group_global_idx", table_name="operation_group")
    op.drop_table("operation_group")
