# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Add the login facts to ``push_subscription``.

Revision ID: 0161
Revises: 0160
Create Date: 2026-11-12

MEHO sends a push message when a request waits for approval, only to the
people who may approve it. The login groups live in the login token, not in
the database. So each subscription keeps the person's login facts in its
tenant, as read from their login: ``tenant_role``, ``approver``,
``self_approver`` and ``platform_admin``, and when they were read
(``facts_read_at``). MEHO reads them again at each console sign-in and
each time the account page opens, and sends nothing to a row whose facts
are more than 7 days old. Rows made before this migration get
``tenant_role`` and ``facts_read_at`` ``NULL``, so they get no message
until the person signs in again. ``downgrade()`` drops the columns.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0161"
down_revision: str | None = "0160"
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
_FLAGS = ("approver", "self_approver", "platform_admin")


def upgrade() -> None:
    """Add the login fact columns."""
    op.add_column(_TABLE, sa.Column("tenant_role", sa.Text(), nullable=True))
    for flag in _FLAGS:
        op.add_column(
            _TABLE, sa.Column(flag, sa.Boolean(), nullable=False, server_default=sa.false())
        )
    op.add_column(_TABLE, sa.Column("facts_read_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    """Drop the login fact columns."""
    with op.batch_alter_table(_TABLE) as batch:
        batch.drop_column("facts_read_at")
        for flag in reversed(_FLAGS):
            batch.drop_column(flag)
        batch.drop_column("tenant_role")
