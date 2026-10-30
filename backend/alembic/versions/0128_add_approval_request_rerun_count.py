# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Add ``approval_request.rerun_count`` (#5417).

Revision ID: 0128
Revises: 0127
Create Date: 2026-10-30

One column: how many times the requester ran an approved request again
after its run failed. ``0`` means the request ran once, or not yet. Every
re-run adds one in the same statement that takes the re-run claim, so two
re-runs sent at the same time cannot both run.

Existing rows get ``0``. An older build never reads the column, so a
rollback only loses the count.

``downgrade()`` drops the column.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0128"
down_revision: str | None = "0127"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "approval_request"
_COLUMN = "rerun_count"


def upgrade() -> None:
    """Add the ``rerun_count`` column, ``0`` for every existing row."""
    with op.batch_alter_table(_TABLE) as batch:
        batch.add_column(
            sa.Column(_COLUMN, sa.Integer(), nullable=False, server_default=sa.text("0"))
        )


def downgrade() -> None:
    """Drop the ``rerun_count`` column."""
    with op.batch_alter_table(_TABLE) as batch:
        batch.drop_column(_COLUMN)
