# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Let an operator mark an approved run with an unknown outcome as checked (#5026).

Revision ID: 0142
Revises: 0141
Create Date: 2026-11-07

Six nullable columns on ``approval_request`` hold what an operator found
when they checked the target of an approved run whose outcome MEHO does not
know: ``outcome_checked_at``, ``outcome_checked_by``,
``outcome_checked_by_name``, ``outcome_check_verdict``,
``outcome_check_note`` and ``outcome_check_reason`` (why the request was on
the list when it was checked). One more nullable column,
``rerun_started_at``, holds when the latest re-run started, so the list
counts its minutes from the start of the latest run. Every existing row
reads NULL ("not checked", "no re-run start recorded").

Two CHECKs allow only known values (NULL passes):
``ck_approval_request_outcome_check_verdict`` (``happened``,
``did_not_happen``) and ``ck_approval_request_outcome_check_reason``
(``uncertain``, ``started_no_result``). The columns and the CHECKs are
added inside ``batch_alter_table``, so SQLite rebuilds the table and
PostgreSQL runs plain ``ALTER TABLE``. Same pattern as migration ``0119``.

``downgrade()`` drops the CHECKs and the seven columns. That loses the
recorded checks; the audit rows of the checks stay.

The value tuples are written out here, not imported from the models, so the
migration keeps working when the models change later.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0142"
down_revision: str | None = "0141"
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
_REASON_CONSTRAINT = "ck_approval_request_outcome_check_reason"

#: The verdicts an operator can record.
_VERDICTS: tuple[str, ...] = ("happened", "did_not_happen")

#: Why a checked request was on the list.
_REASONS: tuple[str, ...] = ("uncertain", "started_no_result")


def _in(column: str, values: tuple[str, ...]) -> str:
    quoted = ", ".join(f"'{v}'" for v in values)
    return f"{column} IN ({quoted})"


def upgrade() -> None:
    """Add the check columns, the re-run start column and the two CHECKs."""
    with op.batch_alter_table(_TABLE) as batch_op:
        batch_op.add_column(
            sa.Column("outcome_checked_at", sa.DateTime(timezone=True), nullable=True)
        )
        batch_op.add_column(sa.Column("outcome_checked_by", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("outcome_checked_by_name", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("outcome_check_verdict", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("outcome_check_note", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("outcome_check_reason", sa.Text(), nullable=True))
        batch_op.add_column(
            sa.Column("rerun_started_at", sa.DateTime(timezone=True), nullable=True)
        )
        batch_op.create_check_constraint(_CONSTRAINT, _in("outcome_check_verdict", _VERDICTS))
        batch_op.create_check_constraint(_REASON_CONSTRAINT, _in("outcome_check_reason", _REASONS))


def downgrade() -> None:
    """Drop the two CHECKs, the re-run start column and the check columns."""
    with op.batch_alter_table(_TABLE) as batch_op:
        batch_op.drop_constraint(_REASON_CONSTRAINT, type_="check")
        batch_op.drop_constraint(_CONSTRAINT, type_="check")
        batch_op.drop_column("rerun_started_at")
        batch_op.drop_column("outcome_check_reason")
        batch_op.drop_column("outcome_check_note")
        batch_op.drop_column("outcome_check_verdict")
        batch_op.drop_column("outcome_checked_by_name")
        batch_op.drop_column("outcome_checked_by")
        batch_op.drop_column("outcome_checked_at")
