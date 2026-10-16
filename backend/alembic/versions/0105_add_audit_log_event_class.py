# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Add the closed ``event_class`` column to ``audit_log`` (#3733).

Revision ID: 0105
Revises: 0104
Create Date: 2026-10-16

What it adds
------------

``audit_log.event_class`` says what kind of accountable event a row
records: ``operation_execution``, ``policy_decision``,
``approval_decision``, ``security_event`` or ``legacy_request``. The
column is ``NOT NULL`` and a CHECK constraint rejects any other value.

Old rows
--------

The column has the server default ``'legacy_request'``. So every row that
exists before this migration reads as ``legacy_request``. The migration
does not look at ``method``, ``path`` or ``payload`` to guess a better
class, and it runs no ``UPDATE``. That also keeps it clear of the
``audit_log`` append-only trigger (``0100`` / ``0103``): on PostgreSQL 11
and later, adding a column with a constant default changes only the
catalog, so no row trigger fires.

The default stays after the upgrade. An older MEHO version that runs
during a rolling upgrade does not know the column; its rows also land as
``legacy_request``, which is true for them. New code always sets the
class itself.

Reversibility
-------------

``downgrade()`` drops the CHECK and the column. ``upgrade()`` only adds,
so the migration compat guard accepts it.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0105"
down_revision: str | None = "0104"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: The closed vocabulary. Mirrors ``AuditEventClass`` in ``db/models.py``;
#: written out here so the migration does not change if the model changes.
_EVENT_CLASS_CHECK: str = (
    "event_class IN ('operation_execution', 'policy_decision', "
    "'approval_decision', 'security_event', 'legacy_request')"
)


def upgrade() -> None:
    """Add the NOT NULL ``event_class`` column with its CHECK."""
    with op.batch_alter_table("audit_log") as batch_op:
        batch_op.add_column(
            sa.Column(
                "event_class",
                sa.Text(),
                nullable=False,
                server_default=sa.text("'legacy_request'"),
            )
        )
        batch_op.create_check_constraint(
            "ck_audit_log_event_class",
            _EVENT_CLASS_CHECK,
        )


def downgrade() -> None:
    """Drop the CHECK and the ``event_class`` column."""
    with op.batch_alter_table("audit_log") as batch_op:
        batch_op.drop_constraint("ck_audit_log_event_class", type_="check")
        batch_op.drop_column("event_class")
