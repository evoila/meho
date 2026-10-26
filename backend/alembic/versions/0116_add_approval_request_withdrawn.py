# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Let the sender withdraw its own pending approval request (#4987).

Revision ID: 0116
Revises: 0115
Create Date: 2026-10-26

Two changes on ``approval_request``:

* The status CHECK ``ck_approval_request_status`` gets a fifth value,
  ``withdrawn``. Every existing row holds one of the four old values, so
  no row breaks the wider rule and nothing is backfilled.
* Two nullable columns, ``withdrawn_at`` (timestamp) and
  ``withdraw_reason`` (text), say when and why the sender withdrew the
  request. Every existing row reads NULL.

The CHECK is dropped and created again inside ``batch_alter_table``, so
SQLite (which cannot drop a constraint in place) rebuilds the table and
PostgreSQL runs plain ``ALTER TABLE``. Same pattern as migration
``0084``.

``downgrade()`` refuses with a clear message while any row is
``withdrawn``: the narrower rule would reject those rows. Otherwise it
drops the two columns and narrows the CHECK back to four values.

The value tuples are written out here, not imported from the models, so
the migration keeps working when the models change later.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0116"
down_revision: str | None = "0115"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "approval_request"
_CONSTRAINT = "ck_approval_request_status"

#: The four values migration ``0023`` allowed.
_STATUSES_BEFORE: tuple[str, ...] = ("pending", "approved", "rejected", "expired")

#: The five values after this migration. Kept equal to
#: ``ApprovalRequestStatus`` by the drift test.
_STATUSES_WITH_WITHDRAWN: tuple[str, ...] = (*_STATUSES_BEFORE, "withdrawn")


def _check_in(column: str, values: tuple[str, ...]) -> str:
    """Render ``column IN ('a', 'b', ...)`` for a CHECK constraint."""
    quoted = ", ".join(f"'{v}'" for v in values)
    return f"{column} IN ({quoted})"


def upgrade() -> None:
    """Add the two columns and widen the status CHECK to ``withdrawn``."""
    with op.batch_alter_table(_TABLE) as batch_op:
        batch_op.add_column(sa.Column("withdrawn_at", sa.DateTime(timezone=True), nullable=True))
        batch_op.add_column(sa.Column("withdraw_reason", sa.Text(), nullable=True))
        batch_op.drop_constraint(_CONSTRAINT, type_="check")
        batch_op.create_check_constraint(_CONSTRAINT, _check_in("status", _STATUSES_WITH_WITHDRAWN))


def downgrade() -> None:
    """Narrow the CHECK back to four values and drop the two columns.

    Raises :class:`RuntimeError` first when any row is ``withdrawn``.
    """
    table = sa.table(_TABLE, sa.column("status", sa.Text()))
    count = (
        op.get_bind()
        .execute(sa.select(sa.func.count()).select_from(table).where(table.c.status == "withdrawn"))
        .scalar_one()
    )
    if count:
        raise RuntimeError(
            f"Cannot downgrade migration 0116: approval_request has {count} withdrawn "
            "row(s) that the narrower status rule would reject. Delete them or set "
            "their status to 'rejected' before running `alembic downgrade`."
        )
    with op.batch_alter_table(_TABLE) as batch_op:
        batch_op.drop_constraint(_CONSTRAINT, type_="check")
        batch_op.create_check_constraint(_CONSTRAINT, _check_in("status", _STATUSES_BEFORE))
        batch_op.drop_column("withdraw_reason")
        batch_op.drop_column("withdrawn_at")
