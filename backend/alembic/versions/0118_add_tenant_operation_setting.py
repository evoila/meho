# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Add the per-tenant operation on/off table (#5029).

Revision ID: 0118
Revises: 0117
Create Date: 2026-10-27

One additive table, ``tenant_operation_setting``: one row per
``(tenant_id, product, version, impl_id, op_id)`` holding one tenant's own
on/off choice for one built-in operation. A tenant without a row uses the
install default (``endpoint_descriptor.is_enabled``), so an existing install
behaves as before. An older build never reads the table, so after a rollback
every tenant uses the install default again.

``downgrade()`` drops the table.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0118"
down_revision: str | None = "0117"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "tenant_operation_setting"


def upgrade() -> None:
    """Create the per-tenant operation setting table."""
    op.create_table(
        _TABLE,
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("product", sa.Text(), nullable=False),
        sa.Column("version", sa.Text(), nullable=False),
        sa.Column("impl_id", sa.Text(), nullable=False),
        sa.Column("op_id", sa.Text(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("set_by_sub", sa.Text(), nullable=False),
        sa.Column("set_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "tenant_id",
            "product",
            "version",
            "impl_id",
            "op_id",
            name="uq_tenant_operation_setting_op",
        ),
    )


def downgrade() -> None:
    """Drop the per-tenant operation setting table."""
    op.drop_table(_TABLE)
