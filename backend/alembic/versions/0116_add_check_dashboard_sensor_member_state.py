# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Add ``check_dashboard_sensors.last_member_state`` (#5068).

Revision ID: 0116
Revises: 0115
Create Date: 2026-10-26

Task #5068 (parent goal #221). A check mail went out only when a
Dashboard's overall state changed. A Dashboard that is already
``critical`` because of one Sensor hid every later problem on it: a second
Sensor turning ``critical`` did not change the overall state, so nobody
got a mail. This column is the per-``(dashboard, sensor)`` memo the
transition claim (``meho_backplane.checks.investigate``) compares each
member's counted state against, so a member that gets worse mails even
when the Dashboard's state stays the same.

* ``last_member_state`` -- ``text`` nullable, CHECK over the five-state
  ``CheckState`` vocabulary (``ok`` / ``degraded`` / ``critical`` /
  ``unknown`` / ``skip``) plus NULL. It holds the member's state **as the
  Dashboard counts it**: after the Sensor's ``severity`` cap and after its
  ``for_seconds`` hold, which is what the rollup folds.

Why the link row and not the ``sensor`` row: one Sensor can be a member of
several Dashboards, and each Dashboard has its own recipients and its own
floor, so each Dashboard needs its own memo. Why a durable column and not
a Valkey key: the mail must go out exactly once also when two MEHO copies
run, and the claim already has a per-``(tenant, dashboard)`` lock plus a
compare-and-swap on ``check_dashboards.last_rollup_state``; the member
memo rides the same transaction with the same conditional ``UPDATE``.

NULL backfill is behaviour-preserving: every row from before this
migration starts "not counted yet", and the first claim after the upgrade
records each member's current state **without mailing**. An upgrade
therefore never produces a burst of "new problem" mails for problems that
are already visible on the Dashboard.

Why ``batch_alter_table``: the same portable cookbook ``0068`` uses to add
a CHECK-constrained column to an existing table. On PostgreSQL Alembic
issues plain ``ALTER TABLE ADD COLUMN`` + ``ADD CONSTRAINT``; on SQLite it
falls back to the table-recreate path, the only way SQLite adds a CHECK to
an existing table.

Reversibility contract
----------------------

``downgrade()`` drops the constraint and the column inside one batch
block. Purely additive forward, so a ``helm rollback`` to a pre-0116 image
(which never mentions the column) is safe: an older build inserts link
rows without it and reads NULL back.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0116"
down_revision: str | None = "0115"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


#: Closed ``check_dashboard_sensors.last_member_state`` vocabulary -- a
#: frozen snapshot of #2504's ``CheckState`` (same shape as ``0065``'s
#: ``_ROLLUP_STATES``). Inlined as a literal (never imported from
#: :mod:`meho_backplane.db.models`) because Alembic must run against any
#: historical revision's model graph; the drift guard in
#: :mod:`tests.test_db_dashboard` asserts this tuple equals the ORM's.
_MEMBER_STATES: tuple[str, ...] = ("ok", "degraded", "critical", "unknown", "skip")


def _check_in(column: str, values: tuple[str, ...]) -> str:
    """Render a portable ``column IN ('a', 'b', ...)`` CHECK body."""
    quoted = ", ".join(f"'{v}'" for v in values)
    return f"{column} IN ({quoted})"


def upgrade() -> None:
    """Add the nullable member-state column + its CHECK."""
    with op.batch_alter_table("check_dashboard_sensors") as batch_op:
        batch_op.add_column(sa.Column("last_member_state", sa.Text(), nullable=True))
        batch_op.create_check_constraint(
            "ck_check_dashboard_sensors_last_member_state",
            _check_in("last_member_state", _MEMBER_STATES),
        )


def downgrade() -> None:
    """Drop the CHECK + the column added in :func:`upgrade`."""
    with op.batch_alter_table("check_dashboard_sensors") as batch_op:
        batch_op.drop_constraint("ck_check_dashboard_sensors_last_member_state", type_="check")
        batch_op.drop_column("last_member_state")
