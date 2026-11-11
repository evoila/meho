# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Store a category and labels per operation, and a policy per tenant.

Revision ID: 0158
Revises: 0157
Create Date: 2026-11-11

Each connector, operation group and operation can carry a category,
labels and an "always wait" mark. This migration only adds:

* two nullable columns on ``operation_policy_override``: ``category``
  (text) and ``labels`` (JSON, a map of free ``key=value`` text);
* a new table ``tenant_policy``: one row per tenant (and at most one for
  the whole install, ``tenant_id`` NULL) with the stored policy document.

MEHO reads none of it while the ``POLICY_CLASSIFICATION`` switch is off.
Rows that exist already keep ``NULL``. An older image never reads the new
columns or the new table, so a rollback is safe. ``downgrade()`` drops
them.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

# revision identifiers, used by Alembic.
revision: str = "0158"
down_revision: str | None = "0157"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Alembic reads these names itself. List them in __all__, so the code-quality
# check does not report them as unused globals.
__all__ = [
    "branch_labels",
    "depends_on",
    "down_revision",
    "downgrade",
    "revision",
    "upgrade",
]

_JSON = sa.JSON().with_variant(JSONB(), "postgresql")


def upgrade() -> None:
    """Add the two override columns and the ``tenant_policy`` table."""
    op.add_column("operation_policy_override", sa.Column("category", sa.Text(), nullable=True))
    op.add_column("operation_policy_override", sa.Column("labels", _JSON, nullable=True))
    op.create_table(
        "tenant_policy",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("tenant_id", sa.Uuid(), nullable=True),
        sa.Column("document", _JSON, nullable=False),
        sa.Column("set_by_sub", sa.Text(), nullable=False),
        sa.Column("set_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "tenant_policy_install_idx",
        "tenant_policy",
        [sa.text("(tenant_id IS NULL)")],
        unique=True,
        postgresql_where=sa.text("tenant_id IS NULL"),
        sqlite_where=sa.text("tenant_id IS NULL"),
    )
    op.create_index(
        "tenant_policy_tenant_idx",
        "tenant_policy",
        ["tenant_id"],
        unique=True,
        postgresql_where=sa.text("tenant_id IS NOT NULL"),
        sqlite_where=sa.text("tenant_id IS NOT NULL"),
    )


def downgrade() -> None:
    """Drop the ``tenant_policy`` table and the two override columns."""
    op.drop_index("tenant_policy_tenant_idx", table_name="tenant_policy")
    op.drop_index("tenant_policy_install_idx", table_name="tenant_policy")
    op.drop_table("tenant_policy")
    op.drop_column("operation_policy_override", "labels")
    op.drop_column("operation_policy_override", "category")
