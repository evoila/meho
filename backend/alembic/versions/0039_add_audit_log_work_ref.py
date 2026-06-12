# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Add ``audit_log.work_ref`` for external change-ticket correlation.

Revision ID: 0039
Revises: 0038
Create Date: 2026-06-12

Schema keystone of Task #1655 (work_ref I1-T1) under Initiative #1652,
Goal #1651. No governed MEHO object can currently be correlated to an
external change ticket. This migration adds the column that lets the
audit trail carry an opaque reference to the out-of-band change record
that authorised an operation -- a GitHub issue (``"gh:evoila/meho#1"``),
a Jira key, a CR id -- threaded by the same ContextVar mechanism that
already carries ``run_id`` / ``agent_session_id`` / ``parent_audit_id``.

What this migration adds
------------------------

* ``audit_log.work_ref text`` -- nullable. The external change-ticket
  reference for the operation that produced the row. Populated only when
  a ``work_ref`` is bound on the request / agent-loop ContextVar
  (:data:`meho_backplane.operations._audit.work_ref_var`), read by the
  three primary audit writers (chassis HTTP, dispatcher DISPATCH, MCP).
  The bind *source* is a separate task (I1-T2), so on this task's own
  the column stays ``NULL`` except where a caller binds the var
  directly. ``NULL`` is also the correct value for the system-internal
  writers (memory / topology / reaper / ui-session) that legitimately
  act without an external ticket. No backfill -- pre-#1655 rows stay
  ``NULL``.
* ``audit_log_work_ref_idx`` -- b-tree index on the new column, for the
  "what did change ticket X authorise?" audit query the I1-T3 filter
  surfaces.

Why no foreign key / NOT NULL
-----------------------------

Same soft-column discipline as ``actor_sub`` (0021), ``parent_audit_id``
(0006), ``agent_session_id`` (0014), ``tenant_id`` (0002), and
``target_id`` (0004): nullable, no server default (the ORM ``default``
of ``None`` works on both PG and SQLite), no FK clause -- so the
migration is trivially reversible on a populated table. ``work_ref`` is
an opaque cross-system reference string (``"gh:evoila/meho#1"``), not a
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
revision: str = "0039"
down_revision: str | None = "0038"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add ``audit_log.work_ref`` column + named index."""
    op.add_column(
        "audit_log",
        sa.Column("work_ref", sa.Text(), nullable=True),
    )
    op.create_index(
        "audit_log_work_ref_idx",
        "audit_log",
        ["work_ref"],
        postgresql_using="btree",
    )


def downgrade() -> None:
    """Reverse the upgrade -- drop the index and column."""
    op.drop_index("audit_log_work_ref_idx", table_name="audit_log")
    op.drop_column("audit_log", "work_ref")
