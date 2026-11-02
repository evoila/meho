# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Add ``approval_request.reveal_secret``.

Revision ID: 0131
Revises: 0130
Create Date: 2026-11-01

One column: whether the requester asked for the hidden values of the
answer (``reveal_secret: true``). The choice is not a parameter of the
operation, so it is not stored with the params and not part of the params
hash. Every approved run reads it from this column, also the re-run of a
failed credential write after the first run deleted the encrypted input.

Existing rows get ``false``: their run keeps the default view. An older
build never reads the column, so a rollback only loses the choice.

``downgrade()`` drops the column.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0131"
down_revision: str | None = "0130"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "approval_request"
_COLUMN = "reveal_secret"


def upgrade() -> None:
    """Add the ``reveal_secret`` column, ``false`` for every existing row."""
    with op.batch_alter_table(_TABLE) as batch:
        batch.add_column(
            sa.Column(_COLUMN, sa.Boolean(), nullable=False, server_default=sa.false())
        )


def downgrade() -> None:
    """Drop the ``reveal_secret`` column."""
    with op.batch_alter_table(_TABLE) as batch:
        batch.drop_column(_COLUMN)
