# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Create the ``operation_run`` table for async governed dispatch.

Revision ID: 0079
Revises: 0078
Create Date: 2026-08-29

Schema substrate of async governed dispatch (#3079). Adds the
``operation_run`` table -- one row per governed operation dispatch
submitted in async mode. The row makes a long-running dispatch return a
durable handle (HTTP 202) instead of holding the request connection for
the op's full duration: execution proceeds on a background task tracked
here, and the caller polls / cancels via the handle. The full
:class:`~meho_backplane.connectors.schemas.OperationResult` envelope is
persisted on ``result`` so a dropped response never loses the outcome
(the motivating incident: an 83s vendor call whose 200 was lost in
transit).

The table reuses the *shape* of ``agent_run`` (durable row + lease /
heartbeat + reaper) without its LLM-loop columns. Two deliberate
differences from ``agent_run``:

* **No ``in_flight_policy`` column.** A governed op can wrap a
  non-idempotent vendor write, so an orphaned run is never re-dispatched
  (that would double-execute the write); the operation-run reaper always
  drives it to ``failed``. There is no ``resume`` policy to snapshot.
* **No ``params`` column -- only ``params_hash``.** Persisting raw params
  would be a new secret surface (the same reason ``audit_log`` stores only
  a hash), and because the run never resumes nothing needs them
  re-hydrated. The running task holds params in memory for the single
  dispatch.

Schema
------

* ``id`` -- UUID PK; the handle. PG ``gen_random_uuid()`` server default,
  ORM ``default=uuid.uuid4`` on SQLite.
* ``tenant_id`` -- UUID NOT NULL, real ``REFERENCES tenant(id)`` FK
  (clean-slate substrate, same discipline as ``agent_run`` / ``graph_node``).
* ``identity_sub`` -- Text NOT NULL; RFC 8693 ``sub`` (submitting operator).
* ``identity_act`` -- Text nullable; RFC 8693 ``act`` (delegated actor).
* ``origin`` -- Text NOT NULL, portable ``CHECK origin IN (...)`` over the
  closed :class:`~meho_backplane.db.models.OperationRunOrigin` vocabulary
  (``direct`` / ``approval_resume``).
* ``connector_id`` / ``op_id`` -- Text NOT NULL; the dispatch coordinates.
* ``target_name`` -- Text nullable; the submitted target name (NULL for a
  target-less op).
* ``params_hash`` -- Text nullable; hex SHA-256 of the submitted params,
  the secret-safe correlation to the dispatch audit row.
* ``approval_request_id`` -- UUID nullable, soft-FK to
  ``approval_request.id`` for an ``approval_resume`` run.
* ``status`` -- Text NOT NULL DEFAULT ``'pending'``, portable
  ``CHECK status IN (...)`` over the closed
  :class:`~meho_backplane.db.models.OperationRunStatus` lifecycle.
* ``result`` -- portable JSON -> JSONB nullable; the persisted
  ``OperationResult`` envelope once ``succeeded``.
* ``error`` -- Text nullable; run-crash / reaper reason on ``failed``.
* ``lease_owner`` / ``lease_expires_at`` -- the reaper lease.
* ``created_at`` -- ``timestamptz`` NOT NULL, PG ``now()`` server default.
* ``started_at`` / ``ended_at`` -- ``timestamptz`` nullable.

Indexes
-------

* ``operation_run_tenant_created_at_idx`` -- (tenant_id, created_at); the
  tenant-scoped "list runs newest first" surface.
* ``operation_run_status_idx`` -- status; "find running runs".
* ``operation_run_approval_request_id_idx`` -- approval_request_id; the
  approval-resume linkage lookup.
* ``operation_run_lease_expires_at_idx`` -- lease_expires_at, partial on
  PG (``WHERE status = 'running'``); drives the reaper's claim query.

Reversibility contract
----------------------

``upgrade()`` creates the table and its four indexes; ``downgrade()``
drops the indexes then the table in inverse order (explicit index drops
keep the inverse symmetric across both dialects).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0079"
down_revision: str | None = "0078"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


#: Closed ``operation_run.status`` vocabulary -- kept in lock-step with
#: :class:`meho_backplane.db.models.OperationRunStatus`. Duplicated here as
#: a literal tuple (not imported) so the migration's recorded DDL is a
#: frozen snapshot; the drift guard in
#: :mod:`tests.test_operation_run_lifecycle` asserts the model enum and the
#: live ``CHECK`` constraint agree.
_OPERATION_RUN_STATUSES: tuple[str, ...] = (
    "pending",
    "running",
    "succeeded",
    "failed",
    "cancelled",
)

#: Closed ``operation_run.origin`` vocabulary -- lock-step with
#: :class:`meho_backplane.db.models.OperationRunOrigin`.
_OPERATION_RUN_ORIGINS: tuple[str, ...] = (
    "direct",
    "approval_resume",
)


def _check_in(column: str, values: tuple[str, ...]) -> str:
    """Render a portable ``column IN ('a', 'b', ...)`` CHECK body."""
    quoted = ", ".join(f"'{v}'" for v in values)
    return f"{column} IN ({quoted})"


def upgrade() -> None:
    """Create the ``operation_run`` table and its indexes."""
    bind = op.get_bind()
    is_postgres = bind.dialect.name == "postgresql"

    # Portable JSONB -> JSON variant; same pattern agent_run.output uses.
    result_type = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")

    op.create_table(
        "operation_run",
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
        sa.Column("identity_sub", sa.Text(), nullable=False),
        sa.Column("identity_act", sa.Text(), nullable=True),
        sa.Column("origin", sa.Text(), nullable=False),
        sa.Column("connector_id", sa.Text(), nullable=False),
        sa.Column("op_id", sa.Text(), nullable=False),
        sa.Column("target_name", sa.Text(), nullable=True),
        sa.Column("params_hash", sa.Text(), nullable=True),
        # Soft-FK to approval_request.id (no clause) -- set on resume runs.
        sa.Column("approval_request_id", sa.Uuid(), nullable=True),
        sa.Column(
            "status",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'pending'"),
        ),
        sa.Column("result", result_type, nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("lease_owner", sa.Text(), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()") if is_postgres else None,
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            _check_in("status", _OPERATION_RUN_STATUSES),
            name="ck_operation_run_status",
        ),
        sa.CheckConstraint(
            _check_in("origin", _OPERATION_RUN_ORIGINS),
            name="ck_operation_run_origin",
        ),
    )

    op.create_index(
        "operation_run_tenant_created_at_idx",
        "operation_run",
        ["tenant_id", "created_at"],
        postgresql_using="btree",
    )
    op.create_index(
        "operation_run_status_idx",
        "operation_run",
        ["status"],
        postgresql_using="btree",
    )
    op.create_index(
        "operation_run_approval_request_id_idx",
        "operation_run",
        ["approval_request_id"],
        postgresql_using="btree",
    )
    # Partial on PG (only ``running`` rows carry a lease); SQLite ignores
    # ``postgresql_where`` and indexes the whole column.
    op.create_index(
        "operation_run_lease_expires_at_idx",
        "operation_run",
        ["lease_expires_at"],
        postgresql_using="btree",
        postgresql_where=sa.text("status = 'running'"),
    )


def downgrade() -> None:
    """Drop the indexes then the ``operation_run`` table."""
    op.drop_index("operation_run_lease_expires_at_idx", table_name="operation_run")
    op.drop_index("operation_run_approval_request_id_idx", table_name="operation_run")
    op.drop_index("operation_run_status_idx", table_name="operation_run")
    op.drop_index("operation_run_tenant_created_at_idx", table_name="operation_run")
    op.drop_table("operation_run")
