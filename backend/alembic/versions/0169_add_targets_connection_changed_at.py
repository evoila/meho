# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Add ``targets.connection_changed_at``.

Revision ID: 0169
Revises: 0168
Create Date: 2026-11-15

One nullable timestamp column: when a person last changed the target. A
probe does not move it, so a probe does not make MEHO sign in again.
Rows that exist already get the value of ``updated_at``.

Only adds a column. An older image never reads it, so a rollback is
safe. ``downgrade()`` drops it.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0169"
down_revision: str | None = "0168"
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
    """Add the column and fill it from ``updated_at``."""
    op.add_column(
        "targets",
        sa.Column("connection_changed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.execute("UPDATE targets SET connection_changed_at = updated_at")


def downgrade() -> None:
    """Drop the column."""
    op.drop_column("targets", "connection_changed_at")
