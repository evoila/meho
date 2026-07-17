# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Create the ``check_dashboards`` + ``check_dashboard_sensors`` tables.

Revision ID: 0065
Revises: 0064
Create Date: 2026-07-16

Task #2506 under Initiative #2416 (parent goal #221). Adds the Dashboard
entity -- a named, tenant-scoped composition of Sensors (#2503) -- and its
many-to-many membership join, so an operator can roll many Sensors up into
one five-state answer to "is everything OK?".

What this migration adds
------------------------

* ``check_dashboards`` -- one row per tenant-scoped Dashboard. Carries the
  ``ScheduledTrigger`` / ``Sensor`` column discipline (UUID PK, ``tenant_id``
  FK, ``created_by_sub``, ``created_at`` / ``updated_at``) plus:
  * ``description`` -- nullable free text.
  * ``last_rollup_state`` -- a nullable transition-detection memo shipped
    UNWRITTEN by this Task (its only writer is #2507's hook). A CHECK admits
    NULL plus #2504's five-state ``CheckState`` vocabulary.
* ``check_dashboard_sensors`` -- the pure many-to-many association table
  (composite PK ``(dashboard_id, sensor_id)``, both real FKs with
  ``ondelete="CASCADE"``). Deleting a Dashboard or a Sensor removes only the
  memberships.
* Indexes: ``check_dashboard_tenant_idx`` (tenant-scoped list),
  ``check_dashboard_tenant_name_idx`` (UNIQUE on ``(tenant_id, name)`` -- a
  Dashboard name is unique per tenant), and
  ``check_dashboard_sensors_sensor_idx`` (reverse lookup + the index behind
  the ``sensor_id`` FK cascade).

Dialect portability
-------------------

Mirrors migration ``0064``:

* ``id`` server default -- PG gets ``gen_random_uuid()``; SQLite leaves it
  to the ORM ``default=uuid.uuid4``.
* ``created_at`` / ``updated_at`` server defaults -- PG gets ``now()``;
  SQLite leaves it to the ORM default.
* ``last_rollup_state`` closed enum -- portable ``IN (...)`` CHECK; the
  literal tuple below is a frozen independent snapshot of #2504's
  ``CheckState``, drift-guarded against it in ``tests.test_db_dashboard``.

Reversibility contract
----------------------

``downgrade()`` drops the association table (which FKs the dashboard table)
first, then the dashboard table -- inverse dependency order, the same
discipline migrations ``0064`` / ``0020`` follow.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0065"
down_revision: str | None = "0064"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


#: Closed ``check_dashboards.last_rollup_state`` vocabulary -- a frozen
#: snapshot of #2504's five-state ``CheckState``. Duplicated here as a
#: literal (not imported) so the migration's recorded DDL is independent of
#: any later edit to the model; the drift guard in
#: :mod:`tests.test_db_dashboard` asserts this set equals ``CheckState``'s
#: members.
_ROLLUP_STATES: tuple[str, ...] = (
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
    """Create the Dashboard table + its membership join + indexes."""
    bind = op.get_bind()
    is_postgres = bind.dialect.name == "postgresql"

    op.create_table(
        "check_dashboards",
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
        sa.Column("description", sa.Text(), nullable=True),
        # Transition-detection memo (#2507). Shipped unwritten; NULL admitted.
        sa.Column("last_rollup_state", sa.Text(), nullable=True),
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
        # ``last_rollup_state`` over exactly #2504's ``CheckState`` members;
        # NULL admitted (the memo is unwritten until #2507). Widening this is
        # a coordinated migration + model change (drift guard in
        # tests.test_db_dashboard).
        sa.CheckConstraint(
            _check_in("last_rollup_state", _ROLLUP_STATES),
            name="ck_check_dashboards_last_rollup_state",
        ),
    )

    # Tenant-scoped list (admin surface).
    op.create_index(
        "check_dashboard_tenant_idx",
        "check_dashboards",
        ["tenant_id"],
        postgresql_using="btree",
    )
    # A Dashboard name is unique within a tenant.
    op.create_index(
        "check_dashboard_tenant_name_idx",
        "check_dashboards",
        ["tenant_id", "name"],
        unique=True,
        postgresql_using="btree",
    )

    op.create_table(
        "check_dashboard_sensors",
        sa.Column(
            "dashboard_id",
            sa.Uuid(),
            sa.ForeignKey("check_dashboards.id", ondelete="CASCADE"),
            primary_key=True,
            nullable=False,
        ),
        sa.Column(
            "sensor_id",
            sa.Uuid(),
            sa.ForeignKey("sensor.id", ondelete="CASCADE"),
            primary_key=True,
            nullable=False,
        ),
    )
    # Reverse lookup "which Dashboards reference this Sensor" + the index PG
    # wants behind the ``sensor_id`` FK's cascade.
    op.create_index(
        "check_dashboard_sensors_sensor_idx",
        "check_dashboard_sensors",
        ["sensor_id"],
        postgresql_using="btree",
    )


def downgrade() -> None:
    """Drop the membership join first, then the Dashboard table."""
    op.drop_index("check_dashboard_sensors_sensor_idx", table_name="check_dashboard_sensors")
    op.drop_table("check_dashboard_sensors")
    op.drop_index("check_dashboard_tenant_name_idx", table_name="check_dashboards")
    op.drop_index("check_dashboard_tenant_idx", table_name="check_dashboards")
    op.drop_table("check_dashboards")
