# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Add the one-time enrollment code columns to ``runner_principal``.

Revision ID: 0185
Revises: 0184
Create Date: 2026-11-23

Four nullable columns on ``runner_principal``:

* ``enrollment_code_hash`` -- the SHA-256 of the one-time code that
  ``meho runner-principal register`` prints. The code itself is never stored.
  The column is emptied when the runner is revoked.
* ``enrollment_code_expires_at`` -- when the code stops working.
* ``enrolled_key_id`` -- the id of the public key that the code enrolled. The
  code is used when this is set.
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
    """Add the enrollment columns."""
    op.add_column("runner_principal", sa.Column("enrollment_code_hash", sa.Text(), nullable=True))
    op.add_column(
        "runner_principal",
        sa.Column("enrollment_code_expires_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column("runner_principal", sa.Column("enrolled_key_id", sa.Text(), nullable=True))
    op.add_column(
        "runner_principal",
        sa.Column("key_enrolled_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    """Drop the enrollment columns."""
    op.drop_column("runner_principal", "key_enrolled_at")
    op.drop_column("runner_principal", "enrolled_key_id")
    op.drop_column("runner_principal", "enrollment_code_expires_at")
    op.drop_column("runner_principal", "enrollment_code_hash")
