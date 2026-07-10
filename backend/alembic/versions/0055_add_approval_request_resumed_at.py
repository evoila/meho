# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Add ``approval_request.resumed_at`` exactly-one-resumer claim (#2293).

Revision ID: 0055
Revises: 0054
Create Date: 2026-07-10

Initiative #2286 (G0.30 v0.20.0 dogfood hardening), Task #2293. Run-bound
approval requests had no exactly-one-resumer invariant, producing two
opposite failure modes:

* **Silent non-execution** -- ``/decide`` and the MCP by-id approve
  skipped re-dispatch whenever ``run_id`` was set, assuming the in-process
  broadcast waiter (#1117) would resume the run. When the waiter was gone
  (wait-timeout exceeded, pod restart, run cancelled) the approval
  committed, the audit said "approved", and nothing executed.

* **Double execution** -- REST ``/approve`` and the UI approve path
  re-dispatched ``_approved=True`` unconditionally; if the waiter was
  still alive it also resumed, so one approval could dispatch an
  approval-gated write twice.

The fix is an exactly-one-resumer *claim*: a single nullable timestamp
column every resumer of an approved op must win via a conditional
``UPDATE ... WHERE resumed_at IS NULL`` before it re-dispatches. The
winner (one row touched) executes; a loser (zero rows touched) no-ops.
The claim is what lets ``/decide`` + MCP safely fall back to server-side
re-dispatch when the claim is free (covers waiter-gone) while preventing
the ``/approve`` / UI double-dispatch when the waiter is alive.

One nullable ``timestamptz`` column on ``approval_request``:

* ``resumed_at`` -- UTC time the winning resumer claimed the single
  execution, or NULL while unclaimed. Set exactly once by the atomic
  claim (:func:`~meho_backplane.operations.approval_queue.claim_resume`);
  never cleared (a one-way latch, so a failed dispatch is not silently
  retried into a possible double write). No index: the column is only
  ever read/written by the primary-key-scoped conditional UPDATE and off
  a row already loaded by id.

Soft-column discipline mirrors ``0036`` / ``0040`` / ``0053``: nullable,
no server default (Python-side ``None`` / claim-set), reversible. Pre-0055
rows keep NULL, meaning "never resumed" -- the same starting state a
freshly-parked request has, so the fallback re-dispatch remains
claimable for them.

Reversibility contract
----------------------

``downgrade()`` drops the column. SQLite's ALTER TABLE drop-column has
been supported since 3.35.0 (we're on 3.45+); Alembic's batch-mode
fallback isn't required.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0055"
down_revision: str | None = "0054"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add the nullable ``resumed_at`` claim column to ``approval_request``."""
    op.add_column(
        "approval_request",
        sa.Column(
            "resumed_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )


def downgrade() -> None:
    """Drop the ``resumed_at`` claim column added in :func:`upgrade`."""
    op.drop_column("approval_request", "resumed_at")
