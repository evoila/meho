# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Add ``runner_refused_results``: the refused runner results MEHO has counted.

Revision ID: 0190
Revises: 0189
Create Date: 2026-11-26

One row per refused runner result (tenant, runner, result id). MEHO adds the
row in the same transaction in which it decides to count the result, so
every MEHO process on the database counts a refused result once. The table
starts empty.

Reversibility
-------------

Purely additive. An older image never reads the table, so a rollback is
safe. ``downgrade()`` drops it.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0190"
down_revision: str | None = "0189"
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

_TABLE = "runner_refused_results"


def upgrade() -> None:
    """Create the table and the index the clean-up job reads."""
    op.create_table(
        _TABLE,
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("runner_name", sa.Text(), nullable=False),
        sa.Column("result_uid", sa.Text(), nullable=False),
        sa.Column("refused_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("tenant_id", "runner_name", "result_uid"),
    )
    op.create_index(
        "runner_refused_results_refused_at_idx",
        _TABLE,
        ["refused_at"],
        postgresql_using="btree",
    )


def downgrade() -> None:
    """Drop the table."""
    op.drop_index("runner_refused_results_refused_at_idx", table_name=_TABLE)
    op.drop_table(_TABLE)
