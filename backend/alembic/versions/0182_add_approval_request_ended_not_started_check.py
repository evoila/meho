# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""A request that ended without a run can never be started.

Revision ID: 0182
Revises: 0181
Create Date: 2026-11-23

One CHECK on ``approval_request``:
``ck_approval_request_ended_not_started``. A row whose status is
``rejected``, ``expired`` or ``withdrawn`` keeps ``resumed_at`` NULL. Every
MEHO release starts a run by setting ``resumed_at``. So the database itself
refuses the start of a request that has ended, whatever release asks for it.

On PostgreSQL the CHECK is added ``NOT VALID``: MEHO checks every new write
at once, and the upgrade does not stop on an old row. SQLite rebuilds the
table inside ``batch_alter_table``, the same pattern as migration ``0142``.

``downgrade()`` drops the CHECK.

The status values are written out here, not imported from the models, so
the migration keeps working when the models change later.
"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0182"
down_revision: str | None = "0181"
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

_TABLE = "approval_request"
_CONSTRAINT = "ck_approval_request_ended_not_started"
_CONDITION = "status NOT IN ('rejected', 'expired', 'withdrawn') OR resumed_at IS NULL"


def upgrade() -> None:
    """Add the CHECK that keeps ``resumed_at`` empty on a request that ended."""
    with op.batch_alter_table(_TABLE) as batch_op:
        batch_op.create_check_constraint(_CONSTRAINT, _CONDITION, postgresql_not_valid=True)


def downgrade() -> None:
    """Drop the CHECK."""
    with op.batch_alter_table(_TABLE) as batch_op:
        batch_op.drop_constraint(_CONSTRAINT, type_="check")
