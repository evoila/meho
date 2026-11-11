# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Add ``lesson_retrieval.channel``.

Revision ID: 0154
Revises: 0153
Create Date: 2026-11-10

One column: how the entry reached the session. ``search`` means
``search_knowledge`` or ``search_memory`` returned it. ``recall`` means
MEHO showed it in the ``team_knowledge`` block before an operation. The
learning numbers of ``meho audit reflex`` count the two apart.

Existing rows get ``search``: before this migration only the two search
tools wrote rows. An older build never reads the column, so a rollback
only loses the channel.

``downgrade()`` drops the column.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0154"
down_revision: str | None = "0153"
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

_TABLE = "lesson_retrieval"
_COLUMN = "channel"


def upgrade() -> None:
    """Add the ``channel`` column, ``search`` for every existing row."""
    with op.batch_alter_table(_TABLE) as batch:
        batch.add_column(
            sa.Column(_COLUMN, sa.Text(), nullable=False, server_default=sa.text("'search'"))
        )


def downgrade() -> None:
    """Drop the ``channel`` column."""
    with op.batch_alter_table(_TABLE) as batch:
        batch.drop_column(_COLUMN)
