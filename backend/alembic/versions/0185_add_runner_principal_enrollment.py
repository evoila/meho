# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Add the one-time enrollment code columns to ``runner_principal``.

Revision ID: 0185
Revises: 0184
Create Date: 2026-11-23

Three nullable columns on ``runner_principal``:

* ``enrollment_code_hash`` -- the SHA-256 of the one-time code that
  ``meho runner-principal register`` prints. The code itself is never stored.
  The column is emptied when the code is used.
* ``enrollment_code_expires_at`` -- when the code stops working.
* ``key_enrolled_at`` -- when the runner registered its public key.

Existing rows start as ``NULL``. ``downgrade()`` drops the columns.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0185"
down_revision: str | None = "0184"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add the enrollment columns."""
    op.add_column("runner_principal", sa.Column("enrollment_code_hash", sa.Text(), nullable=True))
    op.add_column(
        "runner_principal",
        sa.Column("enrollment_code_expires_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "runner_principal",
        sa.Column("key_enrolled_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    """Drop the enrollment columns."""
    op.drop_column("runner_principal", "key_enrolled_at")
    op.drop_column("runner_principal", "enrollment_code_expires_at")
    op.drop_column("runner_principal", "enrollment_code_hash")
