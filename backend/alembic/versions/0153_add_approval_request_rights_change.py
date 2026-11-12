# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Save the rights-change mark on each approval request.

Revision ID: 0153
Revises: 0152
Create Date: 2026-11-09

A request that changes who has rights can never be approved by the person
who asked for it, whatever their group. This migration adds one nullable
boolean column, ``rights_change``, which holds the mark. Rows that exist
already keep ``NULL``.

Purely additive. An older image never reads the column, so a rollback is
safe. ``downgrade()`` drops the column.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0153"
down_revision: str | None = "0152"
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
    """Add the nullable ``rights_change`` column."""
    op.add_column("approval_request", sa.Column("rights_change", sa.Boolean(), nullable=True))


def downgrade() -> None:
    """Drop the ``rights_change`` column."""
    op.drop_column("approval_request", "rights_change")
