# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Add the operation policy override table (#4711).

Revision ID: 0120
Revises: 0119
Create Date: 2026-10-28

One additive table, ``operation_policy_override``: an operator's stricter
risk level or approval flag for one operation, kept apart from the value
in code or in the API description. A start or a new ingest writes only
``endpoint_descriptor``, so the operator's choice stays. ``tenant_id``
``NULL`` is a change for the whole install; a tenant UUID is a change for
that tenant only. Two partial unique indexes keep one row per scope and
operation name (the same shape ``endpoint_descriptor`` uses).

An install without rows behaves as before. An older build never reads the
table, so after a rollback the values in code apply again.

``downgrade()`` drops the table.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0120"
down_revision: str | None = "0119"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "operation_policy_override"


def upgrade() -> None:
    """Create the override table and its two unique indexes."""
    op.create_table(
        _TABLE,
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=True),
        sa.Column("product", sa.Text(), nullable=False),
        sa.Column("version", sa.Text(), nullable=False),
        sa.Column("impl_id", sa.Text(), nullable=False),
        sa.Column("op_id", sa.Text(), nullable=False),
        sa.Column("safety_level", sa.Text(), nullable=True),
        sa.Column("requires_approval", sa.Boolean(), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("set_by_sub", sa.Text(), nullable=False),
        sa.Column("set_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "safety_level IS NULL OR safety_level IN "
            "('safe', 'caution', 'dangerous', 'destructive')",
            name="ck_operation_policy_override_safety_level",
        ),
    )
    op.create_index(
        "operation_policy_override_install_idx",
        _TABLE,
        ["product", "version", "impl_id", "op_id"],
        unique=True,
        postgresql_where=sa.text("tenant_id IS NULL"),
        sqlite_where=sa.text("tenant_id IS NULL"),
    )
    op.create_index(
        "operation_policy_override_tenant_idx",
        _TABLE,
        ["tenant_id", "product", "version", "impl_id", "op_id"],
        unique=True,
        postgresql_where=sa.text("tenant_id IS NOT NULL"),
        sqlite_where=sa.text("tenant_id IS NOT NULL"),
    )


def downgrade() -> None:
    """Drop the override table (its indexes go with it)."""
    op.drop_table(_TABLE)
