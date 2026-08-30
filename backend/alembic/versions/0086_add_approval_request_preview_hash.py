# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Add ``approval_request.preview_hash`` for the destructive-tier preview binding.

Revision ID: 0086
Revises: 0084
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

Numbered ``0086`` with ``down_revision = "0084"`` — the head on
``origin/main`` at branch time. A concurrent PR (#3219, flight-recorder
trace store) holds ``0085``; this revision deliberately skips to ``0086``
to avoid a duplicate-revision-id collision with it. If ``0085`` lands
first, the orchestrator re-points this ``down_revision`` to the
then-current head at merge so the chain stays a single linear head (the
``Python (database migrations)`` CI gate). No data dependency on the
skipped number — Alembic orders by the ``revision``/``down_revision``
graph, not by filename.

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
down_revision: str | None = "0084"
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
