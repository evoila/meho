# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Add ``push_subscription.approve_scope``.

Revision ID: 0152
Revises: 0151
Create Date: 2026-11-09

MEHO sends a push message when a request waits for approval, only to the
people who may approve it. The login groups live in the login token, not in
the database, so the subscription keeps what the person may approve, read
from their login when they switch the notifications on: ``all``,
``lower_risk`` or ``NULL`` (none). The column is nullable, so rows made
before it get ``NULL`` and no message until the person opens the account
page again. ``downgrade()`` drops the column.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0152"
down_revision: str | None = "0151"
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

_TABLE = "push_subscription"
_COLUMN = "approve_scope"


def upgrade() -> None:
    """Add the nullable column."""
    op.add_column(_TABLE, sa.Column(_COLUMN, sa.Text(), nullable=True))


def downgrade() -> None:
    """Drop the column."""
    with op.batch_alter_table(_TABLE) as batch:
        batch.drop_column(_COLUMN)
