# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Add the ``gateway_command`` single-use capability binding columns (#2500).

Revision ID: 0061
Revises: 0060
Create Date: 2026-07-15

Initiative #2415 (Remote execution gateway), Task #2500 — the authorization
keystone. #2498 shipped ``gateway_command`` as a strictly-transport queue
row (migration ``0059``); this migration layers the capability binding on
top so a delivered command is bound to ``(runner, op, target, args-hash,
expiry)`` and executed at most once, with central replay refusal.

Four columns on ``gateway_command``:

* ``params_hash`` — Text NOT NULL. ``compute_params_hash(params)`` stamped
  at mint. The delivery path (``claim_next_command``) re-hashes the stored
  ``params`` against it and refuses delivery on mismatch — the post-mint
  substitution defence ``approve_request`` runs on ``approval_request``.

* ``expires_at`` — ``timestamptz`` NOT NULL. Bounded at mint against a
  module-constant default TTL (caller may only shorten). The claim predicate
  requires ``expires_at > now``, so an expired capability is never
  delivered; a command lost after delivery (runner crash) is not
  redelivered — it expires, and re-minting is the operator's explicit choice.

* ``consumed_at`` — ``timestamptz`` nullable one-way latch. Won by a single
  conditional ``UPDATE ... SET consumed_at = now WHERE consumed_at IS NULL
  AND status = 'delivered'`` (``consume_command``, moulded on
  ``claim_resume`` #2293): a result is accepted at most once and a replay is
  centrally refused. Never cleared. A consumed row is excluded from claiming.

* ``mint_audit_id`` — UUID nullable **soft** FK to ``audit_log.id`` (no DB
  FK, same discipline as ``audit_log.parent_audit_id`` / ``target_id``).
  The id of the synchronous ``gateway.command.mint`` audit row; the accepted
  result's audit row stamps ``parent_audit_id = mint_audit_id`` so a remote
  execution forms one audit subtree.

Serialized order (initiative set-consistency review): this is the **fourth**
migration in the chain (#2502 → #2498 → #2499 → #2500 → #2501). It extends
the then-current single head ``0060`` (#2499's ``runner_assignments`` /
``runner_check_results``). Per the house Alembic rule, if a sibling
migration lands on main first, renumber-before-merge — only
``down_revision`` extending the current head is load-bearing, never the
number.

NOT NULL ADD COLUMN on an empty clean-slate table
-------------------------------------------------

``gateway_command`` is a clean-slate table (``0059``, same in-flight
initiative), so it is empty in every environment. ``params_hash`` and
``expires_at`` are added NOT NULL with a **constant** ``server_default``
(house pattern ``0044`` / ``0057``): SQLite's ``ALTER TABLE ADD COLUMN``
forbids a ``NULL``, ``CURRENT_TIMESTAMP``, or parenthesised-expression
default on a NOT NULL column, so the defaults are literal constants. Both
are deliberately **fail-closed** sentinels — an empty ``params_hash`` never
matches a real params hash (delivery refuses it) and the epoch ``expires_at``
is already past (the claim predicate excludes it) — but no real row ever
sees them: the mint / ``enqueue_command`` path stamps the true values.

Reversibility contract
----------------------

``upgrade()`` adds the four columns; ``downgrade()`` drops them in inverse
order. Purely additive on the way up (no destructive DDL), so the
migration-compat CI guard passes. SQLite drop-column is supported since
3.35.0 (we're on 3.45+), so Alembic batch mode is not required.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0061"
down_revision: str | None = "0060"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: Fail-closed epoch sentinel for the NOT NULL ``expires_at`` ADD COLUMN on
#: the empty table (a constant literal — SQLite forbids an expression
#: default here). Every minted row overrides it with the bounded TTL.
_EXPIRES_AT_SENTINEL = "'1970-01-01 00:00:00+00:00'"


def upgrade() -> None:
    """Add the four capability-binding columns to ``gateway_command``."""
    op.add_column(
        "gateway_command",
        sa.Column(
            "params_hash",
            sa.Text(),
            nullable=False,
            server_default=sa.text("''"),
        ),
    )
    op.add_column(
        "gateway_command",
        sa.Column(
            "expires_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text(_EXPIRES_AT_SENTINEL),
        ),
    )
    op.add_column(
        "gateway_command",
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "gateway_command",
        sa.Column("mint_audit_id", sa.Uuid(), nullable=True),
    )


def downgrade() -> None:
    """Drop the four capability-binding columns in inverse order."""
    op.drop_column("gateway_command", "mint_audit_id")
    op.drop_column("gateway_command", "consumed_at")
    op.drop_column("gateway_command", "expires_at")
    op.drop_column("gateway_command", "params_hash")
