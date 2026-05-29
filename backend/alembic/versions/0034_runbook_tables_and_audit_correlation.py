# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Runbook schema tables + ``audit_log`` run/step correlation columns.

Revision ID: 0034
Revises: 0033
Create Date: 2026-05-29

Storage substrate for Task #1292 (G12.1-T1) under Initiative #1196
(G12.1 Runbook schema + dispatcher correlation). Pure DDL work: three
new tables and two additive nullable columns on ``audit_log``. No
business logic is wired here; that belongs to sibling Tasks T2 (dispatcher
contextvar plumbing) and T3 (tool / MCP / REST surface).

What this migration adds
------------------------

Three new tables
~~~~~~~~~~~~~~~~

**``runbook_templates``** — immutable versioned recipes. A template is
the static definition of a multi-step procedure (e.g. "drain a K8s node
safely"). Each ``(tenant_id, slug, version)`` triple is unique; once a
template row reaches ``status='published'`` the G12.2 write layer rejects
further edits (a new version must be created). The ``steps`` column is a
JSONB array of step descriptors; shape validation (discriminated
``type: operation_call`` / ``type: manual`` unions) lives in the Pydantic
layer (G12.2). Portable ``sa.JSON`` in the migration; JSONB on PG via
the model's ``_PORTABLE_JSON.with_variant`` (same pattern as every
JSON column since migration ``0003``).

**``runbook_runs``** — execution state machine. One row per template
invocation. ``template_slug`` + ``template_version`` are pinned at run
start so later template edits cannot alter an in-flight run's step list.
``params`` carries the substitution context for ``${run.params.X}``
expressions (G12.3); defaults to ``{}`` so a params-less run remains
insertable without ambiguity. The ``state`` column drives the three-state
machine (``in_progress`` → ``completed`` | ``abandoned``). No ``params``
server-default on SQLite — dialect detection at upgrade time follows
the ``0027`` pattern.

**``runbook_run_step_states``** — per-(run, step) state machine. Child
of ``runbook_runs`` via a real ``ForeignKey("runbook_runs.run_id",
ondelete="CASCADE")`` so deleting a run row cascades cleanly. The
composite PK ``(run_id, step_id)`` covers the per-run advance query
without an additional index. ``verify_response`` is nullable JSONB — it
captures the operator's confirmation (``yes`` / ``no`` / ``escalate`` for
``confirm`` steps) or the dispatched-call result (for ``operation_call``
steps).

Two additive columns on ``audit_log``
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

**``audit_log.run_id``** — nullable UUID soft-FK to
``runbook_runs.run_id``. The G12.1-T2 dispatcher contextvar populates it
for every operation issued inside a runbook run. NULL for all pre-G12.1
rows and for operations issued outside a run context. Same soft-FK
discipline as ``parent_audit_id`` / ``target_id`` / ``agent_session_id``
(documented in the module docstring of ``db/models.py``).

**``audit_log.step_id``** — nullable Text. Set alongside ``run_id``;
identifies the specific step within the template that triggered the
operation. Both columns together form the join key between an audit row
and a ``runbook_run_step_states`` row.

One new index on ``audit_log``
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

**``audit_log_run_id_idx``** — b-tree on ``run_id``. Drives the
"show all audit rows for this run" query path used by the G12.3 senior
review and G8.x audit-query surfaces.

Why additive (not amend ``0030``)
----------------------------------

Same reversible-additive discipline established by ``0006``+. Migration
``0033`` is merged to ``main`` and applied in CI/dev DBs. New requirement =
new migration head. This migration is purely additive in ``upgrade()``
and reversible in ``downgrade()`` — the ``check_migration_compat.py``
guard in CI will confirm no destructive pattern exists in ``upgrade()``.

Dialect-portability decisions
------------------------------

* JSON columns use generic ``sa.JSON`` in the migration; the model pins
  ``JSONB`` on PostgreSQL via ``_PORTABLE_JSON.with_variant``. Same
  pattern as every JSON column since ``0003``.
* ``nullable=True`` on the two new ``audit_log`` columns. Additive
  column on a populated table without a server default; existing rows
  carry NULL. Helm-rollback compatible (Goal #11 DoD §3).
* The ``params`` column on ``runbook_runs`` is NOT NULL with ``'{}'`` as
  the PG server default; on SQLite the server default is omitted and
  the ORM's ``default=dict`` provides the Python-side value for dev/test
  inserts (same pattern as ``event_outbox.payload`` in ``0027``).
* The ``runbook_run_step_states.verify_response`` column is nullable
  JSON — NULL while the step is pending/in-progress, populated on
  completion.
* Composite PK ``(run_id, step_id)`` on ``runbook_run_step_states`` is
  declared via ``primary_key=True`` on both columns in ``op.create_table``
  (no separate ``PrimaryKeyConstraint`` needed — Alembic infers it from
  the pair of ``primary_key=True`` column declarations).

Creation order
--------------

``runbook_templates`` first (no FKs), then ``runbook_runs`` (no FKs to
the other two new tables), then ``runbook_run_step_states`` (real FK to
``runbook_runs.run_id``). ``downgrade()`` drops in reverse.

Reversibility contract
-----------------------

``downgrade()`` drops the audit index, then the two audit columns
(in reverse add order), then the child-to-parent tables in reverse
creation order, then the parent tables. ``op.drop_index`` with
``table_name`` is supplied for every index so Alembic's dialect router
can emit the correct dialect-specific SQL without a table scan.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0034"
down_revision: str | None = "0033"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create three runbook tables + two audit_log columns + one audit index."""
    bind = op.get_bind()
    is_postgres = bind.dialect.name == "postgresql"

    # ------------------------------------------------------------------
    # 1. runbook_templates — versioned recipe definitions.
    # ------------------------------------------------------------------
    op.create_table(
        "runbook_templates",
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("slug", sa.Text(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        # JSONB on PG (via model _PORTABLE_JSON.with_variant); generic JSON
        # in the migration for dialect portability (0030 precedent).
        sa.Column("steps", sa.JSON(), nullable=False),
        sa.Column("target_kind", sa.Text(), nullable=True),
        sa.Column(
            "status",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'draft'"),
        ),
        sa.Column("created_by", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("edited_by", sa.Text(), nullable=False),
        sa.Column("edited_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('draft', 'published', 'deprecated')",
            name="ck_runbook_templates_status",
        ),
    )

    # UNIQUE b-tree on (tenant_id, slug, version) — runbook templates are
    # always tenant-scoped, so a single full unique index suffices (no
    # partial-index split like operation_group_global_idx / _tenant_idx).
    op.create_index(
        "runbook_templates_tenant_slug_version_idx",
        "runbook_templates",
        ["tenant_id", "slug", "version"],
        unique=True,
        postgresql_using="btree",
    )

    # b-tree on (tenant_id, status) — drives the runbook_list_templates
    # query path (G12.2: "list templates for tenant, optionally filtered
    # by status").
    op.create_index(
        "runbook_templates_tenant_status_idx",
        "runbook_templates",
        ["tenant_id", "status"],
        postgresql_using="btree",
    )

    # ------------------------------------------------------------------
    # 2. runbook_runs — execution state machine.
    # ------------------------------------------------------------------
    # ``params`` server default: PG only — ``'{}'`` is the empty-JSON-object
    # literal; SQLite omits the server default and relies on the ORM-side
    # ``default=dict`` (same pattern as event_outbox.payload in 0027).
    params_server_default = sa.text("'{}'") if is_postgres else None

    op.create_table(
        "runbook_runs",
        sa.Column("run_id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("template_slug", sa.Text(), nullable=False),
        sa.Column("template_version", sa.Integer(), nullable=False),
        sa.Column("assigned_to", sa.Text(), nullable=False),
        sa.Column("target", sa.Text(), nullable=False),
        sa.Column(
            "params",
            sa.JSON(),
            nullable=False,
            server_default=params_server_default,
        ),
        sa.Column(
            "state",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'in_progress'"),
        ),
        sa.Column("started_by", sa.Text(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("abandoned_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "state IN ('in_progress', 'completed', 'abandoned')",
            name="ck_runbook_runs_state",
        ),
    )

    # b-tree on (tenant_id, assigned_to, state) — drives the priming query
    # (G12.4: "in-progress runs assigned to this operator") and
    # runbook_list_runs (G12.3).
    op.create_index(
        "runbook_runs_tenant_assigned_state_idx",
        "runbook_runs",
        ["tenant_id", "assigned_to", "state"],
        postgresql_using="btree",
    )

    # b-tree on (tenant_id, template_slug, template_version) — drives the
    # post-completion read-allowance lookup (G12.3: "did this operator run
    # this template?").
    op.create_index(
        "runbook_runs_tenant_template_idx",
        "runbook_runs",
        ["tenant_id", "template_slug", "template_version"],
        postgresql_using="btree",
    )

    # ------------------------------------------------------------------
    # 3. runbook_run_step_states — per-(run, step) state.
    # ------------------------------------------------------------------
    # Real FK to runbook_runs.run_id with CASCADE delete — this is a new-
    # table child relationship (same pattern as GraphEdge → GraphNode in
    # db/models.py). The composite PK (run_id, step_id) is inferred by
    # Alembic from the two primary_key=True column declarations.
    op.create_table(
        "runbook_run_step_states",
        sa.Column(
            "run_id",
            sa.Uuid(),
            sa.ForeignKey("runbook_runs.run_id", ondelete="CASCADE"),
            primary_key=True,
            nullable=False,
        ),
        sa.Column("step_id", sa.Text(), primary_key=True, nullable=False),
        sa.Column(
            "state",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'pending'"),
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("verify_response", sa.JSON(), nullable=True),
        sa.CheckConstraint(
            "state IN ('pending', 'in_progress', 'verified', 'failed')",
            name="ck_runbook_run_step_states_state",
        ),
    )

    # ------------------------------------------------------------------
    # 4. audit_log — two new nullable correlation columns.
    # ------------------------------------------------------------------
    # Soft-FK to runbook_runs.run_id — same discipline as parent_audit_id /
    # target_id / agent_session_id (nullable, no DB-level FK constraint).
    # Populated by the G12.1-T2 dispatcher contextvar; pre-G12.1 rows and
    # non-run operations leave both columns NULL.
    op.add_column(
        "audit_log",
        sa.Column("run_id", sa.Uuid(), nullable=True),
    )
    op.add_column(
        "audit_log",
        sa.Column("step_id", sa.Text(), nullable=True),
    )

    # ------------------------------------------------------------------
    # 5. audit_log — index on run_id.
    # ------------------------------------------------------------------
    # Drives the "show all audit rows for this run" query (G12.3 / G8.x
    # senior-review surface).
    op.create_index(
        "audit_log_run_id_idx",
        "audit_log",
        ["run_id"],
        postgresql_using="btree",
    )


def downgrade() -> None:
    """Reverse the upgrade — drop indexes, columns, and tables in reverse order."""
    # Drop the audit_log index and new columns first.
    op.drop_index("audit_log_run_id_idx", table_name="audit_log")
    op.drop_column("audit_log", "step_id")
    op.drop_column("audit_log", "run_id")

    # Drop child table before parents (FK dependency).
    op.drop_table("runbook_run_step_states")

    # Drop runbook_runs indexes before the table.
    op.drop_index("runbook_runs_tenant_template_idx", table_name="runbook_runs")
    op.drop_index("runbook_runs_tenant_assigned_state_idx", table_name="runbook_runs")
    op.drop_table("runbook_runs")

    # Drop runbook_templates indexes before the table.
    op.drop_index("runbook_templates_tenant_status_idx", table_name="runbook_templates")
    op.drop_index("runbook_templates_tenant_slug_version_idx", table_name="runbook_templates")
    op.drop_table("runbook_templates")
