# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Add the ``config_change`` audit event class (#5791).

Revision ID: 0137
Revises: 0136
Create Date: 2026-11-05

An operator who changes the settings of one operation
(``meho_connector_edit_op``) gets an audit row of its own class,
``config_change``. Before, that row was ``operation_execution``, so a
filter for operations that ran also returned the edit.

The CHECK ``ck_audit_log_event_class`` gets a sixth value. Every existing
row holds one of the five old values, so no row breaks the wider rule and
no row is changed. Old edit rows keep ``operation_execution``: the audit
log is append-only.

The CHECK is dropped and created again inside ``batch_alter_table``, so
SQLite (which cannot drop a constraint in place) rebuilds the table and
PostgreSQL runs plain ``ALTER TABLE``. Same pattern as migration
``0119``.

``downgrade()`` refuses with a clear message while any row is
``config_change``: the narrower rule would reject those rows, and the
append-only audit log does not let them be changed.

The value tuples are written out here, not imported from the models, so
the migration keeps working when the models change later.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0137"
down_revision: str | None = "0136"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "audit_log"
_CONSTRAINT = "ck_audit_log_event_class"

#: The five values migration ``0106`` allowed.
_CLASSES_BEFORE: tuple[str, ...] = (
    "operation_execution",
    "policy_decision",
    "approval_decision",
    "security_event",
    "legacy_request",
)

#: The six values after this migration. Kept equal to ``AuditEventClass``
#: by the migration test.
_CLASSES_WITH_CONFIG_CHANGE: tuple[str, ...] = (*_CLASSES_BEFORE, "config_change")


def _check_in(values: tuple[str, ...]) -> str:
    """Render ``event_class IN ('a', 'b', ...)`` for the CHECK constraint."""
    quoted = ", ".join(f"'{v}'" for v in values)
    return f"event_class IN ({quoted})"


def upgrade() -> None:
    """Widen the event class CHECK to ``config_change``."""
    with op.batch_alter_table(_TABLE) as batch_op:
        batch_op.drop_constraint(_CONSTRAINT, type_="check")
        batch_op.create_check_constraint(_CONSTRAINT, _check_in(_CLASSES_WITH_CONFIG_CHANGE))


def downgrade() -> None:
    """Narrow the CHECK back to five values.

    Raises :class:`RuntimeError` first when any row is ``config_change``.
    """
    table = sa.table(_TABLE, sa.column("event_class", sa.Text()))
    count = (
        op.get_bind()
        .execute(
            sa.select(sa.func.count())
            .select_from(table)
            .where(table.c.event_class == "config_change")
        )
        .scalar_one()
    )
    if count:
        raise RuntimeError(
            f"Cannot downgrade migration 0137: audit_log has {count} config_change "
            "row(s) that the narrower event class rule would reject. The audit log "
            "is append-only, so stay on revision 0137."
        )
    with op.batch_alter_table(_TABLE) as batch_op:
        batch_op.drop_constraint(_CONSTRAINT, type_="check")
        batch_op.create_check_constraint(_CONSTRAINT, _check_in(_CLASSES_BEFORE))
