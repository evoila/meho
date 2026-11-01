# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Add ``approval_request.target_hash``.

Revision ID: 0130
Revises: 0129
Create Date: 2026-10-31

One nullable column: a SHA-256 over the target's connection settings when
the request was made. An approved run compares it with the target as it is
now and does not run when the two differ.

Existing rows keep NULL. MEHO skips the check for them, as before. An older
build never reads the column, so a rollback only loses the check.

``downgrade()`` drops the column.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0130"
down_revision: str | None = "0129"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "approval_request"
_COLUMN = "target_hash"


def upgrade() -> None:
    """Add the nullable ``target_hash`` column."""
    with op.batch_alter_table(_TABLE) as batch:
        batch.add_column(sa.Column(_COLUMN, sa.Text(), nullable=True))


def downgrade() -> None:
    """Drop the ``target_hash`` column."""
    with op.batch_alter_table(_TABLE) as batch:
        batch.drop_column(_COLUMN)
