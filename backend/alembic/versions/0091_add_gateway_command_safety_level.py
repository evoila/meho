# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Add ``gateway_command.safety_level`` for write-capable-runner revocation (#3192).

Revision ID: 0091
Revises: 0087
Create Date: 2026-08-31

Initiative #2901 (satellite write path), Task #3192 — revocation hardening
for write-capable runners (the Stage-3 gate of decision
``docs/decisions/satellite-write-path.md``, recommendation 3 / threat T8).

One column on ``gateway_command``:

* ``safety_level`` — Text NOT NULL. The op's ``safety_level`` denormalised
  onto the command row at mint, so the DB-side delivery path
  (``claim_next_command``) can tier-scope the revocation refusal to the
  ``remote-write`` tier without re-resolving the descriptor. A revoked
  runner's claim narrows to ``safety_level NOT IN`` the remote-write set,
  so an already-minted, not-yet-expired ``remote-write`` capability is never
  delivered post-revocation, while a ``safe`` (read) capability is
  unaffected — the read path's coarse kill switch stays unchanged. Same
  "bind at mint, check at delivery without a re-lookup" discipline as the
  ``params_hash`` / ``expires_at`` capability columns (``0061``).

Serialized order (house Alembic rule): this extends the current head
``0087`` (#3216's ``tenant.flight_recorder_agent_readable``) at author time.
The **number 0091** is chosen to sit clear of the concurrent racers under
this same #2901 initiative: sibling #3230 already claims ``0088``
(``reap_probe_epoch_impl_id_registrations``), and ``0089`` / ``0090`` are
reserved for #3189 / #3191; 0091 is the next free number after those, so
there is no duplicate-revision-id clash. Only the ``down_revision`` pointer
to the real current head is load-bearing, never the number — if a sibling
migration lands on main first (e.g. #3230's ``0088`` chaining ``0087``),
re-point this ``down_revision`` to the new head before merge so ``0087``
keeps a single child (no branched heads). The orchestrator re-points at
merge if the race lands differently.

NOT NULL ADD COLUMN on an empty clean-slate table
-------------------------------------------------

``gateway_command`` is a clean-slate table (``0059``), so it is empty in
every environment. ``safety_level`` is added NOT NULL with a **constant**
``server_default`` (``'safe'`` — house pattern ``0044`` / ``0057`` / ``0061``):
SQLite's ``ALTER TABLE ADD COLUMN`` forbids an expression default on a NOT
NULL column, so the default is a literal constant. ``'safe'`` is the
read-tier fail-safe — a legacy or direct-enqueue row that omits the level is
classified as a read, never a remote-write, so the revocation filter never
mis-refuses a read — but no real minted row sees it: the mint /
``enqueue_command`` path stamps the descriptor's true ``safety_level``.

Reversibility contract
----------------------

``upgrade()`` adds the column; ``downgrade()`` drops it. Purely additive on
the way up (no destructive DDL), so the migration-compat CI guard passes.
SQLite drop-column is supported since 3.35.0 (we're on 3.45+), so Alembic
batch mode is not required.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0091"
down_revision: str | None = "0087"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add the ``safety_level`` column to ``gateway_command``."""
    op.add_column(
        "gateway_command",
        sa.Column(
            "safety_level",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'safe'"),
        ),
    )


def downgrade() -> None:
    """Drop the ``safety_level`` column."""
    op.drop_column("gateway_command", "safety_level")
