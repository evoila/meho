# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Create the ``ingested_auth_profile`` table (#5016).

Revision ID: 0121
Revises: 0120
Create Date: 2026-10-27

One additive table. It saves the login type (``auth_scheme``) and the
secret field names an operator chose with ``meho connector ingest
--auth-scheme``. Before this table that choice lived only in memory, so a
connector made this way lost its connector class at every restart and
every call failed with ``no_connector``. At start the backplane reads these
rows and builds the same connector class again.

Natural key: ``(tenant_id, product, version, impl_id)`` with
``tenant_id IS NULL`` for a built-in connector. NULL != NULL under SQL
UNIQUE, so two partial unique indexes, the same shape as
``0056_create_spec_provenance``.

An older build never reads the table, so a rollback only brings back the
old behaviour (the class is lost at restart). ``downgrade()`` drops the
two indexes and then the table.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0121"
down_revision: str | None = "0120"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "ingested_auth_profile"
_GLOBAL_IDX = "ingested_auth_profile_global_idx"
_TENANT_IDX = "ingested_auth_profile_tenant_idx"


def upgrade() -> None:
    """Create the table and its two partial unique indexes."""
    op.create_table(
        _TABLE,
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=True),
        sa.Column("product", sa.Text(), nullable=False),
        sa.Column("version", sa.Text(), nullable=False),
        sa.Column("impl_id", sa.Text(), nullable=False),
        sa.Column("auth_scheme", sa.Text(), nullable=False),
        sa.Column(
            "secret_fields",
            sa.JSON().with_variant(postgresql.JSONB(), "postgresql"),
            nullable=False,
        ),
        sa.Column("operator_sub", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        _GLOBAL_IDX,
        _TABLE,
        ["product", "version", "impl_id"],
        unique=True,
        postgresql_where=sa.text("tenant_id IS NULL"),
        sqlite_where=sa.text("tenant_id IS NULL"),
    )
    op.create_index(
        _TENANT_IDX,
        _TABLE,
        ["tenant_id", "product", "version", "impl_id"],
        unique=True,
        postgresql_where=sa.text("tenant_id IS NOT NULL"),
        sqlite_where=sa.text("tenant_id IS NOT NULL"),
    )


def downgrade() -> None:
    """Drop the two indexes and the table."""
    op.drop_index(_TENANT_IDX, table_name=_TABLE)
    op.drop_index(_GLOBAL_IDX, table_name=_TABLE)
    op.drop_table(_TABLE)
