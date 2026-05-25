# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Create the ``approval_request`` table for the durable approval queue.

Revision ID: 0020
Revises: 0017
Create Date: 2026-05-25

This migration is the schema substrate of Initiative #803 (G11.2 Agent
permission model), Task #817 (T4). It adds the ``approval_request``
table — one row per op dispatch that the policy gate tagged
``requires_approval``. The row parks the pending dispatch durably so a
process restart cannot lose an in-flight approval; the resume endpoint
(:mod:`meho_backplane.operations.approval_queue`) flips the row to
``approved`` / ``rejected`` and, on approval, re-dispatches the original
call with the original params.

Schema
------

* ``id`` -- UUID primary key. PG production gets a
  ``gen_random_uuid()`` server default; the ORM ``default=uuid.uuid4``
  covers SQLite dev/test.

* ``tenant_id`` -- UUID NOT NULL with a real ``REFERENCES tenant(id)``
  FK. Mirrors the discipline ``agent_run`` (0017) / ``graph_node``
  (0007) follow: clean-slate table, real FK.

* ``run_id`` -- UUID nullable, **soft-FK** to ``agent_run.id``. Set
  when the approval request came from an in-flight agent run; NULL for
  approval requests initiated outside of an agent run context (CLI /
  direct REST call). Soft-FK (no clause) mirrors
  ``audit_log.agent_session_id`` (0014).

* ``principal_sub`` -- Text NOT NULL. The ``sub`` of the principal that
  triggered the op (the RFC 8693 ``sub`` claim, as in
  ``audit_log.operator_sub`` and ``agent_run.identity_sub``). Logged
  for every audit row that references this request.

* ``principal_act`` -- Text nullable. The RFC 8693 ``act`` claim of the
  agent acting on behalf of the subject; NULL for direct human calls.
  Mirrors ``agent_run.identity_act``.

* ``op_id`` -- Text NOT NULL. The operation id passed to the
  dispatcher. Recorded so the resume endpoint can re-issue
  :func:`~meho_backplane.operations.dispatcher.dispatch` with the same
  op without re-parsing the connector id.

* ``connector_id`` -- Text NOT NULL. The full ``<impl_id>-<version>``
  connector id string that was passed to the dispatcher. Stored
  verbatim so the resume endpoint can replay the dispatch identically.

* ``target_id`` -- UUID nullable. The target the dispatch was scoped to
  (``target.id``), or NULL for tenant-wide ops. Soft-FK, same
  discipline as ``audit_log.target_id`` (0004).

* ``params_hash`` -- Text NOT NULL. The SHA-256 hex hash of the
  canonicalised params (computed by
  :func:`~meho_backplane.operations._validate.compute_params_hash`).
  Does not leak the params themselves; the reviewer's decision is
  recorded at the row level, not the parameter level. Also used to
  detect param-substitution attacks on the resume path (the resume
  endpoint re-hashes its params against this stored hash).

* ``proposed_effect`` -- portable JSON NOT NULL DEFAULT ``{}``. A
  human-readable summary of what the op would do if approved, so the
  approver can make an informed decision without needing to inspect the
  raw params. Populated by the service layer at queue time; JSONB on
  PostgreSQL for GIN-friendly filtering.

* ``status`` -- Text NOT NULL DEFAULT ``'pending'`` with a portable
  ``CHECK status IN (...)`` constraint enforcing the closed
  :class:`~meho_backplane.db.models.ApprovalRequestStatus` vocabulary
  (``pending`` / ``approved`` / ``rejected`` / ``expired``). The enum
  and the constraint move in lock-step; the drift guard
  :mod:`tests.test_migration_0020_approval_request` asserts equality.

* ``reviewed_by`` -- Text nullable. The ``sub`` of the operator who
  approved or rejected the request; NULL while pending / expired
  without a reviewer.

* ``decided_at`` -- ``timestamptz`` nullable. Stamped by the service on
  approve / reject / expire; NULL while pending.

* ``created_at`` -- ``timestamptz`` NOT NULL. PG-side ``now()`` server
  default; ORM ``default=lambda: datetime.now(UTC)`` for SQLite.

* ``expires_at`` -- ``timestamptz`` nullable. Deadline after which the
  request is auto-expired. NULL means no deadline (request waits
  indefinitely); populated by the service from
  :attr:`Settings.approval_request_ttl_seconds`.

Audit columns
-------------

Approval requests produce **two synchronous audit rows** (see
:mod:`meho_backplane.operations.approval_queue`):

1. A "request" row written when the pending row is inserted. The row's
   ``method`` is ``'APPROVAL'``, ``path`` is ``'approval.request'``,
   and ``payload`` carries the ``approval_request_id`` and op metadata.
2. A "decision" row written when the request is approved / rejected /
   expired. Same ``method``; ``path`` is ``'approval.decision'``; the
   decision row is **not** inserted until the commit succeeds — the same
   synchronous-commit invariant the dispatcher uses.

These two rows are written synchronously inside the same transaction as
the corresponding ``approval_request`` mutation, so the DB is always
consistent: a pending row always has exactly one "request" audit row,
and an approved/rejected/expired row has exactly one "request" + one
"decision" audit row.

Indexes
-------

* ``approval_request_tenant_created_at_idx`` -- composite b-tree on
  ``(tenant_id, created_at)``. Drives "list pending requests for
  tenant, newest first".
* ``approval_request_status_idx`` -- b-tree on ``status``. Drives
  "find all pending requests" for the expiry sweep and the queue
  surface.
* ``approval_request_run_id_idx`` -- b-tree on ``run_id``. Drives
  "find pending requests for run X" (resume path after agent-run
  transitions back to ``awaiting_approval``).

Dialect portability
-------------------

Mirrors migration ``0017`` (agent_run):

* ``id`` server default -- ``gen_random_uuid()`` on PG; the ORM
  ``default=uuid.uuid4`` covers SQLite.
* ``created_at`` / ``expires_at`` -- ``timestamptz``. PG server default
  ``now()`` on ``created_at``; ORM ``default=lambda: datetime.now(UTC)``
  covers SQLite. ``expires_at`` has no server default (nullable; the
  service sets it from settings).
* ``status`` server default -- portable string literal on both dialects.
* ``proposed_effect`` -- JSONB on PG (binary, GIN-friendly), generic
  JSON on SQLite.
* The ``CHECK (col IN (...))`` constraints compile identically on both
  dialects.

Reversibility contract
----------------------

``upgrade()`` creates the table and its three indexes; ``downgrade()``
drops the indexes then the table in inverse order, following the same
symmetric convention as migrations ``0007``, ``0012``, ``0013``,
``0017``.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0020"
down_revision: str | None = "0017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


#: Closed ``approval_request.status`` vocabulary -- kept in lock-step with
#: :class:`meho_backplane.db.models.ApprovalRequestStatus`. Duplicated here
#: as a literal tuple (not imported) so the migration's recorded DDL is a
#: frozen snapshot independent of any later edit to the model enum — the same
#: self-contained discipline migration ``0007`` follows for graph-node kinds.
#: The drift guard in :mod:`tests.test_migration_0020_approval_request`
#: asserts the model enum and the live ``CHECK`` constraint agree.
_APPROVAL_REQUEST_STATUSES: tuple[str, ...] = (
    "pending",
    "approved",
    "rejected",
    "expired",
)


def _check_in(column: str, values: tuple[str, ...]) -> str:
    """Render a portable ``column IN ('a', 'b', ...)`` CHECK body."""
    quoted = ", ".join(f"'{v}'" for v in values)
    return f"{column} IN ({quoted})"


def upgrade() -> None:
    """Create the ``approval_request`` table and its indexes."""
    bind = op.get_bind()
    is_postgres = bind.dialect.name == "postgresql"

    # Portable JSONB -> JSON variant; same pattern as audit_log.payload
    # and agent_run.output. PG gets binary JSONB; SQLite gets text JSON.
    proposed_effect_type = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")

    op.create_table(
        "approval_request",
        sa.Column(
            "id",
            sa.Uuid(),
            primary_key=True,
            nullable=False,
            server_default=sa.text("gen_random_uuid()") if is_postgres else None,
        ),
        # Real FK -- clean-slate substrate, no ondelete (tenant deletion
        # must clear requests first; NO ACTION blocks the cascade).
        sa.Column(
            "tenant_id",
            sa.Uuid(),
            sa.ForeignKey("tenant.id"),
            nullable=False,
        ),
        # Soft-FK to agent_run.id -- NULL for non-agent-run requests.
        sa.Column("run_id", sa.Uuid(), nullable=True),
        # RFC 8693 delegation pair -- matches agent_run.identity_sub / _act.
        sa.Column("principal_sub", sa.Text(), nullable=False),
        sa.Column("principal_act", sa.Text(), nullable=True),
        # Dispatch coordinates needed for resume.
        sa.Column("op_id", sa.Text(), nullable=False),
        sa.Column("connector_id", sa.Text(), nullable=False),
        sa.Column("target_id", sa.Uuid(), nullable=True),
        sa.Column("params_hash", sa.Text(), nullable=False),
        # Human-readable proposed effect for the reviewer.
        sa.Column(
            "proposed_effect",
            proposed_effect_type,
            nullable=False,
            server_default=sa.text("'{}'") if is_postgres else sa.text("'{}'"),
        ),
        # Lifecycle columns.
        sa.Column(
            "status",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'pending'"),
        ),
        sa.Column("reviewed_by", sa.Text(), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()") if is_postgres else None,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        # Closed enum -- portable IN(...) CHECK. Drift guard in test_migration.
        sa.CheckConstraint(
            _check_in("status", _APPROVAL_REQUEST_STATUSES),
            name="ck_approval_request_status",
        ),
    )

    op.create_index(
        "approval_request_tenant_created_at_idx",
        "approval_request",
        ["tenant_id", "created_at"],
        postgresql_using="btree",
    )
    op.create_index(
        "approval_request_status_idx",
        "approval_request",
        ["status"],
        postgresql_using="btree",
    )
    op.create_index(
        "approval_request_run_id_idx",
        "approval_request",
        ["run_id"],
        postgresql_using="btree",
    )


def downgrade() -> None:
    """Drop the indexes then the ``approval_request`` table."""
    op.drop_index("approval_request_run_id_idx", table_name="approval_request")
    op.drop_index("approval_request_status_idx", table_name="approval_request")
    op.drop_index("approval_request_tenant_created_at_idx", table_name="approval_request")
    op.drop_table("approval_request")
