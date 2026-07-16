# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Create the ``sensor`` table for the deterministic check layer.

Revision ID: 0064
Revises: 0063
Create Date: 2026-07-16

This migration is the schema substrate of Task #2503 under Initiative
#2416 (parent goal #221) -- the first persisted entity of the check
layer. It adds the ``sensor`` table: one row per deterministic check
pinning an ``(op + args + assertion + cadence + severity)`` tuple that
#2505's runner evaluates on a schedule and #2506's Dashboard rolls up.
The table is modelled on ``scheduled_trigger`` (migration ``0020``) but
is a deliberately separate table -- ``scheduled_trigger.agent_definition_id``
is ``NOT NULL`` with a real FK, so the trigger row structurally cannot
carry an op-based check.

What this migration adds
------------------------

* The ``sensor`` table -- one row per tenant-scoped deterministic check.
* Three indexes:
  * ``sensor_due_idx`` -- b-tree on ``(status, next_fire_at)`` drives
    #2505's "what's due" claim query; a partial index on PG limits it to
    ``status = 'active'`` so the index only carries the rows the runner
    scans.
  * ``sensor_tenant_idx`` -- b-tree on ``(tenant_id, cadence_kind)``
    drives the admin surface's tenant-scoped list.
  * ``sensor_tenant_name_idx`` -- UNIQUE on ``(tenant_id, name)`` because
    Sensors are referenced by name from Dashboards (#2506).

Cadence discriminated union
---------------------------

``cadence_kind`` picks which of ``interval_seconds`` / ``cron_expr``
carries the cadence; a DB-side ``CHECK`` (``ck_sensor_cadence_fields``)
enforces the invariant -- the right column populated, the other NULL --
exactly as ``ck_scheduled_trigger_kind_fields`` does for the trigger.

Closed enums
------------

``cadence_kind`` / ``status`` / ``severity`` / ``last_state`` are closed
enums with portable ``IN (...)`` CHECK bodies. ``last_state`` is the
five-state check vocabulary declared once in #2504's
``meho_backplane.checks.assertions.CheckState``; the literal tuple below
is a frozen independent snapshot, drift-guarded against ``CheckState`` in
``tests.test_db_sensor``.

Dialect portability
-------------------

Mirrors the discipline migration ``0020`` established:

* ``id`` server default -- PG gets ``gen_random_uuid()``; SQLite leaves
  the column to the ORM ``default=uuid.uuid4``.
* ``created_at`` / ``updated_at`` server defaults -- PG gets ``now()``;
  SQLite leaves it to the ORM ``default=lambda: datetime.now(UTC)``.
* ``status`` / ``severity`` / ``last_state`` / ``timezone`` /
  ``identity_sub`` / ``for_seconds`` -- portable literal defaults on both
  dialects.
* ``target`` / ``params`` / ``assertion`` / ``last_value`` /
  ``last_evidence`` -- portable ``JSON`` -> ``JSONB`` variant. The
  nullable JSON columns use ``none_as_null=True`` so a Python ``None``
  round-trips as SQL NULL rather than the JSON literal ``'null'`` (the
  same load-bearing flag ``scheduled_trigger.event_filter`` uses).
* ``sensor_due_idx`` is a *partial* index on PG (``WHERE
  status = 'active'``); SQLite gets a plain b-tree.

Reversibility contract
----------------------

``downgrade()`` drops the indexes then the table in inverse order, the
same discipline migrations ``0020`` / ``0007`` follow.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0064"
down_revision: str | None = "0063"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


#: Closed ``sensor.cadence_kind`` vocabulary -- lock-step with
#: :class:`meho_backplane.db.models.SensorCadenceKind`. Duplicated here as
#: a frozen literal (not imported) so the migration's recorded DDL is a
#: snapshot independent of any later edit to the model enum -- the same
#: self-contained discipline migration ``0020`` follows.
_SENSOR_CADENCE_KINDS: tuple[str, ...] = (
    "interval",
    "cron",
)

#: Closed ``sensor.status`` vocabulary -- lock-step with
#: :class:`meho_backplane.db.models.SensorStatus`.
_SENSOR_STATUSES: tuple[str, ...] = (
    "active",
    "paused",
)

#: Closed ``sensor.severity`` vocabulary -- lock-step with
#: :class:`meho_backplane.db.models.SensorSeverity`.
_SENSOR_SEVERITIES: tuple[str, ...] = (
    "degraded",
    "critical",
)

#: Closed ``sensor.last_state`` vocabulary -- a frozen snapshot of #2504's
#: five-state ``CheckState``. The drift guard in
#: :mod:`tests.test_db_sensor` asserts this set equals ``CheckState``'s
#: members, so a change to the shared vocabulary reddens this migration's
#: test rather than drifting silently.
_SENSOR_LAST_STATES: tuple[str, ...] = (
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


#: Cadence discriminated-union invariant: exactly one of
#: ``interval_seconds`` / ``cron_expr`` carries the semantics, the other
#: is NULL. Portable ``(cadence_kind = '...' AND col IS NOT NULL AND
#: other IS NULL)`` form. Kept byte-for-byte identical to
#: :data:`meho_backplane.db.models._SENSOR_CADENCE_FIELDS_CHECK`; the
#: drift guard asserts equality.
_SENSOR_CADENCE_FIELDS_CHECK: str = (
    "("
    "(cadence_kind = 'interval' AND interval_seconds IS NOT NULL "
    "AND cron_expr IS NULL) OR "
    "(cadence_kind = 'cron' AND cron_expr IS NOT NULL "
    "AND interval_seconds IS NULL)"
    ")"
)


def upgrade() -> None:
    """Create the ``sensor`` table + its indexes."""
    bind = op.get_bind()
    is_postgres = bind.dialect.name == "postgresql"

    # Nullable JSON columns use ``none_as_null=True`` so a Python ``None``
    # round-trips as SQL NULL. NOT NULL JSON columns (``params`` /
    # ``assertion``) use the plain variant -- they always carry a value.
    nullable_json = sa.JSON(none_as_null=True).with_variant(
        postgresql.JSONB(none_as_null=True), "postgresql"
    )
    not_null_json = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")

    op.create_table(
        "sensor",
        sa.Column(
            "id",
            sa.Uuid(),
            primary_key=True,
            nullable=False,
            server_default=sa.text("gen_random_uuid()") if is_postgres else None,
        ),
        sa.Column(
            "tenant_id",
            sa.Uuid(),
            sa.ForeignKey("tenant.id"),
            nullable=False,
        ),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("connector_id", sa.Text(), nullable=False),
        sa.Column("op_id", sa.Text(), nullable=False),
        sa.Column("target", nullable_json, nullable=True),
        sa.Column("params", not_null_json, nullable=False),
        sa.Column("assertion", not_null_json, nullable=False),
        sa.Column(
            "status",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'active'"),
        ),
        sa.Column("status_reason", sa.Text(), nullable=True),
        # Cadence discriminated fields -- exactly one populated per kind
        # (see _SENSOR_CADENCE_FIELDS_CHECK).
        sa.Column("cadence_kind", sa.Text(), nullable=False),
        sa.Column("interval_seconds", sa.Integer(), nullable=True),
        sa.Column("cron_expr", sa.Text(), nullable=True),
        sa.Column(
            "timezone",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'UTC'"),
        ),
        # Materialised next-fire timestamp #2505's claim query scans.
        sa.Column("next_fire_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "severity",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'critical'"),
        ),
        sa.Column(
            "for_seconds",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        # Latest-state projection (Decision D).
        sa.Column(
            "last_state",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'unknown'"),
        ),
        sa.Column("last_value", nullable_json, nullable=True),
        sa.Column("last_evidence", nullable_json, nullable=True),
        sa.Column("last_evaluated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("state_since", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "identity_sub",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'__sensor__'"),
        ),
        sa.Column("created_by_sub", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()") if is_postgres else None,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()") if is_postgres else None,
        ),
        # Closed enums -- portable IN(...) CHECKs. Widening any of these
        # is a coordinated migration + model change (drift guards live in
        # tests.test_db_sensor).
        sa.CheckConstraint(
            _check_in("cadence_kind", _SENSOR_CADENCE_KINDS),
            name="ck_sensor_cadence_kind",
        ),
        sa.CheckConstraint(
            _check_in("status", _SENSOR_STATUSES),
            name="ck_sensor_status",
        ),
        sa.CheckConstraint(
            _check_in("severity", _SENSOR_SEVERITIES),
            name="ck_sensor_severity",
        ),
        sa.CheckConstraint(
            _check_in("last_state", _SENSOR_LAST_STATES),
            name="ck_sensor_last_state",
        ),
        # Cadence discriminated-union invariant.
        sa.CheckConstraint(
            _SENSOR_CADENCE_FIELDS_CHECK,
            name="ck_sensor_cadence_fields",
        ),
    )

    # #2505's "what's due" claim query scans ``WHERE status = 'active'
    # ORDER BY next_fire_at``. The b-tree on (status, next_fire_at) drives
    # it; the partial ``WHERE status = 'active'`` on PG trims the index to
    # the claimable rows. SQLite gets a plain b-tree on the same columns.
    if is_postgres:
        op.create_index(
            "sensor_due_idx",
            "sensor",
            ["status", "next_fire_at"],
            postgresql_using="btree",
            postgresql_where=sa.text("status = 'active'"),
        )
    else:
        op.create_index(
            "sensor_due_idx",
            "sensor",
            ["status", "next_fire_at"],
        )

    # Tenant-scoped list (admin surface).
    op.create_index(
        "sensor_tenant_idx",
        "sensor",
        ["tenant_id", "cadence_kind"],
        postgresql_using="btree",
    )

    # Sensors are referenced by name from Dashboards (#2506); the name is
    # unique within a tenant.
    op.create_index(
        "sensor_tenant_name_idx",
        "sensor",
        ["tenant_id", "name"],
        unique=True,
        postgresql_using="btree",
    )


def downgrade() -> None:
    """Drop the indexes then the ``sensor`` table."""
    op.drop_index("sensor_tenant_name_idx", table_name="sensor")
    op.drop_index("sensor_tenant_idx", table_name="sensor")
    op.drop_index("sensor_due_idx", table_name="sensor")
    op.drop_table("sensor")
