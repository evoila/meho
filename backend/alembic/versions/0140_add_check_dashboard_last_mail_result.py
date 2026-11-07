# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Add the last alert mail result to ``check_dashboards``.

Revision ID: 0140
Revises: 0139
Create Date: 2026-11-07

When a Dashboard's alert mail could not be sent, MEHO only wrote a log
line. These three nullable columns keep the last result on the Dashboard,
so an operator can see failing mail without reading logs:

* ``last_mail_at`` -- when the last attempt ended.
* ``last_mail_sent`` -- true when the mail was sent, false when it was not.
* ``last_mail_reason`` -- the failure reason code; empty when it was sent.

All three are ``NULL`` for a Dashboard that never tried to send a mail, so
the migration changes no behaviour on its own. ``downgrade()`` drops them.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0140"
down_revision: str | None = "0139"
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
    """Add the three nullable result columns."""
    with op.batch_alter_table("check_dashboards") as batch_op:
        batch_op.add_column(sa.Column("last_mail_at", sa.DateTime(timezone=True), nullable=True))
        batch_op.add_column(sa.Column("last_mail_sent", sa.Boolean(), nullable=True))
        batch_op.add_column(sa.Column("last_mail_reason", sa.Text(), nullable=True))


def downgrade() -> None:
    """Drop the columns added in :func:`upgrade`."""
    with op.batch_alter_table("check_dashboards") as batch_op:
        batch_op.drop_column("last_mail_reason")
        batch_op.drop_column("last_mail_sent")
        batch_op.drop_column("last_mail_at")
