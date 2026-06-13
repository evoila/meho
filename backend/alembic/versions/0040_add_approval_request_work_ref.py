# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Add ``approval_request.work_ref`` for external change-ticket correlation.

Revision ID: 0040
Revises: 0039
Create Date: 2026-06-13

Task #1659 (work_ref I2-T1) under Initiative #1653, Goal #1651. A parked
approval -- the durable change-authorisation record -- carried no link to
the out-of-band ticket that authorised the change. Migration ``0039``
added ``audit_log.work_ref`` (I1-T1 #1655); this migration extends the
same opaque reference onto the ``approval_request`` row so the parked
request, its decision audit row, and the re-dispatched op's audit rows
all share one change-ticket ref.

What this migration adds
------------------------

* ``approval_request.work_ref text`` -- nullable. The external
  change-ticket reference for the dispatch that parked the request --
  a GitHub issue (``"gh:evoila/meho#1"``), a Jira key, a CR id. Set at
  creation from the request-time
  :data:`meho_backplane.operations._audit.work_ref_var` binding (the same
  ContextVar mechanism that carries ``run_id``), re-bound on re-dispatch
  so the approved op's audit rows inherit the ref. ``NULL`` when no
  work_ref was bound -- pre-#1659 rows and direct operator dispatches
  without a ticket stay ``NULL``. No backfill.
* ``approval_request_work_ref_idx`` -- b-tree index on the new column,
  for the ``meho approvals list --work-ref gh:evoila/meho#N`` filter.

Why no foreign key / NOT NULL
-----------------------------

Same soft-column discipline as ``audit_log.work_ref`` (0039),
``run_id`` (0017 mirror), and ``target_id`` (0004): nullable, no server
default (the ORM ``default`` of ``None`` works on both PG and SQLite),
no FK clause -- the migration is trivially reversible on a populated
table. ``work_ref`` is an opaque cross-system reference string, not a
row id in this schema, so there is no table to point a FK at.

Reversibility contract
----------------------

``downgrade()`` reverses ``upgrade()`` in order: drop the index, then
the column. Pure generic SQLAlchemy DDL, portable across PG and SQLite.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0040"
down_revision: str | None = "0039"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add ``approval_request.work_ref`` column + named index."""
    op.add_column(
        "approval_request",
        sa.Column("work_ref", sa.Text(), nullable=True),
    )
    op.create_index(
        "approval_request_work_ref_idx",
        "approval_request",
        ["work_ref"],
        postgresql_using="btree",
    )


def downgrade() -> None:
    """Reverse the upgrade -- drop the index and column."""
    op.drop_index("approval_request_work_ref_idx", table_name="approval_request")
    op.drop_column("approval_request", "work_ref")
