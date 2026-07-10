# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Create the ``spec_provenance`` table (#2291).

Revision ID: 0056
Revises: 0055
Create Date: 2026-07-10

Initiative #2270 (ingest spec hygiene), Task #2291. Persists durable,
non-spoofable provenance for every accepted spec ingest so downstream
reasoning (dispatch debugging, re-ingest, upgrade reconciliation) can
tell a vendor artifact from a hand-mutated one.

Before this table the only per-row provenance was the spoofable
``spec:<uri>`` tag on ``endpoint_descriptor``: an operator's
hand-mutated inline upload labelled with a vendor's ``https`` URL
persisted identically to a genuine fetch of that URL, and the
fetched-vs-inline bit was never persisted anywhere. A single spec fans
out to hundreds of descriptor rows, so provenance is a spec-level table,
not per-descriptor columns.

Columns
-------

* ``uri`` — the audit label exactly as presented (``spec:`` /
  ``https://`` / ``file:///`` / ``docs:`` form preserved).
* ``sha256`` — hex digest over the raw spec bytes (fetched body or
  uploaded content), computed at the ``_load_spec_bytes`` trust boundary
  before any decode.
* ``origin`` — ``fetched`` | ``inline`` | ``shipped`` (bounded via a
  portable ``CHECK`` constraint, same discipline as
  ``ck_endpoint_descriptor_source_kind`` in ``0005``).
* ``operator_sub`` — the ingesting operator's subject claim; nullable
  for boot-time shipped ingests with no operator.
* ``ingested_at`` — UTC time the row was last written; refreshed on
  re-ingest.

Unique-constraint shape — partial unique indexes
------------------------------------------------

The natural key is ``(tenant_id, product, version, impl_id, uri)`` with
``tenant_id IS NULL`` for built-in/global ingests. A single
``UNIQUE (tenant_id, ...)`` would not enforce "two global ingests of the
same spec collide" — NULL != NULL under SQL UNIQUE. Mirrors
``0005``'s ``endpoint_descriptor`` split: two partial unique indexes,
one ``WHERE tenant_id IS NULL`` and one ``WHERE tenant_id IS NOT NULL``.
SQLite has supported partial indexes since 3.8.0 (we're on 3.45+);
SQLAlchemy emits the ``WHERE`` for both dialects via the
``postgresql_where`` / ``sqlite_where`` pair.

Dialect-portability decisions
-----------------------------

Mirrors ``0005``. ``id`` gets a ``gen_random_uuid()`` server default on
PG (SQLite leaves it to the ORM ``default=uuid.uuid4``); ``ingested_at``
gets ``now()`` on PG (SQLite leaves it to the ORM default). No JSON,
vector, or PG-only index — the table is plain text + timestamp columns,
so the migration is symmetric on both dialects.

Reversibility contract
----------------------

``downgrade()`` drops the two partial indexes explicitly (SQLite does
not always cascade indexes on ``drop_table``) then the table. Purely
additive on the way up (no ``DROP`` / destructive DDL in ``upgrade()``),
so the migration-compat CI guard passes.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0056"
down_revision: str | None = "0055"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the ``spec_provenance`` table + its two partial unique indexes."""
    bind = op.get_bind()
    is_postgres = bind.dialect.name == "postgresql"

    op.create_table(
        "spec_provenance",
        sa.Column(
            "id",
            sa.Uuid(),
            primary_key=True,
            nullable=False,
            server_default=sa.text("gen_random_uuid()") if is_postgres else None,
        ),
        # tenant_id NULL → built-in/global ingest; non-null → tenant-scoped.
        # Soft-FK discipline (no FK to tenant.id), same as 0002/0005.
        sa.Column("tenant_id", sa.Uuid(), nullable=True),
        sa.Column("product", sa.Text(), nullable=False),
        sa.Column("version", sa.Text(), nullable=False),
        sa.Column("impl_id", sa.Text(), nullable=False),
        sa.Column("uri", sa.Text(), nullable=False),
        sa.Column("sha256", sa.Text(), nullable=False),
        sa.Column("origin", sa.Text(), nullable=False),
        sa.Column("operator_sub", sa.Text(), nullable=True),
        sa.Column(
            "ingested_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()") if is_postgres else None,
        ),
        sa.CheckConstraint(
            "origin IN ('fetched', 'inline', 'shipped')",
            name="ck_spec_provenance_origin",
        ),
    )

    # Partial unique indexes — see module docstring (NULL != NULL under
    # SQL UNIQUE; global and tenant rows need separate scopes).
    op.create_index(
        "spec_provenance_global_idx",
        "spec_provenance",
        ["product", "version", "impl_id", "uri"],
        unique=True,
        postgresql_where=sa.text("tenant_id IS NULL"),
        sqlite_where=sa.text("tenant_id IS NULL"),
    )
    op.create_index(
        "spec_provenance_tenant_idx",
        "spec_provenance",
        ["tenant_id", "product", "version", "impl_id", "uri"],
        unique=True,
        postgresql_where=sa.text("tenant_id IS NOT NULL"),
        sqlite_where=sa.text("tenant_id IS NOT NULL"),
    )


def downgrade() -> None:
    """Drop the ``spec_provenance`` indexes then the table (reverse order)."""
    op.drop_index("spec_provenance_tenant_idx", table_name="spec_provenance")
    op.drop_index("spec_provenance_global_idx", table_name="spec_provenance")
    op.drop_table("spec_provenance")
