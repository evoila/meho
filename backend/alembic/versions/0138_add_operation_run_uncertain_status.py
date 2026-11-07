# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Add the ``uncertain`` operation run status (#5025).

Revision ID: 0138
Revises: 0137
Create Date: 2026-11-07

When the worker of an async run that changes something dies, MEHO does not
know if the change happened. The reaper now ends such a run as ``uncertain``
instead of ``failed``. A dead read run stays ``failed``.

The CHECK ``ck_operation_run_status`` gets a sixth value. Every existing row
holds one of the five old values, so no row breaks the wider rule and no row
is changed.

The CHECK is dropped and created again inside ``batch_alter_table``, so
SQLite rebuilds the table and PostgreSQL runs plain ``ALTER TABLE``. Same
pattern as migration ``0137``.

``downgrade()`` ends every ``uncertain`` run as ``failed`` first, then
narrows the rule. That loses the "unknown" mark, so stay on this revision
unless you must go back.

The value tuples are written out here, not imported from the models, so the
migration keeps working when the models change later.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0138"
down_revision: str | None = "0137"
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

_TABLE = "operation_run"
_CONSTRAINT = "ck_operation_run_status"

#: The five values migration ``0080`` allowed.
_STATUSES_BEFORE: tuple[str, ...] = (
    "pending",
    "running",
    "succeeded",
    "failed",
    "cancelled",
)

#: The six values after this migration. Kept equal to ``OperationRunStatus``
#: by the lifecycle test.
_OPERATION_RUN_STATUSES: tuple[str, ...] = (*_STATUSES_BEFORE, "uncertain")


def _check_in(values: tuple[str, ...]) -> str:
    """Render ``status IN ('a', 'b', ...)`` for the CHECK constraint."""
    quoted = ", ".join(f"'{v}'" for v in values)
    return f"status IN ({quoted})"


def upgrade() -> None:
    """Widen the status CHECK to ``uncertain``."""
    with op.batch_alter_table(_TABLE) as batch_op:
        batch_op.drop_constraint(_CONSTRAINT, type_="check")
        batch_op.create_check_constraint(_CONSTRAINT, _check_in(_OPERATION_RUN_STATUSES))


def downgrade() -> None:
    """End every ``uncertain`` run as ``failed``, then narrow the CHECK."""
    table = sa.table(_TABLE, sa.column("status", sa.Text()))
    op.get_bind().execute(
        sa.update(table).where(table.c.status == "uncertain").values(status="failed")
    )
    with op.batch_alter_table(_TABLE) as batch_op:
        batch_op.drop_constraint(_CONSTRAINT, type_="check")
        batch_op.create_check_constraint(_CONSTRAINT, _check_in(_STATUSES_BEFORE))
