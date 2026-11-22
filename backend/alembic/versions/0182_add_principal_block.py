# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Add the ``principal_block`` table.

Revision ID: 0182
Revises: 0181
Create Date: 2026-11-21

One row for each person an admin blocked in MEHO. ``sub`` is the login id.
``scope`` is ``*`` for a block of a platform admin (every tenant), or the
tenant id for a block of a tenant admin. MEHO reads the table with a cache
of a few seconds and refuses every request of a blocked login.

Only adds a table. An older image never reads it, so a rollback is safe.
``downgrade()`` drops it.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0182"
down_revision: str | None = "0181"
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


def upgrade() -> None:
    """Create the table."""
    op.create_table(
        "principal_block",
        sa.Column("sub", sa.Text(), nullable=False),
        sa.Column("scope", sa.Text(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=True),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("blocked_by_sub", sa.Text(), nullable=False),
        sa.Column("blocked_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("sub", "scope"),
    )


def downgrade() -> None:
    """Drop the table."""
    op.drop_table("principal_block")
