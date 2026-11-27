# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Add ``hard_to_undo`` and ``changes_rights`` to ``endpoint_descriptor``.

Revision ID: 0190
Revises: 0189
Create Date: 2026-11-26

Two nullable columns that an operation's own definition in code fills:

* ``hard_to_undo`` -- ``deletes``, ``stops`` or ``overwrites`` when a call
  can do that and no later call of the same connector undoes it. A CHECK
  allows only these three words. ``NULL`` means no.
* ``changes_rights`` -- true when a call gives or takes rights, a login or
  an access policy in the target system. ``NULL`` reads as no.

Existing rows start as ``NULL``. The next start writes the values of the
typed and composite operations again. An older build never reads the
columns, so a rollback is safe.

SQLite adds the CHECK by rebuilding the table inside ``batch_alter_table``,
the same pattern as migration ``0184``. Every row is ``NULL``, so the CHECK
holds for every old row.

``downgrade()`` drops the CHECK and both columns.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0190"
down_revision: str | None = "0189"
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

_TABLE = "endpoint_descriptor"
_CHECK = "ck_endpoint_descriptor_hard_to_undo"
_CONDITION = "hard_to_undo IN ('deletes', 'stops', 'overwrites')"


def upgrade() -> None:
    """Add both columns and the CHECK on ``hard_to_undo``."""
    with op.batch_alter_table(_TABLE) as batch_op:
        batch_op.add_column(sa.Column("hard_to_undo", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("changes_rights", sa.Boolean(), nullable=True))
        batch_op.create_check_constraint(_CHECK, _CONDITION)


def downgrade() -> None:
    """Drop the CHECK and both columns."""
    with op.batch_alter_table(_TABLE) as batch_op:
        batch_op.drop_constraint(_CHECK, type_="check")
        batch_op.drop_column("changes_rights")
        batch_op.drop_column("hard_to_undo")
