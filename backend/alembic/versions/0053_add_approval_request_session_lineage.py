# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Add ``approval_request.agent_session_id`` + ``request_audit_id`` (#2086).

Revision ID: 0053
Revises: 0052
Create Date: 2026-07-07

Initiative #2151 (Execution-ledger & run-outcome integrity, v0.19.0),
Task #2086. Approval-gated dispatches were invisible to the G8.2
session-replay surface (``GET /api/v1/audit/sessions/{id}/replay``):
the park → decide → execute chain's audit rows carried neither an
``agent_session_id`` anchor nor a ``parent_audit_id`` back-link, so the
recursive-CTE closure (anchored on ``agent_session_id``, walked over
``parent_audit_id``) never reached them — ``{root: [], row_count: 0}``
for a chain whose rows all exist and are queryable via ``/audit/query``.

The root cause is a context boundary: the approve / resume surfaces run
on a *different task* (a different operator's request) than the parking
dispatch, so the session contextvars bound at the parking boundary are
gone by the time the decision + re-dispatch audit rows are written. The
fix is the same durability move ``work_ref`` made in ``0040`` (#1659):
persist the lineage on the parked row at creation time and re-hydrate it
on every later lifecycle step.

Two nullable soft-FK columns on ``approval_request``:

* ``agent_session_id`` -- UUID. The session the parking dispatch
  belonged to (the agent run id inside an agent loop; the
  ``Mcp-Session-Id`` for a direct MCP operator dispatch), resolved at
  creation via ``operations._audit.resolve_agent_session_id``. Mirrors
  ``audit_log.agent_session_id`` (``0014``); no FK — same soft-reference
  discipline.

* ``request_audit_id`` -- UUID. The primary key of the
  ``approval.request`` audit row written in the same transaction as the
  parked row. Decision audit rows and the resumed dispatch's audit row
  set ``parent_audit_id`` to this value, linking the chain into one
  replay subtree. Soft-FK to ``audit_log.id``; no constraint (audit rows
  are append-only and never deleted, but the reference is correlation,
  not integrity).

Soft-column discipline mirrors ``0036`` / ``0040``: nullable, no server
default (Python-side ``None``), reversible. Pre-0053 rows keep NULLs and
every consumer treats NULL as "lineage unknown" (the pre-fix behaviour).
No indexes: neither column serves a filter path — both are read off a
row already loaded by primary key.

Reversibility contract
----------------------

``downgrade()`` drops both columns. SQLite's ALTER TABLE drop-column has
been supported since 3.35.0 (we're on 3.45+); Alembic's batch-mode
fallback isn't required.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0053"
down_revision: str | None = "0052"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add the nullable lineage columns to ``approval_request``."""
    op.add_column(
        "approval_request",
        sa.Column(
            "agent_session_id",
            sa.Uuid(),
            nullable=True,
        ),
    )
    op.add_column(
        "approval_request",
        sa.Column(
            "request_audit_id",
            sa.Uuid(),
            nullable=True,
        ),
    )


def downgrade() -> None:
    """Drop the two lineage columns added in :func:`upgrade`."""
    op.drop_column("approval_request", "request_audit_id")
    op.drop_column("approval_request", "agent_session_id")
