# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Create the ``sensor_results`` per-tick evidence-history table.

Revision ID: 0071
Revises: 0070
Create Date: 2026-08-04

Task #2756 under Initiative #2780 (parent goal #221). The check layer's
:class:`~meho_backplane.db.models.Sensor` carries only a latest-state
projection (``last_state`` / ``last_value`` / ``last_evidence`` /
``last_evaluated_at``); every tick overwrites it, so the history the runner
already computed was discarded. A post-incident review needed "when did this
first flap / how fast is it filling" and had nothing to read. This migration
adds the append-only history table that
:func:`~meho_backplane.checks.repository.record_sensor_result` writes to, in
the same transaction as the projection update, when
``CHECKS_EVIDENCE_RETENTION_DAYS > 0``.

What this migration adds
------------------------

* The ``sensor_results`` table -- one append-only row per completed (non-stale)
  evaluation: ``(sensor_id, evaluated_at, state, value, evidence, reason)``.
* Two indexes:
  * ``sensor_results_sensor_evaluated_idx`` -- b-tree on
    ``(sensor_id, evaluated_at)`` drives the trend query
    (``WHERE sensor_id = ? ... ORDER BY evaluated_at ASC`` + the keyset
    ``evaluated_at > :cursor``).
  * ``sensor_results_evaluated_at_idx`` -- b-tree on ``evaluated_at`` lets the
    retention sweep range-scan ``WHERE evaluated_at < cutoff`` instead of
    seq-scanning (the composite above leads with ``sensor_id``).

Cascade delete
--------------

``sensor_id`` is a real ``REFERENCES sensor(id)`` FK with
``ON DELETE CASCADE`` -- deleting a Sensor drops its history, the same
discipline the ``check_dashboard_sensors`` membership join (migration
``0065``) uses off ``sensor.id``. The #2756 acceptance criterion pins this
choice (cascade, not tombstone).

Closed enum
-----------

``state`` is the five-state check vocabulary declared once in #2504's
``meho_backplane.checks.assertions.CheckState``; the literal tuple below is a
frozen independent snapshot, drift-guarded against ``CheckState`` in
``tests.test_db_sensor_results`` -- the same discipline ``sensor.last_state``
(migration ``0064``) follows.

Dialect portability
-------------------

Mirrors migration ``0064``:

* ``id`` server default -- PG gets ``gen_random_uuid()``; SQLite leaves the
  column to the ORM ``default=uuid.uuid4``.
* ``value`` / ``evidence`` -- portable ``JSON`` -> ``JSONB`` variant with
  ``none_as_null=True`` so a Python ``None`` round-trips as SQL NULL.

Reversibility contract
----------------------

Purely additive forward (a new table + its indexes), so a ``helm rollback``
to a pre-0071 image (which never mentions the table) is safe -- the projection
on ``sensor`` is untouched and remains the current-state source.
``downgrade()`` drops the indexes then the table in inverse order, the same
discipline migrations ``0065`` / ``0064`` follow; it discards accumulated
history, the expected cost of removing a history table.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0071"
down_revision: str | None = "0070"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


#: Closed ``sensor_results.state`` vocabulary -- a frozen snapshot of #2504's
#: five-state ``CheckState``. The drift guard in
#: :mod:`tests.test_db_sensor_results` asserts this set equals ``CheckState``'s
#: members, so a change to the shared vocabulary reddens this migration's test
#: rather than drifting silently.
_SENSOR_RESULT_STATES: tuple[str, ...] = (
    "ok",
    "degraded",
    "critical",
    "unknown",
    "skip",
)


def _check_in(column: str, values: tuple[str, ...]) -> str:
    """Render a portable ``column IN ('a', 'b', ...)`` CHECK body."""
    quoted = ", ".join(f"'{v}'" for v in values)
    return f"{column} IN ({quoted})"


def upgrade() -> None:
    """Create the ``sensor_results`` table + its two indexes."""
    bind = op.get_bind()
    is_postgres = bind.dialect.name == "postgresql"

    # Nullable JSON columns use ``none_as_null=True`` so a Python ``None``
    # round-trips as SQL NULL rather than the JSON literal ``'null'`` -- the
    # same load-bearing flag ``sensor.last_value`` / ``last_evidence`` use.
    nullable_json = sa.JSON(none_as_null=True).with_variant(
        postgresql.JSONB(none_as_null=True), "postgresql"
    )

    op.create_table(
        "sensor_results",
        sa.Column(
            "id",
            sa.Uuid(),
            primary_key=True,
            nullable=False,
            server_default=sa.text("gen_random_uuid()") if is_postgres else None,
        ),
        sa.Column(
            "sensor_id",
            sa.Uuid(),
            sa.ForeignKey("sensor.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("evaluated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("value", nullable_json, nullable=True),
        sa.Column("evidence", nullable_json, nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        # ``state`` over exactly #2504's CheckState members -- widening it is a
        # coordinated migration + model change (drift guard in
        # tests.test_db_sensor_results).
        sa.CheckConstraint(
            _check_in("state", _SENSOR_RESULT_STATES),
            name="ck_sensor_results_state",
        ),
    )

    # The trend query: ``WHERE sensor_id = ? [AND evaluated_at >= ?]
    # [AND evaluated_at <= ?] ORDER BY evaluated_at ASC`` + keyset.
    op.create_index(
        "sensor_results_sensor_evaluated_idx",
        "sensor_results",
        ["sensor_id", "evaluated_at"],
        postgresql_using="btree",
    )
    # The retention sweep: ``DELETE WHERE evaluated_at < cutoff``.
    op.create_index(
        "sensor_results_evaluated_at_idx",
        "sensor_results",
        ["evaluated_at"],
        postgresql_using="btree",
    )


def downgrade() -> None:
    """Drop the indexes then the ``sensor_results`` table."""
    op.drop_index(
        "sensor_results_evaluated_at_idx",
        table_name="sensor_results",
    )
    op.drop_index(
        "sensor_results_sensor_evaluated_idx",
        table_name="sensor_results",
    )
    op.drop_table("sensor_results")
