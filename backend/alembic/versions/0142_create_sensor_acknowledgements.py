# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Create the ``sensor_acknowledgements`` table.

Revision ID: 0142
Revises: 0140
Create Date: 2026-11-07

A person can acknowledge a Sensor that is not green, for a set time and
with a reason (#4128). One row per acknowledgement:

* ``sensor_id`` -- the Sensor; ``ON DELETE CASCADE``, so deleting a Sensor
  drops its acknowledgements (the audit rows stay).
* ``acked_state`` -- the Sensor's state at that moment. A worse state later
  ends the acknowledgement early.
* ``reason`` (``accepted_risk`` / ``fix_planned`` / ``false_alarm``) and a
  short ``note``.
* ``created_by_sub``, ``created_at``, ``expires_at`` -- who, when, until when.
* ``ended_at`` / ``end_reason`` (``expired`` / ``state_worse``) -- set when it
  ends; NULL while it is open.

A partial unique index allows one open row (``ended_at IS NULL``) per Sensor.
The table is new and empty, so the migration changes no behaviour on its own.
``downgrade()`` drops the index and the table.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0142"
down_revision: str | None = "0140"
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

#: Frozen copies of the model's vocabularies. ``tests/test_checks_acknowledgement.py``
#: checks that they match ``db.models``.
_ACK_STATES: tuple[str, ...] = ("unknown", "degraded", "critical")
_ACK_REASONS: tuple[str, ...] = ("accepted_risk", "fix_planned", "false_alarm")
_ACK_END_REASONS: tuple[str, ...] = ("expired", "state_worse")


def _check_in(column: str, values: tuple[str, ...]) -> str:
    """Render a portable ``column IN ('a', 'b', ...)`` CHECK body."""
    quoted = ", ".join(f"'{v}'" for v in values)
    return f"{column} IN ({quoted})"


def upgrade() -> None:
    """Create the table and its partial unique index."""
    op.create_table(
        "sensor_acknowledgements",
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column(
            "sensor_id",
            sa.Uuid(),
            sa.ForeignKey("sensor.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("acked_state", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("note", sa.Text(), nullable=False),
        sa.Column("created_by_sub", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("end_reason", sa.Text(), nullable=True),
        sa.CheckConstraint(
            _check_in("acked_state", _ACK_STATES),
            name="ck_sensor_acknowledgements_acked_state",
        ),
        sa.CheckConstraint(
            _check_in("reason", _ACK_REASONS),
            name="ck_sensor_acknowledgements_reason",
        ),
        sa.CheckConstraint(
            _check_in("end_reason", _ACK_END_REASONS),
            name="ck_sensor_acknowledgements_end_reason",
        ),
    )
    op.create_index(
        "sensor_acknowledgements_open_idx",
        "sensor_acknowledgements",
        ["sensor_id"],
        unique=True,
        postgresql_where=sa.text("ended_at IS NULL"),
        sqlite_where=sa.text("ended_at IS NULL"),
    )


def downgrade() -> None:
    """Drop what :func:`upgrade` created."""
    op.drop_index("sensor_acknowledgements_open_idx", table_name="sensor_acknowledgements")
    op.drop_table("sensor_acknowledgements")
