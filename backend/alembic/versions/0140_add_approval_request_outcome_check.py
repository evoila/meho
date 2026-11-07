# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Let an operator mark an approved run with an unknown outcome as checked (#5026).

Revision ID: 0140
Revises: 0139
Create Date: 2026-11-07

Five nullable columns on ``approval_request`` hold what an operator found
when they checked the target of an approved run whose outcome MEHO does not
know: ``outcome_checked_at``, ``outcome_checked_by``,
``outcome_checked_by_name``, ``outcome_check_verdict`` and
``outcome_check_note``. Every existing row reads NULL ("not checked").

A CHECK ``ck_approval_request_outcome_check_verdict`` allows only
``happened`` and ``did_not_happen`` (NULL passes). The columns and the
CHECK are added inside ``batch_alter_table``, so SQLite rebuilds the table
and PostgreSQL runs plain ``ALTER TABLE``. Same pattern as migration
``0119``.

``downgrade()`` drops the CHECK and the five columns. That loses the
recorded checks; the audit rows of the checks stay.

The value tuple is written out here, not imported from the models, so the
migration keeps working when the models change later.
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

_TABLE = "approval_request"
_CONSTRAINT = "ck_approval_request_outcome_check_verdict"

#: The verdicts an operator can record.
_VERDICTS: tuple[str, ...] = ("happened", "did_not_happen")


def upgrade() -> None:
    """Add the five check columns and the verdict CHECK."""
    quoted = ", ".join(f"'{v}'" for v in _VERDICTS)
    with op.batch_alter_table(_TABLE) as batch_op:
        batch_op.add_column(
            sa.Column("outcome_checked_at", sa.DateTime(timezone=True), nullable=True)
        )
        batch_op.add_column(sa.Column("outcome_checked_by", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("outcome_checked_by_name", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("outcome_check_verdict", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("outcome_check_note", sa.Text(), nullable=True))
        batch_op.create_check_constraint(_CONSTRAINT, f"outcome_check_verdict IN ({quoted})")


def downgrade() -> None:
    """Drop the verdict CHECK and the five check columns."""
    with op.batch_alter_table(_TABLE) as batch_op:
        batch_op.drop_constraint(_CONSTRAINT, type_="check")
        batch_op.drop_column("outcome_check_note")
        batch_op.drop_column("outcome_check_verdict")
        batch_op.drop_column("outcome_checked_by_name")
        batch_op.drop_column("outcome_checked_by")
        batch_op.drop_column("outcome_checked_at")
