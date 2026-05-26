# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Add ``targets.deleted_at`` column for soft-delete (G0.14-T4).

Revision ID: 0028
Revises: 0027
Create Date: 2026-05-26

G0.14-T4 (#1145) ships ``DELETE /api/v1/targets/{name}`` so an
operator who misregisters a target (typo'd ``product``, stale
credentials referenced via ``secret_ref``, etc.) can remove the
broken row instead of leaving a permanent tombstone in the
tenant's registry. Hard-deletes would orphan ``audit_log.target_id``
soft-FK references (audit is append-only per v0.1-spec §6, so the
referenced ids must keep pointing at *something*); the
implementation therefore soft-deletes by stamping a ``deleted_at``
timestamp and excluding the row from every read path
(:func:`~meho_backplane.targets.resolver.resolve_target`, list,
probe, discover).

Schema
------

* ``deleted_at`` -- ``timestamptz`` NULL on PostgreSQL, generic
  ``DateTime`` NULL on SQLite. ``NULL`` means "live"; a non-NULL
  value is the wall-clock time of the DELETE call. Server-managed:
  only the DELETE handler writes it, and never twice (a re-DELETE
  collapses to 404 because the row no longer resolves through
  :func:`resolve_target`).

* **No index** on ``deleted_at`` in this migration. The existing
  ``targets_tenant_name_idx`` already filters by ``(tenant_id,
  name)`` first; the ``deleted_at IS NULL`` predicate runs
  in-memory against the index hits, which is the right cost
  profile while soft-deleted rows are a small minority of the
  table (the typical operator deletes a handful of typos, not
  thousands of rows). A future tightening migration can add a
  partial index ``WHERE deleted_at IS NOT NULL`` if forensic
  queries against the deleted set become routine.

Why additive (not amend ``0004``)
---------------------------------

Migration ``0004`` (``targets`` table) is merged to ``main`` and
applied in CI and dev databases. The
reversible-additive discipline established by ``0006``+ applies:
new requirement = new migration head, never rewrite historical
migrations. Same rationale as ``0009`` (``targets.fingerprint``
/ ``preferred_impl_id``).

Dialect-portability decisions
-----------------------------

* ``deleted_at`` -- :class:`sqlalchemy.DateTime` with
  ``timezone=True`` on both dialects. PG renders ``timestamptz``;
  SQLite carries the value as a string and ``aiosqlite`` round-
  trips through Python ``datetime``. Same portability pattern as
  the existing ``created_at`` / ``updated_at`` columns on the
  same table.
* ``nullable=True`` -- explicit on both dialects so the column
  default of ``NULL`` is the live-row state. No server default
  (every existing row is live; an explicit ``DEFAULT NULL`` on
  the column is the standard ``ALTER TABLE ADD COLUMN`` behaviour
  for nullable columns on both PG and SQLite ≥ 3.35).

Reversibility contract
----------------------

``downgrade()`` drops the column. SQLite's ALTER TABLE
drop-column has been supported since 3.35.0 (we're on 3.45+);
Alembic's batch-mode fallback isn't required.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0028"
down_revision: str | None = "0027"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add ``deleted_at`` column to ``targets``."""
    op.add_column(
        "targets",
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    """Drop the ``deleted_at`` column added in :func:`upgrade`."""
    op.drop_column("targets", "deleted_at")
