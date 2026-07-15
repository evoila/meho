# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Add the gateway dead-man's-switch liveness columns (#2501).

Revision ID: 0061
Revises: 0060
Create Date: 2026-07-15

Initiative #2415 (Remote execution gateway), Task #2501 — the runner
dead-man's switch + mandatory heartbeat. Two additive columns on the
gateway tables the earlier tasks landed:

* ``runner_principal.last_seen_at`` (``timestamptz``, ``NOT NULL``,
  server-default ``now()``) — the runner-liveness stamp. Every
  authenticated runner-plane request refreshes it on the central clock
  (the single choke-point is
  :func:`meho_backplane.auth.runner_guard.assert_runner_scope`), so a
  runner's ability to reach central is observable without a dedicated
  heartbeat endpoint. The server default initialises both the
  pre-existing rows this migration back-fills and any future non-ORM
  insert to "seen now" rather than epoch, so an already-registered
  runner is treated as alive the instant the column lands, not
  immediately stale.

* ``runner_assignments.stale_at`` (``timestamptz``, ``NULL``) — the flip
  marker. ``NULL`` = fresh; non-``NULL`` = the moment the central
  dead-man sweeper declared this runner's workloads unknown because its
  ``last_seen_at`` fell behind ``N x GATEWAY_LONGPOLL_MAX_WAIT_SECONDS``.
  An accepted result ingestion clears it; the sweeper only ever flips it.
  Shape mirrors ``web_session.revoked_at`` (NULL-marker timestamp).

A b-tree index on ``runner_principal.last_seen_at`` backs the sweeper's
``last_seen_at < cutoff`` predicate (index-rationale mould: the
``web_session_expires_at_idx`` sweep index).

This migration is the **fifth** in the initiative's serialized chain
(#2502 -> #2498 -> #2499 -> #2500 -> #2501); it extends the then-current
single head ``0060`` (``runner_assignments`` / ``runner_check_results``).
Per the house Alembic rule, if a sibling migration lands on main first,
renumber-before-merge — never fork the linear chain.

Dialect portability
-------------------

PostgreSQL keeps a live ``now()`` server default on ``last_seen_at``.
SQLite (the dev / test driver) forbids ``CURRENT_TIMESTAMP`` and every
other expression as an ``ALTER TABLE ADD COLUMN`` default — only a
constant literal is permitted — so the SQLite branch pins a
migration-time literal. Freshly-registered runners get their real
timestamp from the ORM ``default`` regardless of dialect; the literal
only ever back-fills rows that predate this column.

Reversibility contract
----------------------

``upgrade()`` adds the two columns + the index; ``downgrade()`` drops
them in inverse order. Purely additive on the way up.
"""

from collections.abc import Sequence
from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0062"
down_revision: str | None = "0061"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add ``last_seen_at`` + its index and ``stale_at``."""
    bind = op.get_bind()
    is_postgres = bind.dialect.name == "postgresql"

    if is_postgres:
        last_seen_default: str | sa.sql.elements.TextClause = sa.text("now()")
    else:
        # SQLite rejects CURRENT_TIMESTAMP / expressions as an ADD COLUMN
        # default (only constant literals are allowed). Pin a migration-time
        # literal so the NOT NULL add succeeds and any pre-existing row
        # initialises to ~now (treated as freshly-seen, not epoch-stale).
        #
        # Pass the literal as a plain ``str`` server_default -- SQLAlchemy
        # renders it as a properly-quoted SQL literal, giving DDL identical
        # to ``sa.text(f"'{value}'")`` -- rather than wrapping it in an
        # interpolated ``sa.text(...)``. A non-literal ``text()`` argument
        # trips Semgrep's ``avoid-sqlalchemy-text`` rule, and CI's
        # registry-pack Semgrep does not honour an inline ``# nosemgrep``
        # for it, so the repo convention (``events/outbox.py``,
        # ``retrieval/retriever.py``, ``topology/query.py``) is to keep the
        # value off ``text()`` entirely.
        last_seen_default = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S.%f")

    op.add_column(
        "runner_principal",
        sa.Column(
            "last_seen_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=last_seen_default,
        ),
    )
    op.create_index(
        "runner_principal_last_seen_at_idx",
        "runner_principal",
        ["last_seen_at"],
        postgresql_using="btree",
    )
    op.add_column(
        "runner_assignments",
        sa.Column("stale_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    """Drop ``stale_at``, then the index and ``last_seen_at`` (reverse order)."""
    op.drop_column("runner_assignments", "stale_at")
    op.drop_index(
        "runner_principal_last_seen_at_idx",
        table_name="runner_principal",
    )
    op.drop_column("runner_principal", "last_seen_at")
