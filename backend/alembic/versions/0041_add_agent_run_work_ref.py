# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Add ``agent_run.work_ref`` for change-ticket correlation of agent runs.

Revision ID: 0041
Revises: 0040
Create Date: 2026-06-13

Task #1662 (work_ref I3-T2) under Initiative #1654, Goal #1651. An agent
run carries no link to the change ticket it works under, and the
agent-run list cannot be narrowed to "what did change ticket X run?".
Migration ``0039`` added ``audit_log.work_ref`` (the column the dispatcher
audit writers stamp from the shared
:data:`meho_backplane.operations._audit.work_ref_var` ContextVar); this
migration adds the *durable bind source* for agent runs: a ``work_ref``
recorded on the ``agent_run`` row at create time, filterable on the
agent-run list and surfaced on the run's terminal ``agent_run.completed``
event.

This ``work_ref`` is a genuinely new column -- it must not be conflated
with ``agent_run.id`` (which doubles as the ``agent_session_id`` lineage
key, a UUID generated for *this* run). ``work_ref`` is an opaque external
reference set from outside the run; ``id`` is the run's own identity.

What this migration adds
------------------------

* ``agent_run.work_ref text`` -- nullable. The opaque external
  change-ticket reference (a GitHub issue ``"gh:evoila/meho#11"``, a Jira
  key, a CR id) of the change record the run works under. Set at create
  time from the request-time ``work_ref_var`` binding (the same ContextVar
  mechanism that carries the value onto ``audit_log.work_ref``, 0039), or
  ``None`` when no ticket is bound. ``NULL`` for pre-#1662 rows and direct
  runs without a ticket. Set-at-create-only -- no later mutation. No
  backfill.
* ``agent_run_tenant_work_ref_idx`` -- composite b-tree index on
  ``(tenant_id, work_ref)``, driving the tenant-scoped exact-match
  ``--work-ref`` filter the agent-run list surfaces (mirrors the
  ``runbook_runs_tenant_work_ref_idx`` shape of 0040).

Why no foreign key / NOT NULL
-----------------------------

Same soft-column discipline as ``audit_log.work_ref`` (0039),
``agent_definition_id`` / ``parent_run_id`` (the existing ``agent_run``
soft-FK columns), and ``tenant_id`` (0002): nullable, no server default
(the ORM ``default`` of ``None`` works on both PG and SQLite), no FK
clause -- the migration is trivially reversible on a populated table.
``work_ref`` is an opaque cross-system reference string, not a row id in
this schema, so there is no table to point a FK at.

Reversibility contract
----------------------

``downgrade()`` reverses ``upgrade()`` in order: drop the index, then
the column. Pure generic SQLAlchemy DDL, portable across PG and SQLite.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0041"
down_revision: str | None = "0040"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add ``agent_run.work_ref`` column + composite index."""
    op.add_column(
        "agent_run",
        sa.Column("work_ref", sa.Text(), nullable=True),
    )
    op.create_index(
        "agent_run_tenant_work_ref_idx",
        "agent_run",
        ["tenant_id", "work_ref"],
        postgresql_using="btree",
    )


def downgrade() -> None:
    """Reverse the upgrade -- drop the index and column."""
    op.drop_index("agent_run_tenant_work_ref_idx", table_name="agent_run")
    op.drop_column("agent_run", "work_ref")
