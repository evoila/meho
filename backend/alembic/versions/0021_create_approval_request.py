# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Create the ``approval_request`` table for the G11.2 approval gate.

Revision ID: 0021
Revises: 0018
Create Date: 2026-05-25

This migration is the schema substrate of Initiative #803 (G11.2 Agent
identity + RBAC + approval), Task #818 (T5) — the approval surfacing
channel. The ``approval_request`` table is the durable state that the
T4 (#817) approval queue mechanics write and the T5 surfacing layer
(list/show/approve/reject REST + MCP + CLI) reads.

Note on down_revision
---------------------

``0018`` is the highest revision on ``main`` at the time of writing this
PR (``0018_seed_rdc_internal_conventions.py`` landed in main before this
PR was opened). PRs #1050 / #1051 (0019 / 0020) and the parallel T4 PR
(0020) each revise against their predecessor; this migration uses
``0018`` as ``down_revision`` and will be updated to chain from ``0020``
once the T4 approval-queue PR lands ahead of this one.

Schema
------

* ``id`` — UUID primary key. PG gets ``gen_random_uuid()``; ORM falls
  back to ``default=uuid.uuid4`` for SQLite / out-of-band inserts.

* ``tenant_id`` — UUID NOT NULL ``REFERENCES tenant(id)``. Every
  approval request is tenant-scoped; cross-tenant reads are
  structurally impossible. ``NO ACTION`` on delete (tenant deletion
  must drain requests first).

* ``agent_run_id`` — UUID nullable, **soft-FK** to ``agent_run.id``
  (no clause). The run that triggered the pause; NULL when the request
  was created by a direct REST call rather than an in-flight agent run.
  Soft-FK mirrors ``audit_log.parent_audit_id`` — defer any tightening
  until both tables are stable.

* ``principal_sub`` — Text NOT NULL. RFC 8693 ``sub`` claim: the
  principal the action runs *for*. Mirrors ``agent_run.identity_sub``.

* ``principal_act`` — Text nullable. RFC 8693 ``act`` claim: the agent
  acting on the subject's behalf. NULL for direct-operator requests.

* ``connector_id`` — Text NOT NULL. The connector the proposed
  operation targets.

* ``op_id`` — Text NOT NULL. The operation id (same shape as
  ``audit_log.op_id``).

* ``target_id`` — UUID nullable. The target the op would run against;
  NULL for connector-wide operations.

* ``params_hash`` — Text NOT NULL. SHA-256 over canonicalised params
  (same :func:`~meho_backplane.operations._validate.compute_params_hash`
  the dispatcher uses). Correlates retries without storing raw params
  in this row.

* ``proposed_effect`` — portable JSON -> JSONB. Human-readable
  description of what the operation would do, pre-computed at pause
  time from the EndpointDescriptor's ``llm_instructions`` + params.
  Nullable: an operator may approve without a rendered effect (e.g. a
  programmatically generated request).

* ``status`` — Text NOT NULL DEFAULT ``'pending'``. Closed enum:
  ``pending | approved | rejected | expired``. The
  ``ck_approval_request_status`` CHECK constraint enforces the
  closed vocabulary; the :class:`~meho_backplane.db.models.ApprovalStatus`
  enum and the constraint move in lock-step.

* ``reviewed_by`` — Text nullable. Operator ``sub`` who approved or
  rejected; NULL while pending or expired.

* ``decided_at`` — ``timestamptz`` nullable. Stamped on approve /
  reject; NULL while pending or expired.

* ``expires_at`` — ``timestamptz`` nullable. When set, the expiry
  sweep closes stale pending rows to ``expired`` and writes a second
  audit row. NULL = no expiry (operator-configured; the service
  defaults to 24 h when creating via the resume path).

* ``created_at`` — ``timestamptz`` NOT NULL. PG server default
  ``now()``; ORM ``default=lambda: datetime.now(UTC)`` for SQLite.

* ``request_audit_id`` — UUID nullable, soft-FK to
  ``audit_log.id``. The audit row recording the *pause* event; NULL
  when the approval was created outside the normal dispatch path (e.g.
  a test fixture). Soft-FK discipline mirrors
  ``tenant_convention_history.audit_id``.

* ``decision_audit_id`` — UUID nullable, soft-FK to ``audit_log.id``.
  The audit row recording the approve/reject *decision*; NULL while
  pending. Two separate soft-FKs make the request-row self-describing
  for forensic audit queries.

Indexes
-------

* ``approval_request_tenant_status_idx`` — ``(tenant_id, status)``.
  Drives the "list pending for tenant X" operator query; the most
  common operational read.
* ``approval_request_tenant_created_at_idx`` — ``(tenant_id, created_at)``.
  Drives the chronological listing surface.
* ``approval_request_agent_run_id_idx`` — ``agent_run_id``. Drives the
  "find all approval requests for this run" sub-query the run-status
  API uses.
* ``approval_request_expires_at_idx`` — ``expires_at`` (partial: WHERE
  status = 'pending'). Drives the expiry-sweep ``SELECT … WHERE
  expires_at < now() AND status = 'pending'`` efficiently. The partial
  predicate is PostgreSQL-only; SQLite falls back to a full-index scan,
  which is acceptable on the typically small approval table.

Dialect portability
-------------------

Mirrors the discipline of migrations 0007 / 0012 / 0013 / 0017:

* ``id`` server default — ``gen_random_uuid()`` on PG; ORM
  ``default=uuid.uuid4`` covers SQLite.
* ``created_at`` server default — ``now()`` on PG; ORM lambda covers
  SQLite.
* ``status`` server default — portable string literal on both.
* ``CHECK (col IN (...))`` constraints compile identically on PG and
  SQLite — same portable enforcement pattern as ``agent_run.status``.
* Partial index on ``expires_at`` — PG only (``postgresql_where``
  kwarg is silently ignored on SQLite, which creates a regular index
  instead).

Reversibility contract
----------------------

``upgrade()`` creates the table and its four indexes; ``downgrade()``
drops the indexes then the table in inverse order. Explicit index drops
keep the inverse symmetric across dialects.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0021"
down_revision: str | None = "0018"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


#: Closed ``approval_request.status`` vocabulary — kept in lock-step
#: with :class:`meho_backplane.db.models.ApprovalStatus`. Duplicated as
#: a literal tuple so the migration's recorded DDL is a frozen snapshot
#: independent of any later edit to the model enum — same self-contained
#: discipline migration 0017 uses for ``agent_run.status``.
_APPROVAL_STATUSES: tuple[str, ...] = (
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
        # Real FK — clean-slate substrate; tenant deletion must drain first.
        sa.Column(
            "tenant_id",
            sa.Uuid(),
            sa.ForeignKey("tenant.id"),
            nullable=False,
        ),
        # Soft-FK to agent_run.id — set when paused from an in-flight run.
        sa.Column("agent_run_id", sa.Uuid(), nullable=True),
        # RFC 8693 delegation pair — mirrors agent_run.identity_{sub,act}.
        sa.Column("principal_sub", sa.Text(), nullable=False),
        sa.Column("principal_act", sa.Text(), nullable=True),
        # Proposed operation identity.
        sa.Column("connector_id", sa.Text(), nullable=False),
        sa.Column("op_id", sa.Text(), nullable=False),
        sa.Column("target_id", sa.Uuid(), nullable=True),
        sa.Column("params_hash", sa.Text(), nullable=False),
        # Human-readable effect preview, nullable.
        sa.Column("proposed_effect", proposed_effect_type, nullable=True),
        # Lifecycle.
        sa.Column(
            "status",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'pending'"),
        ),
        sa.Column("reviewed_by", sa.Text(), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()") if is_postgres else None,
        ),
        # Soft-FKs to audit_log.id — request row + decision row.
        sa.Column("request_audit_id", sa.Uuid(), nullable=True),
        sa.Column("decision_audit_id", sa.Uuid(), nullable=True),
        # Closed status enum — portable IN(...) CHECK.
        sa.CheckConstraint(
            _check_in("status", _APPROVAL_STATUSES),
            name="ck_approval_request_status",
        ),
    )

    op.create_index(
        "approval_request_tenant_status_idx",
        "approval_request",
        ["tenant_id", "status"],
        postgresql_using="btree",
    )
    op.create_index(
        "approval_request_tenant_created_at_idx",
        "approval_request",
        ["tenant_id", "created_at"],
        postgresql_using="btree",
    )
    op.create_index(
        "approval_request_agent_run_id_idx",
        "approval_request",
        ["agent_run_id"],
        postgresql_using="btree",
    )
    # Partial index: only pending rows have a meaningful expires_at to sweep.
    # postgresql_where is ignored on SQLite (falls back to a full index).
    op.create_index(
        "approval_request_expires_at_idx",
        "approval_request",
        ["expires_at"],
        postgresql_using="btree",
        postgresql_where=sa.text("status = 'pending'"),
    )


def downgrade() -> None:
    """Drop the indexes then the ``approval_request`` table."""
    op.drop_index("approval_request_expires_at_idx", table_name="approval_request")
    op.drop_index("approval_request_agent_run_id_idx", table_name="approval_request")
    op.drop_index(
        "approval_request_tenant_created_at_idx", table_name="approval_request"
    )
    op.drop_index(
        "approval_request_tenant_status_idx", table_name="approval_request"
    )
    op.drop_table("approval_request")
