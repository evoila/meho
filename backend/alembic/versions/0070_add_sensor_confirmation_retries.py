# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Add ``sensor`` state-confirmation retry columns (#2799).

Revision ID: 0070
Revises: 0069
Create Date: 2026-08-04

Task #2799 under Initiative #2780 (parent goal #221). One transient bad
reading flips a sensor's ``last_state`` on the very next evaluation, so a
single flaky reading pages and its recovery pages again (the observed
``capacity-physical`` one-minute flap). These four columns are the
per-sensor confirmation config + the soft-state window
``record_sensor_result`` holds an unconfirmed candidate in (Nagios
soft/hard states):

* ``retry_times`` -- ``integer`` NOT NULL, server default ``'0'``.
  Consecutive confirming re-evaluations required after the first
  differing reading before the new state commits. The ``'0'`` backfill
  disables confirmation for every pre-#2799 row, so the migration is
  behaviour-preserving on its own.
* ``retry_backoff_seconds`` -- ``integer`` NOT NULL, server default
  ``'15'``. Accelerated re-check spacing while a state is pending
  (Nagios ``retry_interval``). Inert while ``retry_times`` is 0.
* ``pending_state`` -- ``text`` nullable, CHECK over the ``CheckState``
  vocabulary minus ``skip``. The held candidate state; NULL (the
  backfilled state) means no confirmation window is open.
* ``pending_count`` -- ``integer`` NOT NULL, server default ``'0'``.
  How many consecutive readings have agreed on ``pending_state``.

The CHECK excludes ``skip`` because ``skip`` is a rollup-side
*derivation* for paused members (#2506), never an evaluation outcome --
the evaluator cannot emit it, so it can never be a confirmation
candidate. The vocabulary is frozen here as a literal (never imported
from :mod:`meho_backplane.db.models`) because Alembic must run against
any historical revision's model graph; the drift guard in
:mod:`tests.test_db_sensor` asserts this tuple equals the ORM's
``_SENSOR_PENDING_STATES``.

Why NOT NULL + server default for the three counters: the commit gate in
``record_sensor_result`` reads concrete values on every persist, so
"unset" would need a Python-side fallback duplicating the default in a
second place -- the same reasoning ``0068``'s ``notify_min_state``
documents (and ``0057``'s ``skip_count`` before it).

Why ``batch_alter_table``: the same portable cookbook ``0025`` / ``0051``
/ ``0068`` use to add a CHECK-constrained column to an existing table.
On PostgreSQL Alembic issues plain ``ALTER TABLE ADD COLUMN`` + ``ADD
CONSTRAINT`` (no copy; PG 11+ adds a defaulted NOT NULL column without a
rewrite); on SQLite it falls back to the table-recreate path, which is
the only way SQLite adds a CHECK to an existing table. One batch block
adds all four columns and the constraint together, so SQLite recreates
once.

Reversibility contract
----------------------

``downgrade()`` drops the constraint and all four columns inside one
batch block, in reverse add order. Purely additive forward, so a ``helm
rollback`` to a pre-0070 image (which never mentions the columns) is
safe.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0070"
down_revision: str | None = "0069"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


#: Closed ``sensor.pending_state`` vocabulary -- #2504's ``CheckState``
#: minus ``skip``. Frozen as a literal; the drift guard in
#: :mod:`tests.test_db_sensor` pins it against the ORM's
#: ``_SENSOR_PENDING_STATES`` (itself derived from ``CheckState``).
_SENSOR_PENDING_STATES: tuple[str, ...] = ("ok", "degraded", "critical", "unknown")


def _check_in(column: str, values: tuple[str, ...]) -> str:
    """Render a portable ``column IN ('a', 'b', ...)`` CHECK body."""
    quoted = ", ".join(f"'{v}'" for v in values)
    return f"{column} IN ({quoted})"


def upgrade() -> None:
    """Add the confirmation columns + the ``pending_state`` CHECK."""
    with op.batch_alter_table("sensor") as batch_op:
        batch_op.add_column(
            sa.Column("retry_times", sa.Integer(), nullable=False, server_default="0")
        )
        batch_op.add_column(
            sa.Column(
                "retry_backoff_seconds",
                sa.Integer(),
                nullable=False,
                server_default="15",
            )
        )
        batch_op.add_column(sa.Column("pending_state", sa.Text(), nullable=True))
        batch_op.add_column(
            sa.Column("pending_count", sa.Integer(), nullable=False, server_default="0")
        )
        batch_op.create_check_constraint(
            "ck_sensor_pending_state",
            _check_in("pending_state", _SENSOR_PENDING_STATES),
        )


def downgrade() -> None:
    """Drop the CHECK + all four columns added in :func:`upgrade` (reverse order)."""
    with op.batch_alter_table("sensor") as batch_op:
        batch_op.drop_constraint("ck_sensor_pending_state", type_="check")
        batch_op.drop_column("pending_count")
        batch_op.drop_column("pending_state")
        batch_op.drop_column("retry_backoff_seconds")
        batch_op.drop_column("retry_times")
