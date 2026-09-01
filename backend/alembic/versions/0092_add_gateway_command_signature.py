# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Add ``gateway_command.signature`` for the signed remote-write capability.

Revision ID: 0092
Revises: 0091
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
column is therefore ``NULL`` for every ``safe`` row and every pre-0092 row. A
nullable ``ADD COLUMN`` needs no backfill default -- existing rows take
``NULL`` (the correct "unsigned" state) with no table rewrite. Mirrors the
``approval_request.preview_hash`` add (migration ``0086``), nullable for the
same "the property is tier-specific, absent rows have no value" reason.

Migration-chain note (single linear head)
------------------------------------------

Numbered ``0092`` with ``down_revision = "0091"`` -- the head on
``origin/main`` after two sibling migrations landed while this task was in
flight: ``0088_reap_probe_epoch_impl_id_registrations`` (#3061) and
``0091_add_gateway_command_safety_level`` (#3192 revocation). The chain is a
single linear head ``0088 -> 0091 -> 0092``, which the ``Python (database
migrations)`` CI gate requires (``0089`` / ``0090`` were never used). #3191
(credential brokering) shipped no migration.

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
revision: str = "0092"
down_revision: str | None = "0091"
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
