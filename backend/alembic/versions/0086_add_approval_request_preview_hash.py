# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Add ``approval_request.preview_hash`` for the destructive-tier preview binding.

Revision ID: 0086
Revises: 0085
Create Date: 2026-08-30

Task #3197 under Initiative #3183 (governed deletes), decision
``docs/decisions/governed-delete-operations.md`` requirement 2. The
``destructive`` safety tier (shipped #3196, migration 0084) refuses to
park a delete-shaped op unless a ``preview_operation`` of the identical
``(connector_id, op_id, target, params)`` was executed and *its result
hash* is presented with the request — so the approver sees precisely
what will be destroyed. This migration ships the storage half: the
column that records that bound hash on the parked row. The dispatch /
approve code that produces + re-verifies it ships in the same task.

What this migration adds
------------------------

* ``approval_request.preview_hash text NULL`` -- the SHA-256 hex over the
  canonicalised resolved preview envelope, recorded at park time for a
  ``destructive``-tier request and re-verified at approve time. Distinct
  from the existing ``params_hash`` (which hashes the request *params*,
  not the preview *result*).

Why nullable (no ``server_default``)
------------------------------------

The binding is a **destructive-tier requirement only**: a ``safe`` /
``caution`` / ``dangerous`` request carries no preview hash, so the
column is ``NULL`` for every non-destructive row and for every pre-0086
row. A nullable ``ADD COLUMN`` needs no backfill default — existing rows
take ``NULL`` (the correct "no binding" state) with no table rewrite.
Mirrors the ``approval_request.params`` add (migration ``0036``), which
is nullable for the same "pre-existing rows have no value" reason.

Migration-chain note (single linear head)
------------------------------------------

Numbered ``0086`` with ``down_revision = "0085"`` — the head on
``origin/main`` (``0085_create_dispatch_trace_store``, the flight-recorder
trace store that landed while this task was in flight). The chain is a
single linear head ``0084 -> 0085 -> 0086``, which the ``Python (database
migrations)`` CI gate requires. This column and the trace-store table are
independent; the linear ordering is purely the head discipline.

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
revision: str = "0086"
down_revision: str | None = "0085"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add the nullable ``preview_hash`` column to ``approval_request``."""
    op.add_column(
        "approval_request",
        sa.Column("preview_hash", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    """Drop the ``preview_hash`` column added in :func:`upgrade`."""
    op.drop_column("approval_request", "preview_hash")
