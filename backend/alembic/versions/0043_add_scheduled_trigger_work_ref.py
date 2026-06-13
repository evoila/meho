# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Add ``scheduled_trigger.work_ref`` for change-ticket inheritance.

Revision ID: 0043
Revises: 0042
Create Date: 2026-06-13

Task #1663 (work_ref I3-T3) under Initiative #1654, Goal #1651. A
scheduled trigger -- the authorizing definition for a recurring agent
run -- carries no link to the change ticket it works under, and the
trigger -> dispatched-run seam carries no ref to inherit. Migration
``0039`` added ``audit_log.work_ref`` (the column the dispatch audit
writers stamp from the shared
:data:`meho_backplane.operations._audit.work_ref_var` ContextVar),
``0041`` added ``agent_run.work_ref`` (the durable bind source for a
single run), ``0042`` added ``runbook_runs.work_ref``; this migration
adds the *trigger-level* bind source: a ``work_ref`` recorded on the
``scheduled_trigger`` row at create time. When the trigger fires, the
scheduler binds ``work_ref_var`` from this column around the dispatched
run, so the dispatched run's ``agent_run.work_ref`` and every audit row
the run produces inherit the trigger's ref end-to-end.

What this migration adds
------------------------

* ``scheduled_trigger.work_ref text`` -- nullable. The opaque external
  change-ticket reference (a GitHub issue ``"gh:evoila/meho#13"``, a
  Jira key, a CR id) of the change record the trigger -- and every run
  it dispatches -- works under. Set at create time; ``None`` when no
  ticket is bound. ``NULL`` for pre-#1663 rows. Set-at-create-only --
  triggers have no UPDATE path. No backfill.
* ``scheduled_trigger_tenant_work_ref_idx`` -- composite b-tree index
  on ``(tenant_id, work_ref)``, driving the tenant-scoped exact-match
  ``--work-ref`` filter the scheduled-trigger list surfaces (mirrors the
  ``agent_run_tenant_work_ref_idx`` shape of 0041 and the
  ``runbook_runs_tenant_work_ref_idx`` shape of 0042).

Why no foreign key / NOT NULL
-----------------------------

Same soft-column discipline as ``audit_log.work_ref`` (0039),
``agent_run.work_ref`` (0041), and ``runbook_runs.work_ref`` (0042):
nullable, no server default (the ORM ``default`` of ``None`` works on
both PG and SQLite), no FK clause -- the migration is trivially
reversible on a populated table. ``work_ref`` is an opaque cross-system
reference string, not a row id in this schema, so there is no table to
point a FK at.

Reversibility contract
----------------------

``downgrade()`` reverses ``upgrade()`` in order: drop the index, then
the column. Pure generic SQLAlchemy DDL, portable across PG and SQLite.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0043"
down_revision: str | None = "0042"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add ``scheduled_trigger.work_ref`` column + composite index."""
    op.add_column(
        "scheduled_trigger",
        sa.Column("work_ref", sa.Text(), nullable=True),
    )
    op.create_index(
        "scheduled_trigger_tenant_work_ref_idx",
        "scheduled_trigger",
        ["tenant_id", "work_ref"],
        postgresql_using="btree",
    )


def downgrade() -> None:
    """Reverse the upgrade -- drop the index and column."""
    op.drop_index(
        "scheduled_trigger_tenant_work_ref_idx",
        table_name="scheduled_trigger",
    )
    op.drop_column("scheduled_trigger", "work_ref")
