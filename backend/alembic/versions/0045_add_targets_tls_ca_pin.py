# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Add ``targets.tls_ca_pin`` column for the per-target CA-trust pin.

Revision ID: 0045
Revises: 0044
Create Date: 2026-06-15

T5 (#1784) under Initiative #1774, Goal #214. ``verify_tls=false`` (T1/T2,
``0044`` + #1781) unblocks self-signed appliances but drops **all**
verification -- no chain, no hostname -- exposing the target's
Vault-resolved credential to a man-in-the-middle. The secure answer for a
self-signed / internal-CA endpoint is to **pin its CA**: trust exactly
that CA while keeping ``CERT_REQUIRED`` + hostname checking on (the
govc-thumbprint pattern). This migration ships the *storage* half: a
nullable ``tls_ca_pin`` column carrying a PEM string. The dispatch path
that *consumes* it -- building
``ssl.create_default_context(); ctx.load_verify_locations(cadata=<pem>)``
and threading it into the pooled httpx client -- lives in
:mod:`meho_backplane.connectors.adapters.http`.

What this migration adds
------------------------

* ``targets.tls_ca_pin text NULL`` -- a per-target CA-trust pin (PEM).
  ``NULL`` (the default) means "no pin -- verify against the global
  ``SSL_CERT_FILE`` bundle only", so existing rows keep today's behaviour
  byte-identical. No index -- the column is read per-request from the
  in-memory :class:`~meho_backplane.db.models.Target` row the resolver
  already loaded; it is never a filter predicate.

Why nullable (no ``server_default``, unlike ``0044``'s ``verify_tls``)
----------------------------------------------------------------------

``verify_tls`` (``0044``) is ``NOT NULL`` and therefore needed a
``server_default`` to backfill existing rows on the ``ADD COLUMN``.
``tls_ca_pin`` is **nullable** -- "no pin" is a first-class state, not a
default value -- so a plain nullable ``ADD COLUMN`` is safe on a populated
table: PostgreSQL and SQLite both add it as ``NULL`` for every existing
row with no rewrite. Mirrors the ``secret_ref`` / ``fqdn`` / ``version``
nullable-Text columns.

Why additive (not amend ``0044``)
---------------------------------

The reversible-additive discipline established by ``0006``+ applies: new
requirement = new migration head, never rewrite historical migrations.
Same rationale as ``0044`` (``verify_tls``) and ``0032``
(``targets.version`` / #1215).

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
revision: str = "0045"
down_revision: str | None = "0044"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add the nullable ``tls_ca_pin`` column to ``targets``."""
    op.add_column(
        "targets",
        sa.Column(
            "tls_ca_pin",
            sa.Text(),
            nullable=True,
        ),
    )


def downgrade() -> None:
    """Drop the ``tls_ca_pin`` column added in :func:`upgrade`."""
    op.drop_column("targets", "tls_ca_pin")
