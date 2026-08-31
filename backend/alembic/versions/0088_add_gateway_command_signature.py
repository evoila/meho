# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Add ``gateway_command.signature`` for the signed remote-write capability.

Revision ID: 0088
Revises: 0087
Create Date: 2026-08-31

Task #3189 under Initiative #2901 (satellite write path), design
``docs/research/2901-satellite-write-path.md`` §3 mechanism 1. The
``remote-write`` (caution) tier reverses #2500 *for the write tier only*: a
capability is minted only against a committed approval and is stamped with a
**real Ed25519 signature** over its canonical serialisation, created once at
mint (authorization time) so the DB-free runner can verify integrity +
freshness + target-scope offline before executing. This migration ships the
storage half: the column that persists that signature on the durable
``gateway_command`` row so it can be delivered on the later poll and so it
stands as the non-repudiation anchor the store-and-forward effect audit
references. The signing / edge-verification code ships in the same task.

What this migration adds
------------------------

* ``gateway_command.signature text NULL`` -- base64 Ed25519 signature over the
  canonical work-item payload (``op_id`` + ``params_hash`` + ``target_scope``
  + ``expires_at``). Stamped by ``mint_gateway_command`` for a ``remote-write``
  mint only.

Why nullable (no ``server_default``)
------------------------------------

Signing is a **remote-write-tier property only**: a ``safe`` capability (the
read path, every row today) carries no signature -- the opaque-UUID PK + DB
consume latch remain its authorization (#2500 stands for the read tier). The
column is therefore ``NULL`` for every ``safe`` row and every pre-0088 row. A
nullable ``ADD COLUMN`` needs no backfill default -- existing rows take
``NULL`` (the correct "unsigned" state) with no table rewrite. Mirrors the
``approval_request.preview_hash`` add (migration ``0086``), nullable for the
same "the property is tier-specific, absent rows have no value" reason.

Migration-chain note (single linear head)
------------------------------------------

Numbered ``0088`` with ``down_revision = "0087"`` -- the head on
``origin/main`` (``0087_add_tenant_flight_recorder_agent_readable``). The
chain is a single linear head ``0086 -> 0087 -> 0088``, which the ``Python
(database migrations)`` CI gate requires. Concurrent sibling tasks under
#2901 (#3191 credential brokering, #3192 revocation) take the numbers *after*
this one (``0089`` onward, coordinated).

Reversibility contract
----------------------

``downgrade()`` drops the column. SQLite's ALTER TABLE drop-column has been
supported since 3.35.0 (we're on 3.45+); Alembic's batch-mode fallback isn't
required.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0088"
down_revision: str | None = "0087"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add the nullable ``signature`` column to ``gateway_command``."""
    op.add_column(
        "gateway_command",
        sa.Column("signature", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    """Drop the ``signature`` column added in :func:`upgrade`."""
    op.drop_column("gateway_command", "signature")
