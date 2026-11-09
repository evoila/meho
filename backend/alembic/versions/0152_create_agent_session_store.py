# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Create the three tables of the agent session store.

Revision ID: 0152
Revises: 0151
Create Date: 2026-11-09

MEHO keeps a copy of the agent sessions that people upload. The files are
in S3-compatible object storage; these tables are the index
(``docs/codebase/session-store.md``).

What this migration adds
------------------------

* ``agent_session_record`` -- one row per session and tenant: who uploaded
  it, the project, the app, start and end, the state (open, ended or
  unfinished), the last upload time and the hash of each file.
* ``agent_session_upload`` -- one row per stored upload of a session: the
  object key, the archive hash and whether it went to quarantine.
* ``agent_session_retention`` -- one row per tenant that set its own
  retention period.

Reversibility
-------------

Purely additive. An older image never reads the tables, so a rollback is
safe. ``downgrade()`` drops the tables and their rows. The files in the
object storage stay; remove them by hand.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0152"
down_revision: str | None = "0151"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Alembic reads these names itself. List them in __all__, so the code-quality
# check does not report them as unused globals. Keep a plain list of strings
# with no type annotation: the check reads only that form.
__all__ = [
    "branch_labels",
    "depends_on",
    "down_revision",
    "downgrade",
    "revision",
    "upgrade",
]

_RECORD = "agent_session_record"
_UPLOAD = "agent_session_upload"
_RETENTION = "agent_session_retention"


def _json() -> sa.types.TypeEngine[object]:
    return sa.JSON().with_variant(JSONB(), "postgresql")


def upgrade() -> None:
    """Create the record, upload and retention tables."""
    op.create_table(
        _RECORD,
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("session_id", sa.Text(), nullable=False),
        sa.Column("uploaded_by_sub", sa.Text(), nullable=False),
        sa.Column("project", sa.Text(), nullable=False),
        sa.Column("app", sa.Text(), nullable=False),
        sa.Column("app_version", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("outcome", sa.Text(), nullable=True),
        sa.Column("files", _json(), nullable=False),
        sa.Column("upload_count", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("tenant_id", "session_id", name="agent_session_record_tenant_session"),
        sa.CheckConstraint(
            "state IN ('open', 'ended', 'unfinished')", name="ck_agent_session_record_state"
        ),
    )
    op.create_index("agent_session_record_tenant_seen_idx", _RECORD, ["tenant_id", "last_seen_at"])
    op.create_table(
        _UPLOAD,
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column(
            "record_id",
            sa.Uuid(),
            sa.ForeignKey(f"{_RECORD}.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("object_key", sa.Text(), nullable=False),
        sa.Column("archive_sha256", sa.Text(), nullable=False),
        sa.Column("archive_bytes", sa.Integer(), nullable=False),
        sa.Column("quarantined", sa.Boolean(), nullable=False),
        sa.Column("redactions", sa.Integer(), nullable=False),
        sa.Column("files", _json(), nullable=False),
        sa.Column("uploaded_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("record_id", "seq", name="agent_session_upload_record_seq"),
    )
    op.create_table(
        _RETENTION,
        sa.Column("tenant_id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("retention_days", sa.Integer(), nullable=False),
        sa.Column("updated_by_sub", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    """Drop the three tables; the index rows are lost."""
    op.drop_table(_RETENTION)
    op.drop_table(_UPLOAD)
    op.drop_index("agent_session_record_tenant_seen_idx", table_name=_RECORD)
    op.drop_table(_RECORD)
