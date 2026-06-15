# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Add ``targets.verify_tls`` column for per-target TLS-verification opt-out.

Revision ID: 0044
Revises: 0043
Create Date: 2026-06-15

T1 (#1780) under Initiative #1774, Goal #214. Connector dispatch can
only verify the target endpoint's certificate against the global
``SSL_CERT_FILE`` bundle today, so a target presenting a self-signed or
internal-CA cert (a nested lab, a freshly-deployed appliance before cert
replacement, a Fleet-managed component with its own locker CA) dies on
``CERTIFICATE_VERIFY_FAILED``. This migration ships the *storage* half
of a per-target opt-out: a ``verify_tls`` flag, default-secure, mirroring
the proven ``vpn_required`` first-class column (``db/models.py``) -- not
an ``extras`` key, because connection-affecting config is the
first-class-column precedent (``version`` #1215, ``auth_model`` CHECK).
The dispatch path that *consumes* the flag (passing
``verify=<insecure SSLContext>`` to the pooled httpx client when the flag
is ``False``) lands in T2 (#1781); this column only stores + exposes +
audits the flag.

What this migration adds
------------------------

* ``targets.verify_tls boolean NOT NULL DEFAULT true`` -- whether
  connector dispatch verifies the target's TLS certificate chain.
  ``True`` (the secure default) keeps the dispatch client byte-identical
  to today; ``False`` is the audited per-target opt-out. No index -- the
  column is read per-request from the in-memory
  :class:`~meho_backplane.db.models.Target` row the resolver already
  loaded; it is never a filter predicate.

Why ``server_default=true`` (not the bare ORM ``default``)
----------------------------------------------------------

``vpn_required`` (migration 0004) carries no server default because it
was created with the ``targets`` table, so there were no pre-existing
rows to backfill. ``verify_tls`` is an ``ADD COLUMN`` on a populated
table: a ``NOT NULL`` add-column with no default is rejected by both
PostgreSQL and SQLite, so the migration must supply a ``server_default``
to backfill every existing row to the secure ``true`` state in one DDL
statement. The ORM column declares the same ``server_default=sa.true()``
so ``MetaData``-driven schema creation stays in sync with the migrated
schema. The acceptance contract ("existing rows read back
``verify_tls=true`` after ``alembic upgrade head``") is exactly this
backfill.

Why additive (not amend ``0004``)
---------------------------------

The reversible-additive discipline established by ``0006``+ applies:
new requirement = new migration head, never rewrite historical
migrations. Same rationale as ``0032`` (``targets.version`` / #1215).

Dialect-portability decisions
-----------------------------

* ``verify_tls`` -- :class:`sqlalchemy.Boolean` on both dialects. PG
  renders ``BOOLEAN``; SQLite renders the boolean as an ``INTEGER`` with
  a ``0``/``1`` CHECK. ``server_default=sa.true()`` renders ``true`` on
  PG and ``1`` on SQLite (SQLAlchemy maps the literal per dialect), so
  the backfill is portable.
* ``nullable=False`` -- explicit on both dialects; the secure default is
  enforced at the DB layer, not only in the Pydantic schema.

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
revision: str = "0044"
down_revision: str | None = "0043"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add the ``verify_tls`` column to ``targets`` (default-secure)."""
    op.add_column(
        "targets",
        sa.Column(
            "verify_tls",
            sa.Boolean(),
            nullable=False,
            server_default=sa.true(),
        ),
    )


def downgrade() -> None:
    """Drop the ``verify_tls`` column added in :func:`upgrade`."""
    op.drop_column("targets", "verify_tls")
