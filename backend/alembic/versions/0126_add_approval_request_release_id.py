# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Add ``approval_request.release_id`` (#5030).

Revision ID: 0126
Revises: 0125
Create Date: 2026-10-27

One nullable column: the MEHO release an approval request was made under.
A person approves one exact release. When MEHO starts with a different
release, every request that still waits from an older release expires,
and an approved request from an older release is refused before it runs.

Rows saved before this migration keep NULL. NULL counts as an older
release, so the first start with this change ends them too. An older
build never reads the column, so a rollback only loses the binding.

``downgrade()`` drops the column.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0126"
down_revision: str | None = "0125"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "approval_request"
_COLUMN = "release_id"


def upgrade() -> None:
    """Add the nullable ``release_id`` column."""
    with op.batch_alter_table(_TABLE) as batch:
        batch.add_column(sa.Column(_COLUMN, sa.Text(), nullable=True))


def downgrade() -> None:
    """Drop the ``release_id`` column."""
    with op.batch_alter_table(_TABLE) as batch:
        batch.drop_column(_COLUMN)
