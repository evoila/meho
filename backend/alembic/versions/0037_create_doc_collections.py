# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Create the doc_collections table (collections-as-data registry).

Revision ID: 0037
Revises: 0036
Create Date: 2026-06-06

This migration is the schema foundation of Initiative #1548 (G4.6
Doc-collection catalogue), Task #1550 (T1). It adds the
``doc_collections`` table — one row per documentation corpus an agent
can search. The table is the docs analogue of ``targets``:
``list_targets`` answers "what infra can I act on?", the catalogue
built on this table (T4 #1553) answers "what docs can I search?".

The registry is **authoritative for identity + backend binding**
(operator-set ``collection_key`` / ``vendor`` / ``backend``), with
**liveness fields probe-written from the backend** (``doc_count`` /
``last_ingested_at`` / ``readiness``, T6 #1555). This mirrors the
targets-as-data split (``targets`` rows + ``Target.fingerprint``
written by the probe).

Tenancy — NULLABLE tenant_id
----------------------------

``tenant_id`` is NULL for global / shared collections (every tenant
sees them) and populated for tenant-curated collections, the same
global+tenant idiom ``operation_group`` established in migration 0005.
No FK to ``tenant.id`` by the soft-FK discipline 0002 established for
``audit_log.tenant_id``; the application layer enforces referential
integrity until a tightening migration adds the FK.

Unique-constraint shape — partial unique indexes
------------------------------------------------

The natural key is ``collection_key``, but the same key may
legitimately appear in both a global row (``tenant_id IS NULL``) and a
tenant-curated row (``tenant_id IS NOT NULL``) — the resolver prefers
the tenant row. A single ``UNIQUE (tenant_id, collection_key)``
constraint would *not* enforce "two global rows with the same key
collide" — NULL != NULL in SQL UNIQUE semantics, so any number of
``tenant_id IS NULL`` rows with identical ``collection_key`` would
commit cleanly.

The fix is two **partial unique indexes** (the ``operation_group``
pattern from migration 0005):

* one ``WHERE tenant_id IS NULL`` on ``(collection_key)`` for
  global/shared rows
* one ``WHERE tenant_id IS NOT NULL`` on ``(tenant_id, collection_key)``
  for tenant-scoped rows (the ``tenant_id`` column is in the key so two
  tenants can each curate the same ``collection_key`` without collision)

PostgreSQL supports the syntax natively; SQLite has supported partial
indexes since 3.8.0 (we run 3.45+). SQLAlchemy emits the ``WHERE``
clause for both dialects via the ``postgresql_where`` / ``sqlite_where``
keyword pair on :func:`op.create_index`.

CHECK constraint — portable enum enforcement
--------------------------------------------

``status`` is a bounded enum (``'provisioning'`` / ``'ready'`` /
``'rebuilding'`` / ``'disabled'``). Per the discipline every prior
migration follows (see ``ck_operation_group_review_status`` in 0005),
it is ``TEXT NOT NULL`` + a ``CHECK (status IN (...))`` constraint
rather than a PG-specific named ``ENUM`` type — identical enforcement
on both dialects, no named type lifecycle to track when the enum
widens.

Dialect-portability decisions
-----------------------------

Mirrors the discipline 0001 through 0036 established; runs cleanly on
both PostgreSQL (production / pgvector image) and SQLite (dev/test via
aiosqlite).

* ``id`` server default — ``gen_random_uuid()`` on PG; SQLite relies on
  the ORM ``default=uuid.uuid4``.
* ``created_at`` / ``updated_at`` server defaults — ``now()`` on PG;
  SQLite leaves them to the ORM ``default=lambda: datetime.now(UTC)``.
* ``products`` — native ``TEXT[]`` on PG (GIN-indexable containment),
  JSON-text array on SQLite. NOT NULL with an empty-array default.
* ``backend`` / ``readiness`` / ``extras`` — JSONB on PG, generic JSON
  (text) on SQLite via :func:`sqlalchemy.JSON.with_variant`.
* ``products`` GIN index — PG only, wrapped in ``if is_postgres:``
  (SQLite cannot index a JSON/TEXT[] column with GIN semantics).

Reversibility contract
----------------------

``downgrade()`` undoes everything ``upgrade()`` created in reverse
order: GIN index (PG only) → partial-unique indexes → table. Explicit
index drops keep the inverse symmetric on SQLite (which does not always
cascade indexes on ``drop_table``).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0037"
down_revision: str | None = "0036"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the ``doc_collections`` table + its indexes."""
    bind = op.get_bind()
    is_postgres = bind.dialect.name == "postgresql"

    # Portable JSONB → JSON variant; PG gets binary JSONB (GIN-friendly,
    # indexable by ``@>``), SQLite gets text-stored JSON.
    json_type = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")

    # ``products`` — native TEXT[] on PG (GIN-indexed containment),
    # JSON-text array on SQLite (no native ARRAY type; the GIN index is
    # also skipped there).
    products_type = sa.JSON().with_variant(postgresql.ARRAY(sa.Text()), "postgresql")

    op.create_table(
        "doc_collections",
        sa.Column(
            "id",
            sa.Uuid(),
            primary_key=True,
            nullable=False,
            server_default=sa.text("gen_random_uuid()") if is_postgres else None,
        ),
        # tenant_id NULL → global/shared collection; non-null →
        # tenant-curated. No FK to tenant.id by the soft-FK discipline.
        sa.Column("tenant_id", sa.Uuid(), nullable=True),
        sa.Column("collection_key", sa.Text(), nullable=False),
        sa.Column("vendor", sa.Text(), nullable=False),
        # ``products`` — NOT NULL, empty-array default. TEXT[] on PG,
        # JSON array on SQLite. [] means "no products listed".
        sa.Column(
            "products",
            products_type,
            nullable=False,
            server_default=(sa.text("'{}'::text[]") if is_postgres else sa.text("'[]'")),
        ),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("when_to_use", sa.Text(), nullable=True),
        # Operator-set {type, ref} backend routing record (the T2 router
        # key). NOT NULL with no server_default — every collection must
        # bind to exactly one backend, so a writer has to supply
        # ``{type, ref}`` explicitly. Unlike ``products`` / ``extras``
        # (empty is valid there), an empty ``backend`` is a routing-broken
        # row, so there is no silent ``{}`` fallback. The table is new, so
        # there are no existing rows to backfill.
        sa.Column(
            "backend",
            json_type,
            nullable=False,
        ),
        # Lifecycle enum — bounded by the CHECK constraint below.
        sa.Column(
            "status",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'provisioning'"),
        ),
        # Probe-written liveness (T6 #1555); NULL until the first probe.
        sa.Column("last_ingested_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("doc_count", sa.Integer(), nullable=True),
        sa.Column("readiness", json_type, nullable=True),
        sa.Column(
            "extras",
            json_type,
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
        # Bounded-enum enforcement at the DB layer. Extend the IN(...)
        # list when the lifecycle gains new states.
        sa.CheckConstraint(
            "status IN ('provisioning', 'ready', 'rebuilding', 'disabled')",
            name="ck_doc_collections_status",
        ),
    )

    # Partial unique indexes — see module docstring for the rationale
    # (NULL != NULL in SQL UNIQUE; global rows need their own scope,
    # tenant rows need theirs). The operation_group pattern from 0005.
    op.create_index(
        "doc_collections_global_idx",
        "doc_collections",
        ["collection_key"],
        unique=True,
        postgresql_where=sa.text("tenant_id IS NULL"),
        sqlite_where=sa.text("tenant_id IS NULL"),
    )
    op.create_index(
        "doc_collections_tenant_idx",
        "doc_collections",
        ["tenant_id", "collection_key"],
        unique=True,
        postgresql_where=sa.text("tenant_id IS NOT NULL"),
        sqlite_where=sa.text("tenant_id IS NOT NULL"),
    )

    # GIN index on ``products`` — PostgreSQL only. SQLite has no GIN
    # support and cannot index a JSON/TEXT[] column with GIN semantics;
    # skipping on non-PG dialects keeps the migration dialect-portable.
    if is_postgres:
        op.create_index(
            "doc_collections_products_gin_idx",
            "doc_collections",
            ["products"],
            postgresql_using="gin",
        )


def downgrade() -> None:
    """Reverse the upgrade — drop everything in reverse order.

    Symmetric inverse of :func:`upgrade`. Indexes are dropped explicitly
    so the reversal is clean on SQLite (which does not always cascade
    indexes on ``drop_table``) as well as PostgreSQL.
    """
    bind = op.get_bind()
    is_postgres = bind.dialect.name == "postgresql"

    if is_postgres:
        op.drop_index("doc_collections_products_gin_idx", table_name="doc_collections")

    op.drop_index("doc_collections_tenant_idx", table_name="doc_collections")
    op.drop_index("doc_collections_global_idx", table_name="doc_collections")
    op.drop_table("doc_collections")
