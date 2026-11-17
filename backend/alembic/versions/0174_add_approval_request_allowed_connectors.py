# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Save a hosted agent's connector list on its approval request.

Revision ID: 0174
Revises: 0173
Create Date: 2026-11-17

A hosted agent with a connector list may use only targets of those
connectors. When its call waits for approval, the request now stores the
list (``allowed_connectors``). The run after the approval binds it again,
also when a person approves the request after the agent stopped waiting,
so the call and the steps of a multi-step operation keep the same limit.
This migration adds the nullable column. Rows that exist already keep
``NULL``: their run has no connector list.

An older image never reads the column, so a rollback is safe.
``downgrade()`` drops it.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0174"
down_revision: str | None = "0173"
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
    """Add the nullable ``allowed_connectors`` column."""
    op.add_column("approval_request", sa.Column("allowed_connectors", sa.JSON(), nullable=True))


def downgrade() -> None:
    """Drop the column."""
    op.drop_column("approval_request", "allowed_connectors")
