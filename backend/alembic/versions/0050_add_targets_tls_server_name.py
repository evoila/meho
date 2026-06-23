# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Add ``targets.tls_server_name`` to decouple SNI/cert-verify from ``Host``.

Revision ID: 0050
Revises: 0049
Create Date: 2026-06-21

Task #2002 under Initiative #1998, Goal #221. The per-target TLS columns
shipped so far (``verify_tls`` ``0044`` / #1780, ``tls_ca_pin`` ``0045`` /
#1784) let an operator reach a self-signed / internal-CA appliance, but
the dispatch client still derives **three** identities from the single
``host`` value: the TCP connect address, the wire ``Host:`` header, and
the TLS SNI + certificate hostname-verification name
(``httpcore``: ``server_hostname = request.extensions["sni_hostname"] or
origin.host``). An appliance that pins its certificate to an FQDN-CN but
only accepts ``Host: <IP>`` (the FQDN is NXDOMAIN off-cluster) therefore
cannot be reached with ``verify_tls=true`` -- the operator is forced down
to the insecure ``verify_tls=false`` downgrade.

This migration ships the *storage* half of the decoupling: a nullable
``tls_server_name`` column carrying the SNI + cert-CN/SAN verification
name. The dispatch path that *consumes* it -- threading
``extensions={"sni_hostname": <name>}`` into the pooled httpx client's
requests while keeping ``base_url=https://<host>`` (connect + ``Host`` =
IP) -- lives in :mod:`meho_backplane.connectors.adapters.http`.

What this migration adds
------------------------

* ``targets.tls_server_name text NULL`` -- the per-target TLS SNI /
  cert-verification hostname. ``NULL`` (the default) means "derive the
  SNI / verify name from ``host`` as today", so existing rows keep their
  behaviour byte-identical. No index -- the column is read per-request
  from the in-memory :class:`~meho_backplane.db.models.Target` row the
  resolver already loaded; it is never a filter predicate.

Why nullable (no ``server_default``, like ``0045``'s ``tls_ca_pin``)
--------------------------------------------------------------------

"No override -- use ``host``" is a first-class state, not a default
value, so a plain nullable ``ADD COLUMN`` is safe on a populated table:
PostgreSQL and SQLite both add it as ``NULL`` for every existing row
with no rewrite. Mirrors the ``tls_ca_pin`` / ``secret_ref`` / ``fqdn``
nullable-Text columns. (Contrast ``0044``'s ``verify_tls``, which is
``NOT NULL`` and therefore needed a ``server_default`` to backfill.)

Why no mutual-exclusion (unlike ``tls_ca_pin`` + ``verify_tls=false``)
----------------------------------------------------------------------

``tls_server_name`` is orthogonal to both other TLS columns: it only
moves the SNI / verification *name*, not the trust material or the
verify on/off switch. It composes cleanly with ``verify_tls=true`` (the
intended use: verify against the cert-CN while ``Host`` stays the IP)
and with ``tls_ca_pin`` (verify the pinned CA's chain against the
override name). There is no contradictory combination to reject, so no
model validator is added.

Why additive (not amend ``0044``/``0045``)
------------------------------------------

The reversible-additive discipline established by ``0006``+ applies: new
requirement = new migration head, never rewrite historical migrations.
Same rationale as ``0044`` (``verify_tls``) and ``0045`` (``tls_ca_pin``).

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
revision: str = "0050"
down_revision: str | None = "0049"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add the nullable ``tls_server_name`` column to ``targets``."""
    op.add_column(
        "targets",
        sa.Column(
            "tls_server_name",
            sa.Text(),
            nullable=True,
        ),
    )


def downgrade() -> None:
    """Drop the ``tls_server_name`` column added in :func:`upgrade`."""
    op.drop_column("targets", "tls_server_name")
