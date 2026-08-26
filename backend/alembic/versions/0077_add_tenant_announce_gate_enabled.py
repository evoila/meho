# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Add ``tenant.announce_gate_enabled`` for the opt-in announce gate.

Revision ID: 0077
Revises: 0076
Create Date: 2026-08-26

Task #3133 under Initiative #3128. The dispatch-time announce gate is
off by default and opt-in per tenant; its enablement is a structured
per-tenant policy field -- a first-class Boolean column on ``tenant``,
NOT a free-form ``tenant_conventions`` row. This migration ships the
storage half: the flag column. The dispatch path that *consumes* it
(:mod:`meho_backplane.broadcast.announce_gate`) ships in the same task's
code change; this column stores + exposes the flag.

What this migration adds
------------------------

* ``tenant.announce_gate_enabled boolean NOT NULL DEFAULT false`` --
  whether the dispatcher enforces the announce gate for this tenant.
  ``False`` (the default) keeps dispatch byte-identical to pre-#3133 for
  every existing tenant; ``True`` is the audited per-tenant opt-in. No
  index -- the flag is read per-dispatch through a cache-aware resolver
  (a one-row SELECT on a cache miss, keyed on the ``tenant`` primary
  key), never a filter predicate.

Why ``server_default=false`` (not the bare ORM ``default``)
-----------------------------------------------------------

``announce_gate_enabled`` is an ``ADD COLUMN`` on a populated table: a
``NOT NULL`` add-column with no default is rejected by both PostgreSQL
and SQLite, so the migration supplies a ``server_default`` to backfill
every existing row to the OFF state in one DDL statement. The ORM column
declares the same ``server_default=sa.false()`` so ``MetaData``-driven
schema creation stays in sync with the migrated schema. Mirrors the
``targets.verify_tls`` precedent (migration ``0044``).

Dialect-portability decisions
-----------------------------

* :class:`sqlalchemy.Boolean` on both dialects -- PG renders
  ``BOOLEAN``, SQLite renders it as an ``INTEGER`` with a ``0``/``1``
  CHECK. ``server_default=sa.false()`` renders ``false`` on PG and ``0``
  on SQLite (SQLAlchemy maps the literal per dialect), so the backfill
  is portable.
* ``nullable=False`` -- explicit on both dialects; the default is
  enforced at the DB layer, not only in the ORM.

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
revision: str = "0077"
down_revision: str | None = "0076"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add the ``announce_gate_enabled`` column to ``tenant`` (default OFF)."""
    op.add_column(
        "tenant",
        sa.Column(
            "announce_gate_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )


def downgrade() -> None:
    """Drop the ``announce_gate_enabled`` column added in :func:`upgrade`."""
    op.drop_column("tenant", "announce_gate_enabled")
