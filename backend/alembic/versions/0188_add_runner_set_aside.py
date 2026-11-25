# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Add the set-aside count and time to ``runner_assignments``.

Revision ID: 0188
Revises: 0187
Create Date: 2026-11-25

``set_aside_count`` is the number of result batches a runner has set aside,
as the runner last said. ``set_aside_last_at`` is the time the runner set the
newest one aside. Every older row gets 0 and NULL.

Reversibility
-------------

Purely additive. An older image never reads the columns, so a rollback is
safe. ``downgrade()`` drops them.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0188"
down_revision: str | None = "0187"
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

_TABLE = "runner_assignments"


def upgrade() -> None:
    """Add the count and the time."""
    op.add_column(
        _TABLE,
        sa.Column("set_aside_count", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(_TABLE, sa.Column("set_aside_last_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    """Drop the count and the time."""
    op.drop_column(_TABLE, "set_aside_last_at")
    op.drop_column(_TABLE, "set_aside_count")
