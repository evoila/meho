# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Add runner columns to ``sensor`` and an index on ``runner_check_results``.

Revision ID: 0185
Revises: 0184
Create Date: 2026-11-23

``sensor.runner_name`` names the runner that runs a check at a site. It is
NULL for a check that the central check loop runs. Every older row keeps
NULL.

``sensor.runner_produced_at`` holds the runner's own time of the last result
counted for the check. It is NULL until a runner result counts.

The index on ``runner_check_results.received_at`` serves the clean-up job
that removes raw runner results older than the retention window.

Reversibility
-------------

Purely additive. An older image never reads the column, so a rollback is
safe. ``downgrade()`` drops the index and the columns.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0185"
down_revision: str | None = "0184"
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

_SENSOR_TABLE = "sensor"
_COLUMN = "runner_name"
_PRODUCED_COLUMN = "runner_produced_at"
_RESULTS_TABLE = "runner_check_results"
_INDEX = "runner_check_results_received_at_idx"


def upgrade() -> None:
    """Add the runner column and the clean-up index."""
    op.add_column(_SENSOR_TABLE, sa.Column(_COLUMN, sa.Text(), nullable=True))
    op.add_column(
        _SENSOR_TABLE,
        sa.Column(_PRODUCED_COLUMN, sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(_INDEX, _RESULTS_TABLE, ["received_at"])


def downgrade() -> None:
    """Drop the clean-up index and the runner column."""
    op.drop_index(_INDEX, table_name=_RESULTS_TABLE)
    op.drop_column(_SENSOR_TABLE, _PRODUCED_COLUMN)
    op.drop_column(_SENSOR_TABLE, _COLUMN)
