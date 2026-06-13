# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Add ``runbook_runs.work_ref`` for change-ticket correlation of runs.

Revision ID: 0042
Revises: 0041
Create Date: 2026-06-13

Task #1661 (work_ref I3-T1) under Initiative #1654, Goal #1651. A runbook
run carries no link to the change ticket it executes under, and its
per-step ``operation_call`` audit rows cannot be correlated to one
either. Migration ``0039`` added ``audit_log.work_ref`` (the column the
dispatcher's audit writer stamps from the shared
:data:`meho_backplane.operations._audit.work_ref_var` ContextVar); this
migration adds the *durable bind source* for runbook runs: a ``work_ref``
recorded on the run row at start, which the run engine binds onto
``work_ref_var`` around each step's dispatch so every step's audit row
inherits the same reference.

What this migration adds
------------------------

* ``runbook_runs.work_ref text`` -- nullable. The opaque external
  change-ticket reference (a GitHub issue ``"gh:evoila/meho#9"``, a Jira
  key, a CR id) of the change record the run executes under. ``NULL``
  when the run was started without a ticket -- the field is optional on
  ``meho.runbook.start``. No backfill -- pre-#1661 runs stay ``NULL``.
* ``runbook_runs_tenant_work_ref_idx`` -- composite b-tree index on
  ``(tenant_id, work_ref)``, driving the tenant-scoped exact-match
  ``--work-ref`` filter the runbook-run list surfaces.

Why no foreign key / NOT NULL
-----------------------------

Same soft-column discipline as ``audit_log.work_ref`` (0039) and the
existing ``runbook_runs`` soft-FK columns (``tenant_id`` /
``assigned_to``): nullable, no server default (the ORM ``default`` of
``None`` works on both PG and SQLite), no FK clause. ``work_ref`` is an
opaque cross-system reference string, not a row id in this schema, so
there is no table to point a FK at.

Reversibility contract
----------------------

``downgrade()`` reverses ``upgrade()`` in order: drop the index, then
the column. Pure generic SQLAlchemy DDL, portable across PG and SQLite.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0042"
down_revision: str | None = "0041"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add ``runbook_runs.work_ref`` column + composite index."""
    op.add_column(
        "runbook_runs",
        sa.Column("work_ref", sa.Text(), nullable=True),
    )
    op.create_index(
        "runbook_runs_tenant_work_ref_idx",
        "runbook_runs",
        ["tenant_id", "work_ref"],
        postgresql_using="btree",
    )


def downgrade() -> None:
    """Reverse the upgrade -- drop the index and column."""
    op.drop_index("runbook_runs_tenant_work_ref_idx", table_name="runbook_runs")
    op.drop_column("runbook_runs", "work_ref")
