# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Add ``targets.version`` column for operator-asserted product version (G0.15-T6).

Revision ID: 0032
Revises: 0031
Create Date: 2026-05-27

G0.15-T6 (#1215) ships operator-editable ``version`` on
:class:`~meho_backplane.targets.schemas.TargetCreate` and
:class:`~meho_backplane.targets.schemas.TargetUpdate` so a fresh target
can carry an explicit product version (e.g. ``"9.0"`` for a vCenter)
*before* the first probe -- breaking the chicken-and-egg the v0.7.0
dogfood surfaced (RDC #753, signal 6): every typed connector except
K8s required ``fingerprint.version`` to resolve, but the probe needed
the resolver to find a connector first. The K8s exception was the
sibling ``("k8s", "", "")`` wildcard registration -- that pattern is
fanned out across every typed connector in the same PR.

Schema
------

* ``version`` -- ``TEXT`` NULL on both PostgreSQL and SQLite. ``NULL``
  means "no operator-asserted version; resolver falls back to
  ``fingerprint.version`` (if probed) or the wildcard registration
  (if neither set)". A non-NULL value is the operator-asserted product
  version (free-form string; the resolver parses it via
  :class:`packaging.version.Version` so semver-shaped values like
  ``"9.0"`` or ``"9.0.2"`` are honoured, and unparseable values fall
  back to the wildcard).

* **No index.** The column is read at resolver time per-request from
  the in-memory :class:`~meho_backplane.db.models.Target` row already
  loaded by :func:`~meho_backplane.targets.resolver.resolve_target`;
  it is not a filter predicate. The existing ``targets_tenant_name_idx``
  drives the row lookup.

Why additive (not amend ``0004``)
---------------------------------

The reversible-additive discipline established by ``0006``+ applies:
new requirement = new migration head, never rewrite historical
migrations. Same rationale as ``0009`` (``targets.fingerprint`` /
``preferred_impl_id``) and ``0029`` (``targets.deleted_at``).

Dialect-portability decisions
-----------------------------

* ``version`` -- :class:`sqlalchemy.Text` on both dialects. PG renders
  ``TEXT``; SQLite renders ``TEXT`` (no length cap on either). The
  Pydantic ``TargetCreate.version`` / ``TargetUpdate.version`` fields
  carry a ``max_length=100`` guard so unbounded strings cannot reach
  the DB.
* ``nullable=True`` -- explicit on both dialects so the column default
  of ``NULL`` is the "no operator hint" state. No server default
  (every existing row is ``NULL`` per ``ADD COLUMN`` semantics on both
  PG and SQLite ≥ 3.35).

Reversibility contract
----------------------

``downgrade()`` drops the column. SQLite's ALTER TABLE drop-column
has been supported since 3.35.0 (we're on 3.45+); Alembic's batch-mode
fallback isn't required.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0032"
down_revision: str | None = "0031"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add ``version`` column to ``targets``."""
    op.add_column(
        "targets",
        sa.Column("version", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    """Drop the ``version`` column added in :func:`upgrade`."""
    op.drop_column("targets", "version")
