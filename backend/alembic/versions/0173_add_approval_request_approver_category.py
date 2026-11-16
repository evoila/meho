# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Save the categories that approver routing used for each approval request.

Revision ID: 0173
Revises: 0172
Create Date: 2026-11-16

With approver routing on, a request that starts to wait now also stores
its category (``approver_category``) and the category that the install's
policy alone gives it (``approver_install_category``). A tenant can move
an operation to another category for its own rules. At the approve click
MEHO routes the request again with the install's current routing and the
install's category: the install's routing is a floor that a tenant can
only narrow. This migration adds the two nullable columns. Rows that
exist already and have stored approver groups (``approver_roles``) get
the empty text in both: MEHO does not know their categories. At the click
such a request is refused when an install entry that matches on a
category could fit it. The other rows keep ``NULL``: they were not routed
and keep today's rule.

An older image never reads the columns, so a rollback is safe.
``downgrade()`` drops them.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0173"
down_revision: str | None = "0172"
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


_COLUMNS = ("approver_category", "approver_install_category")


def upgrade() -> None:
    """Add the two nullable category columns; routed rows get the empty text."""
    for name in _COLUMNS:
        op.add_column("approval_request", sa.Column(name, sa.Text(), nullable=True))
    op.execute(
        sa.text(
            "UPDATE approval_request SET approver_category = '', approver_install_category = '' "
            "WHERE approver_roles IS NOT NULL"
        )
    )


def downgrade() -> None:
    """Drop the two columns."""
    for name in reversed(_COLUMNS):
        op.drop_column("approval_request", name)
