# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Create the ``agent_run`` table for the in-process agent runtime.

Revision ID: 0015
Revises: 0014
Create Date: 2026-05-24

This migration is the schema substrate of Initiative #802 (G11.1 Agent
runtime), Task #813 (T6). It adds the ``agent_run`` table -- one row per
LLM-agent invocation hosted in MEHO's process. The row ties a session's
tool calls together, makes a run inspectable + cancellable, and seeds
the audit/replay lineage: the row's ``id`` **is** the
``agent_session_id`` lineage key that G11.4/C2 binds into every
per-tool-call audit row (the column added by migration ``0014`` on
``audit_log``).

T6 ships **the table, its indexes, and its ORM model + lifecycle
service**. The lifecycle state machine (legal transitions, cancellation
path) lives in
:mod:`meho_backplane.operations.agent_run`; the invocation surface that
drives the runtime is G11.1-T4 (#811).

Schema
------

* ``id`` -- UUID primary key. PG production gets a
  ``gen_random_uuid()`` server default; SQLite leaves it to the ORM
  ``default=uuid.uuid4``. Doubles as the ``agent_session_id`` lineage
  key, so it must be globally unique without a central allocator --
  the chassis-wide UUID shape (``audit_log.id``, ``web_session.id``).

* ``agent_definition_id`` -- UUID nullable, **soft-FK** (no clause).
  The ``agent_definition`` table (G11.1-T2 / #809) lands in a sibling
  task in parallel; a hard FK here would couple the two migrations'
  ordering. Soft-FK discipline mirrors ``audit_log.target_id`` (0004):
  defer any tightening to a dedicated future migration once both tables
  are settled.

* ``tenant_id`` -- UUID NOT NULL with a real ``REFERENCES tenant(id)``
  FK. ``agent_run`` is a clean-slate substrate (no chassis-era rows),
  so the FK is enforced at the DB layer -- same discipline ``graph_node``
  (0007) / ``documents`` (0003) follow. No ``ondelete``: tenant
  deletion is a major operation that must clear the tenant's runs first;
  the default ``NO ACTION`` blocks the cascade.

* ``identity_sub`` -- Text NOT NULL. The RFC 8693 ``sub`` claim: the
  principal the agent acts *for*. Mirrors ``audit_log.operator_sub``
  (the chassis has no ``operator`` table; the Keycloak ``sub`` is the
  stable identifier).

* ``identity_act`` -- Text nullable. The RFC 8693 ``act`` claim: the
  agent principal acting on the subject's behalf. NULL when a human
  invokes a run directly with no delegation.

* ``trigger`` -- Text NOT NULL with a portable ``CHECK trigger IN
  (...)`` constraint enforcing the closed
  :class:`~meho_backplane.db.models.AgentRunTrigger` vocabulary
  (``direct`` / ``scheduled`` / ``event`` / ``agent-invoked``).

* ``model_tier`` -- Text NOT NULL. The logical tier the operator
  requested; the multi-provider resolver (G11.5) maps it to a concrete
  provider + model. Free-text -- the tier vocabulary is consumer-defined.

* ``provider`` / ``model`` -- Text nullable. The *resolved* provider +
  model the run executed against. NULL until the resolver runs (a
  ``pending`` run has not resolved them yet).

* ``status`` -- Text NOT NULL DEFAULT ``'pending'`` with a portable
  ``CHECK status IN (...)`` constraint enforcing the closed
  :class:`~meho_backplane.db.models.AgentRunStatus` lifecycle
  (``pending`` / ``running`` / ``awaiting_approval`` / ``succeeded`` /
  ``failed`` / ``cancelled``). The enum and the constraint move in
  lock-step; the drift guard in :mod:`tests.test_db_agent_run` asserts
  equality.

* ``turns`` -- Integer NOT NULL DEFAULT 0. Observable count of tool-use
  turns the loop has executed.

* ``cost`` -- ``Numeric(12, 6)`` nullable. **Stub until G11.5/C3** --
  recorded NULL in v0.2 so C3 can populate per-identity cost attribution
  without a follow-up migration. Numeric (not float) -- cost is
  money-shaped; exact decimal arithmetic.

* ``output`` -- portable JSON -> JSONB nullable. The run's final
  structured result; NULL until a terminal-with-result state.

* ``error`` -- Text nullable. Human-readable failure reason on a
  ``failed`` run; NULL otherwise. Distinct from ``output`` so failure
  diagnostics never masquerade as a result.

* ``parent_run_id`` -- UUID nullable, self-referential **soft-FK** (no
  clause). Set on agent-invoked child runs (G11.1-T5) so the
  composition tree is walkable. Same discipline as
  ``audit_log.parent_audit_id`` (0006).

* ``created_at`` -- ``timestamptz`` NOT NULL. PG-side ``now()`` server
  default; ORM ``default=lambda: datetime.now(UTC)`` for SQLite.

* ``started_at`` / ``ended_at`` -- ``timestamptz`` nullable. Stamped by
  the lifecycle service on ``pending`` -> ``running`` and on reaching a
  terminal state, respectively.

Indexes
-------

* ``agent_run_tenant_created_at_idx`` -- composite b-tree on
  ``(tenant_id, created_at)``. Drives the "list runs for tenant X,
  newest first" inspection surface (G11.1-T4).
* ``agent_run_status_idx`` -- b-tree on ``status``. Drives the "find
  all running / awaiting-approval runs" query an operator needs to
  inspect / cancel in-flight work.
* ``agent_run_parent_run_id_idx`` -- b-tree on ``parent_run_id``.
  Drives the composition-tree walk.

Dialect portability
-------------------

Mirrors the discipline migrations ``0007`` / ``0012`` / ``0013``
established:

* ``id`` server default -- ``gen_random_uuid()`` on PG (built-in since
  PG 13, the chassis floor); the ORM ``default=uuid.uuid4`` covers
  SQLite.
* ``created_at`` server default -- ``now()`` on PG; the ORM
  ``default=lambda: datetime.now(UTC)`` covers SQLite.
* ``status`` server default -- a portable string literal on both
  dialects.
* The ``CHECK (col IN (...))`` constraints compile identically on PG and
  SQLite -- the same portable enforcement ``graph_node.kind`` (0007)
  uses.

Reversibility contract
----------------------

``upgrade()`` creates the table and its three indexes; ``downgrade()``
drops the indexes then the table in inverse order. Explicit index drops
keep the inverse symmetric across both dialects (SQLite does not always
auto-cascade index drops on ``DROP TABLE``), matching the discipline
migrations ``0007`` / ``0012`` / ``0013`` established.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0015"
down_revision: str | None = "0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


#: Closed ``agent_run.status`` vocabulary -- kept in lock-step with
#: :class:`meho_backplane.db.models.AgentRunStatus`. Duplicated here as a
#: literal tuple (not imported) so the migration's recorded DDL is a
#: frozen snapshot independent of any later edit to the model enum -- the
#: same self-contained discipline migration ``0007`` follows for the
#: graph-node kind vocabulary. The drift guard in
#: :mod:`tests.test_db_agent_run` asserts the model enum and the live
#: ``CHECK`` constraint agree.
_AGENT_RUN_STATUSES: tuple[str, ...] = (
    "pending",
    "running",
    "awaiting_approval",
    "succeeded",
    "failed",
    "cancelled",
)

#: Closed ``agent_run.trigger`` vocabulary -- lock-step with
#: :class:`meho_backplane.db.models.AgentRunTrigger`.
_AGENT_RUN_TRIGGERS: tuple[str, ...] = (
    "direct",
    "scheduled",
    "event",
    "agent-invoked",
)


def _check_in(column: str, values: tuple[str, ...]) -> str:
    """Render a portable ``column IN ('a', 'b', ...)`` CHECK body."""
    quoted = ", ".join(f"'{v}'" for v in values)
    return f"{column} IN ({quoted})"


def upgrade() -> None:
    """Create the ``agent_run`` table and its indexes."""
    bind = op.get_bind()
    is_postgres = bind.dialect.name == "postgresql"

    # Portable JSONB -> JSON variant; same pattern audit_log.payload /
    # graph_node_history.snapshot use. PG gets binary JSONB; SQLite gets
    # text JSON. Nullable column -- no server default needed.
    output_type = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")

    op.create_table(
        "agent_run",
        sa.Column(
            "id",
            sa.Uuid(),
            primary_key=True,
            nullable=False,
            server_default=sa.text("gen_random_uuid()") if is_postgres else None,
        ),
        # Soft-FK to agent_definition.id (table lands in parallel #809).
        sa.Column(
            "agent_definition_id",
            sa.Uuid(),
            nullable=True,
        ),
        # Real FK -- clean-slate substrate, see module docstring.
        sa.Column(
            "tenant_id",
            sa.Uuid(),
            sa.ForeignKey("tenant.id"),
            nullable=False,
        ),
        # RFC 8693 delegation pair.
        sa.Column("identity_sub", sa.Text(), nullable=False),
        sa.Column("identity_act", sa.Text(), nullable=True),
        sa.Column("trigger", sa.Text(), nullable=False),
        sa.Column("model_tier", sa.Text(), nullable=False),
        # Resolved provider + model -- NULL until the resolver runs.
        sa.Column("provider", sa.Text(), nullable=True),
        sa.Column("model", sa.Text(), nullable=True),
        sa.Column(
            "status",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'pending'"),
        ),
        sa.Column(
            "turns",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        # Stub until G11.5/C3 -- Numeric (money-shaped), NULL in v0.2.
        sa.Column("cost", sa.Numeric(12, 6), nullable=True),
        sa.Column("output", output_type, nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        # Self-referential soft-FK -- set on agent-invoked child runs.
        sa.Column("parent_run_id", sa.Uuid(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()") if is_postgres else None,
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        # Closed enums -- portable IN(...) CHECKs. v0.2 vocabularies;
        # widening either requires a coordinated migration so the enum
        # and the check move in lock-step.
        sa.CheckConstraint(
            _check_in("status", _AGENT_RUN_STATUSES),
            name="ck_agent_run_status",
        ),
        sa.CheckConstraint(
            _check_in("trigger", _AGENT_RUN_TRIGGERS),
            name="ck_agent_run_trigger",
        ),
    )

    op.create_index(
        "agent_run_tenant_created_at_idx",
        "agent_run",
        ["tenant_id", "created_at"],
        postgresql_using="btree",
    )
    op.create_index(
        "agent_run_status_idx",
        "agent_run",
        ["status"],
        postgresql_using="btree",
    )
    op.create_index(
        "agent_run_parent_run_id_idx",
        "agent_run",
        ["parent_run_id"],
        postgresql_using="btree",
    )


def downgrade() -> None:
    """Drop the indexes then the ``agent_run`` table."""
    op.drop_index("agent_run_parent_run_id_idx", table_name="agent_run")
    op.drop_index("agent_run_status_idx", table_name="agent_run")
    op.drop_index("agent_run_tenant_created_at_idx", table_name="agent_run")
    op.drop_table("agent_run")
