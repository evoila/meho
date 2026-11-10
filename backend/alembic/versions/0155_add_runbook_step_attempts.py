# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Add ``runbook_run_step_states.attempts``.

Revision ID: 0155
Revises: 0154
Create Date: 2026-11-10

One column: the list of check attempts for a step. Each entry holds the
time of the attempt, whether it passed, and the result of the check.
A failed step can be tried again, and the earlier failed result must
stay visible, so the list only grows.

Existing rows get NULL. An older build never reads the column, so a
rollback only loses the list.

``downgrade()`` drops the column.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0155"
down_revision: str | None = "0154"
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

_TABLE = "runbook_run_step_states"
_COLUMN = "attempts"


def upgrade() -> None:
    """Add the nullable ``attempts`` JSON column."""
    with op.batch_alter_table(_TABLE) as batch:
        batch.add_column(sa.Column(_COLUMN, sa.JSON(), nullable=True))


def downgrade() -> None:
    """Drop the ``attempts`` column."""
    with op.batch_alter_table(_TABLE) as batch:
        batch.drop_column(_COLUMN)
