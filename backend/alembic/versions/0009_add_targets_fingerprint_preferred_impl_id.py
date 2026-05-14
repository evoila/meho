# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Add ``targets.fingerprint`` + ``targets.preferred_impl_id`` columns.

Revision ID: 0009
Revises: 0008
Create Date: 2026-05-14

This migration closes the remediation gap opened by the 2026-05-14
amendment to Initiative #224 (G0.3 Targets-as-data). The original
schema migration ``0004`` shipped before the amendment added two
columns the G0.6 resolver (#388) and the operator override surface
need:

* ``fingerprint`` — JSONB on PostgreSQL, generic JSON on SQLite.
  Cached :class:`~meho_backplane.connectors.schemas.FingerprintResult`
  from the last successful probe. ``NULL`` until first probe; populated
  by ``POST /api/v1/targets/{name}/probe`` via
  :meth:`~meho_backplane.connectors.base.Connector.fingerprint`. The
  G0.6 resolver reads this column to pick a connector implementation
  without re-probing the live target.
* ``preferred_impl_id`` — ``TEXT`` nullable. Operator override for the
  G0.6 resolver's tie-break ladder (#393): when multiple connector
  impls advertise overlapping ``(product, version)`` ranges, the
  resolver consults ``preferred_impl_id`` first and falls back to the
  ``priority`` integer only when this column is ``NULL``.

The columns are additive and nullable; no backfill is needed
(``fingerprint`` is naturally ``NULL`` until first probe;
``preferred_impl_id`` defaults to "no override").

Why additive rather than amending ``0004``
------------------------------------------

Migration ``0004`` is merged to ``main`` and has been applied in CI
and dev databases. Rewriting it would force operators to run
``alembic downgrade -1 && alembic upgrade head`` against every
environment that has already migrated. The reversible-additive
discipline established by migrations ``0006``, ``0007``, and ``0008``
applies: new requirement = new migration head, never rewrite
historical migrations.

Why ``preferred_impl_id`` is plain ``TEXT`` rather than a FK
------------------------------------------------------------

v0.2 has no ``connector_impl`` table — the registry is in-process
(``register_typed_operation()`` at G0.6 lives entirely in code).
Operators set ``preferred_impl_id`` to a string that matches a
registered impl's ``impl_id`` (advertised by the
:class:`~meho_backplane.connectors.base.Connector` subclass); the
resolver looks it up at dispatch time. This matches the soft-FK
pattern already used for ``targets.product`` (a string matched
against the in-process connector registry) and ``targets.auth_model``
(a string matched against the
:class:`~meho_backplane.connectors.schemas.AuthModel` enum).

A future tightening migration can add a real
``REFERENCES connector_impl(impl_id)`` constraint once a
``connector_impl`` table is introduced.

Dialect-portability decisions
-----------------------------

Same discipline as the prior migrations:

* ``fingerprint`` column type — :class:`JSONB` on PostgreSQL (binary,
  GIN-friendly), generic :class:`JSON` (text) on SQLite via
  :func:`sqlalchemy.JSON.with_variant`. Matches the
  :attr:`AuditLog.payload` and :attr:`Target.extras` portability
  pattern.
* ``preferred_impl_id`` column type — plain :class:`Text` on both
  dialects; no portability gymnastics needed.

Reversibility contract
----------------------

``downgrade()`` drops both columns in reverse order so the inverse is
clean on both dialects. SQLite's ALTER TABLE drop-column has been
supported since 3.35.0 (we're on 3.45+); Alembic's batch-mode
fallback isn't required.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add ``fingerprint`` + ``preferred_impl_id`` columns to ``targets``."""
    fingerprint_type = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")

    op.add_column(
        "targets",
        sa.Column("fingerprint", fingerprint_type, nullable=True),
    )
    op.add_column(
        "targets",
        sa.Column("preferred_impl_id", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    """Drop the columns added in :func:`upgrade`.

    Symmetric inverse: drop ``preferred_impl_id`` first then
    ``fingerprint`` so the inverse order mirrors the upgrade ordering.
    No indexes were created on either column, so column drop is the
    complete reversal.
    """
    op.drop_column("targets", "preferred_impl_id")
    op.drop_column("targets", "fingerprint")
