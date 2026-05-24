# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Add ``audit_log.agent_session_id`` for MCP-session audit correlation.

Revision ID: 0014
Revises: 0013
Create Date: 2026-05-24

This migration is the schema foundation of Task #1009 (G8.2-T1 audit
replay) under Initiative #377. It carries the column every other G8.2
task reads or writes, but ships **no write path** -- the column lands
NULL on every row until #1009's sibling T2 wires the MCP
``Mcp-Session-Id`` header capture into ``write_mcp_audit_row``.

What this migration adds
------------------------

* ``audit_log.agent_session_id uuid`` -- nullable. The MCP-session
  correlation id. Populated only on **MCP** audit rows, sourced from
  the inbound ``Mcp-Session-Id`` header (wired in T2). Chassis
  HTTP-side audit rows are not agent sessions by design, so they leave
  the column NULL; pre-G8.2 rows stay NULL too (no backfill -- that is
  explicitly out of scope per #377). ``meho audit replay
  <session-id>`` (T6) groups rows by this value to reconstruct an
  agent's MCP session timeline.
* ``audit_log_agent_session_id_idx`` -- b-tree index on the new
  column. Drives the ``WHERE agent_session_id = ?`` probe that the
  replay query (T3 un-gates the filter, T6 builds the CLI verb) runs.

Why no foreign key clause in v0.2
---------------------------------

Identical rationale to the soft-FK columns ``audit_log.tenant_id``
(0002), ``audit_log.target_id`` (0004), and the audit-tree linkage
column added in 0006: keeping the column shape *soft* (no FK clause,
no NOT NULL)
makes the migration trivially reversible on a populated table and
defers any tightening to a dedicated future migration. There is no
``agent_session`` table to point a FK at -- the session id is an
opaque correlation key sourced from the MCP transport header, not a
row identifier in this schema. Any FK / NOT NULL tightening is
v0.2.next territory (per #377 Out of scope).

Dialect-portability decisions
------------------------------

Mirrors the same discipline 0002 / 0004 / 0006 established: pure DDL,
no server defaults (Python-side ORM default of ``None`` works on both
PG and SQLite), and a named index that is dropped explicitly in
``downgrade()`` so the inverse works cleanly on both dialects.

Reversibility contract
----------------------

``downgrade()`` undoes everything ``upgrade()`` created in reverse
order: drop the agent_session_id index -> drop the agent_session_id
column. SQLite-portable because every step uses generic SQLAlchemy DDL
the dialect understands without quoting tricks.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0014"
down_revision: str | None = "0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add ``audit_log.agent_session_id`` column + named index."""
    op.add_column(
        "audit_log",
        sa.Column("agent_session_id", sa.Uuid(), nullable=True),
    )
    op.create_index(
        "audit_log_agent_session_id_idx",
        "audit_log",
        ["agent_session_id"],
        postgresql_using="btree",
    )


def downgrade() -> None:
    """Reverse the upgrade -- drop the index and column."""
    op.drop_index("audit_log_agent_session_id_idx", table_name="audit_log")
    op.drop_column("audit_log", "agent_session_id")
