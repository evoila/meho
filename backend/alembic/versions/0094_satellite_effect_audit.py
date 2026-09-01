# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Satellite effect audit + un-reported-mint alarm storage (#3193).

Revision ID: 0094
Revises: 0092
Create Date: 2026-09-01

Initiative #2901 (satellite write path), Task #3193 — mechanism 4 of the
composed write-tier gate (design ``docs/research/2901-satellite-write-path.md``
§3, decision ``docs/decisions/satellite-write-path.md``): the tamper-evident
store-and-forward effect audit and the un-reported-mint security alarm, the two
compensating controls for the consciously-recorded v0.1-spec §6 exception.

What this migration adds
------------------------

1. ``gateway_command.unreported_alarm_at timestamptz NULL`` — the one-way latch
   the un-reported-mint sweeper (``gateway/unreported_mint.py``, the
   ``deadman.py`` mould) flips when a minted ``remote-write`` capability passes
   ``expires_at`` still ``consumed_at IS NULL`` (its effect was never reported,
   threat T4). The ``unreported_alarm_at IS NULL`` predicate + a conditional
   ``UPDATE`` rowcount gate keep "exactly one security audit row per unreported
   mint" true across ticks / replicas — the same discipline
   ``runner_assignments.stale_at`` follows for the *liveness* dead-man flip;
   this is the *security* alarm, kept distinct. Nullable with no
   ``server_default``: an un-flipped capability is ``NULL`` (the correct "not
   yet alarmed" state) and a nullable ``ADD COLUMN`` needs no backfill, mirroring
   the ``signature`` add (``0092``).

2. ``runner_effect_chain`` — the per-runner head of the store-and-forward
   effect-audit hash chain. The centre verifies chain continuity *across*
   forwards against this row (``last_seq`` / ``last_hash``); a sequence gap or a
   broken link is detected here and refused / quarantined. Keyed on
   ``(tenant_id, runner_id)`` via a composite unique index; ``runner_id`` is the
   runner principal **name** (the wire identity). Additive-only: a fresh table +
   its unique index, no ALTER of an existing object beyond the nullable column
   above.

Migration-chain note (single linear head)
------------------------------------------

Numbered ``0094`` with ``down_revision = "0092"`` — the head on ``origin/main``
at author/push time (``0092_add_gateway_command_signature``, #3189). The sibling
#3190 (per-runner allowlist) runs concurrently under this same #2901 initiative
and owns ``0093`` if it needs a migration; this task deliberately skips ``0093``
so the two never collide on a revision id. If #3190 lands **no** migration (or
lands after this), the chain is ``0092 -> 0094`` linear, which the ``Python
(database migrations)`` CI gate requires; only the ``down_revision`` pointer is
load-bearing, never the number, so the orchestrator re-points at merge if the
race lands differently.

Reversibility contract
----------------------

``downgrade()`` drops the table (and its index) and the column. SQLite ALTER
TABLE drop-column is supported since 3.35.0 (we're on 3.45+); Alembic's
batch-mode fallback is not required.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0094"
down_revision: str | None = "0092"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add the un-reported-mint latch column + the effect-chain head table."""
    op.add_column(
        "gateway_command",
        sa.Column("unreported_alarm_at", sa.DateTime(timezone=True), nullable=True),
    )

    bind = op.get_bind()
    is_postgres = bind.dialect.name == "postgresql"
    op.create_table(
        "runner_effect_chain",
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("tenant_id", sa.Uuid(), sa.ForeignKey("tenant.id"), nullable=False),
        sa.Column("runner_id", sa.Text(), nullable=False),
        sa.Column("last_seq", sa.BigInteger(), nullable=False),
        sa.Column("last_hash", sa.Text(), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()") if is_postgres else None,
        ),
    )
    op.create_index(
        "runner_effect_chain_runner_uq",
        "runner_effect_chain",
        ["tenant_id", "runner_id"],
        unique=True,
    )


def downgrade() -> None:
    """Drop the effect-chain head table + the un-reported-mint latch column."""
    op.drop_index("runner_effect_chain_runner_uq", table_name="runner_effect_chain")
    op.drop_table("runner_effect_chain")
    op.drop_column("gateway_command", "unreported_alarm_at")
