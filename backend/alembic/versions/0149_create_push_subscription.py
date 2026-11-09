# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Create ``push_subscription``.

Revision ID: 0149
Revises: 0148
Create Date: 2026-11-09

A person can switch on "Notify me about approvals" per browser on the
account page. MEHO stores the browser's push subscription here: the person,
the tenant they worked in, the push endpoint (unique: one row per browser),
its two keys, and the time. The table is new and empty, so the migration
changes no behaviour on its own. ``downgrade()`` drops the indexes and the
table.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0149"
down_revision: str | None = "0148"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Alembic reads these names itself. List them in __all__, so the code-quality
# check does not report them as unused globals. Keep a plain list of strings
# with no type annotation: the check reads only that form.
__all__ = [
    "branch_labels",
    "depends_on",
    "down_revision",
    "downgrade",
    "revision",
    "upgrade",
]

_TABLE = "push_subscription"


def upgrade() -> None:
    """Create the table and its two indexes."""
    op.create_table(
        _TABLE,
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("operator_sub", sa.Text(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("endpoint", sa.Text(), nullable=False),
        sa.Column("p256dh", sa.Text(), nullable=False),
        sa.Column("auth", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("endpoint", name="push_subscription_endpoint_key"),
    )
    op.create_index("push_subscription_operator_sub_idx", _TABLE, ["operator_sub"])
    op.create_index("push_subscription_tenant_id_idx", _TABLE, ["tenant_id"])


def downgrade() -> None:
    """Drop the indexes, then the table."""
    op.drop_index("push_subscription_tenant_id_idx", table_name=_TABLE)
    op.drop_index("push_subscription_operator_sub_idx", table_name=_TABLE)
    op.drop_table(_TABLE)
