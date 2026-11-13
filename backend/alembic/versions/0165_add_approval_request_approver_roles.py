# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Save who may approve each approval request.

Revision ID: 0165
Revises: 0164
Create Date: 2026-11-13

With approver routing on, a request that starts to wait stores the login
group kinds that may approve it (``approver_roles``) and the routing entry
that set them (``approver_rule``). This migration adds the two nullable
columns. Rows that exist already keep ``NULL``: today's rule decides who
may approve them.

Purely additive. An older image never reads the columns, so a rollback is
safe. ``downgrade()`` drops the columns.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0165"
down_revision: str | None = "0164"
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
    """Add the nullable ``approver_roles`` and ``approver_rule`` columns."""
    op.add_column("approval_request", sa.Column("approver_roles", sa.JSON(), nullable=True))
    op.add_column("approval_request", sa.Column("approver_rule", sa.Text(), nullable=True))


def downgrade() -> None:
    """Drop the two columns."""
    op.drop_column("approval_request", "approver_rule")
    op.drop_column("approval_request", "approver_roles")
