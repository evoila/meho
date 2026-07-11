# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Add ``scheduled_trigger`` skip-state projection columns (#2327).

Revision ID: 0057
Revises: 0056
Create Date: 2026-07-11

Initiative #2364, Task #2327. The tick loop's precondition gate
(:func:`~meho_backplane.scheduler.loop._prepare_invocation`) skips a due
trigger *without advancing its state* whenever the agent definition is
missing/disabled or the agent credentials cannot be resolved. Skip-and-
retry is right for a transient miss, but a **permanent** miss (expired
scheduler Vault token, never-persisted agent secret, deleted-but-still-
referenced definition) produced an infinite silent loop whose only trace
was a WARN pair in the pod log every tick -- nothing on the trigger row,
so ``scheduler list`` showed a healthy-looking ``active`` trigger while it
skipped every fire for weeks (a real deploy lost ~360 hourly fires over 15
days before anyone noticed).

This migration adds three columns that project the cumulative skip state
onto the row so the read surfaces agree with the pod-log WARNs:

* ``last_skip_reason`` -- ``text`` nullable. The stable machine tag of the
  most recent skip cause (``definition_missing`` / ``definition_disabled``
  / ``credentials_unresolved``; the park paths also stamp
  ``invalid_cron_expr`` / ``unknown_kind``). NULL until the first skip;
  the loop clears it back to NULL on the next successful fire.
* ``last_skipped_at`` -- ``timestamptz`` nullable. UTC time of the most
  recent skip; NULL until the first skip, cleared on the next fire.
* ``skip_count`` -- ``integer`` NOT NULL, server default ``0``. Consecutive
  skips since the last successful fire (reset to 0 on the next fire). The
  loop parks the trigger (``status='paused'``) once this reaches
  ``_PARK_AFTER_CONSECUTIVE_SKIPS`` so a permanently-unresolvable trigger
  stops silently re-tripping every tick.

Soft-column discipline mirrors ``0043`` / ``0053`` / ``0055``: the two
nullable columns take no server default (Python-side ``None`` /
loop-set); ``skip_count`` takes a ``0`` server default so pre-#2327 rows
backfill to "never skipped" without a data-migration pass. All three are
purely additive, so ``helm rollback`` to the pre-0057 image (which never
mentions the columns) is safe -- the forward-compat regression test
covers this shape.

Reversibility contract
----------------------

``downgrade()`` drops the three columns in reverse add order. SQLite's
ALTER TABLE drop-column has been supported since 3.35.0 (we're on 3.45+),
so Alembic's batch-mode fallback is not required.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0057"
down_revision: str | None = "0056"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add the three skip-state columns to ``scheduled_trigger``."""
    op.add_column(
        "scheduled_trigger",
        sa.Column("last_skip_reason", sa.Text(), nullable=True),
    )
    op.add_column(
        "scheduled_trigger",
        sa.Column("last_skipped_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "scheduled_trigger",
        sa.Column(
            "skip_count",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    )


def downgrade() -> None:
    """Drop the skip-state columns added in :func:`upgrade` (reverse order)."""
    op.drop_column("scheduled_trigger", "skip_count")
    op.drop_column("scheduled_trigger", "last_skipped_at")
    op.drop_column("scheduled_trigger", "last_skip_reason")
