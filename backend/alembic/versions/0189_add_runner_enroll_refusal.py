# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Add the ``runner_enroll_refusal`` table.

Revision ID: 0189
Revises: 0188
Create Date: 2026-11-25

One row per runner and refusal reason of the open enrollment route:

* ``last_row_at`` -- when the newest refusal audit row of this runner and
  reason was written.
* ``left_out`` -- the refusals since then that got no audit row of their own.
  The next audit row records this count and sets it back to zero, in the same
  transaction.

The rows go with their runner (``ON DELETE CASCADE``). ``downgrade()`` drops
the table.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0189"
down_revision: str | None = "0188"
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


def upgrade() -> None:
    """Create the table."""
    op.create_table(
        "runner_enroll_refusal",
        sa.Column(
            "runner_id",
            sa.Uuid(),
            sa.ForeignKey("runner_principal.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("reason", sa.Text(), primary_key=True),
        sa.Column("last_row_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("left_out", sa.Integer(), nullable=False, server_default=sa.text("0")),
    )


def downgrade() -> None:
    """Drop the table."""
    op.drop_table("runner_enroll_refusal")
