# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Add ``sensor.auth_failure_count``.

Revision ID: 0162
Revises: 0161
Create Date: 2026-11-12

One column: how many evaluations in a row the target refused the login.
The check runner pauses a Sensor when the count reaches a limit, so a
stale credential is not tried again on every tick.

Existing rows get 0. An older build never reads the column, so a rollback
only loses the count.

``downgrade()`` drops the column.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0162"
down_revision: str | None = "0161"
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

_TABLE = "sensor"
_COLUMN = "auth_failure_count"


def upgrade() -> None:
    """Add the ``auth_failure_count`` column, 0 for every existing row."""
    with op.batch_alter_table(_TABLE) as batch:
        batch.add_column(
            sa.Column(_COLUMN, sa.Integer(), nullable=False, server_default="0"),
        )


def downgrade() -> None:
    """Drop the ``auth_failure_count`` column."""
    with op.batch_alter_table(_TABLE) as batch:
        batch.drop_column(_COLUMN)
