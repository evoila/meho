# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Add ``check_dashboards`` email-notification columns (#2719).

Revision ID: 0068
Revises: 0067
Create Date: 2026-08-01

Task #2719 under Initiative #2716 (parent goal #221). #2507's transition
detector already compare-and-swaps ``check_dashboards.last_rollup_state``
exactly once per rollup edge, but the claim reached no operator channel.
These two columns are the per-Dashboard notification config the checks
notifier (``meho_backplane.checks.notify``) reads on that claim:

* ``notify_email`` -- ``text`` nullable. The single recipient address.
  ``NULL`` (the backfilled state for every pre-#2719 Dashboard) means
  notifications are **off** for that Dashboard, so the migration is
  behaviour-preserving on its own: no existing row starts emailing.
* ``notify_min_state`` -- ``text`` NOT NULL, server default ``'critical'``,
  CHECK ``IN ('degraded', 'critical')``. The threshold an edge must reach
  before mail is sent: an edge notifies when
  ``max(rank(previous), rank(current)) >= rank(notify_min_state)``, so the
  recovery edge back to ``ok`` sends the all-clear.

The CHECK admits only the two *actionable* states, not the full five-state
``CheckState`` vocabulary: ``ok`` as a floor would mail on every edge
(including ``ok -> ok`` noise), and ``skip`` / ``unknown`` are not
severities an operator can meaningfully set a threshold at. That is why
this vocabulary is declared independently of ``CheckState`` rather than
snapshotted from it (contrast ``0065``'s ``_ROLLUP_STATES``); the drift
guard in ``tests.test_db_dashboard`` pins it against the ORM's copy, not
against ``CheckState``.

Why ``notify_min_state`` is NOT NULL with a server default: the notifier's
threshold comparison reads a concrete state on every claimed edge, so
"unset" would need a Python-side fallback duplicating the default in a
second place. A ``'critical'`` server default backfills every pre-#2719
row with the conservative choice (page only on the worst state) and
matches the ``0057`` ``skip_count`` discipline.

Why ``batch_alter_table``: the same portable cookbook ``0025`` / ``0051``
use to add a CHECK-constrained column to an existing table. On PostgreSQL
Alembic issues plain ``ALTER TABLE ADD COLUMN`` + ``ADD CONSTRAINT`` (no
copy; PG 11+ adds a defaulted NOT NULL column without a rewrite); on
SQLite it falls back to the table-recreate path, which is the only way
SQLite adds a CHECK to an existing table. One batch block adds both
columns and the constraint together, so SQLite recreates once.

Reversibility contract
----------------------

``downgrade()`` drops the constraint and both columns inside one batch
block, in reverse add order. Purely additive forward, so a ``helm
rollback`` to a pre-0068 image (which never mentions the columns) is safe.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0068"
down_revision: str | None = "0067"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


#: Closed ``check_dashboards.notify_min_state`` vocabulary -- the two
#: actionable states an operator can set a notification floor at. Inlined
#: as a literal (never imported from :mod:`meho_backplane.db.models`)
#: because Alembic must run against any historical revision's model graph;
#: the drift guard in :mod:`tests.test_db_dashboard` asserts this tuple
#: equals the ORM's.
_NOTIFY_MIN_STATES: tuple[str, ...] = ("degraded", "critical")

#: Backfill value for every pre-#2719 row: page only on the worst state.
_NOTIFY_MIN_STATE_DEFAULT: str = "critical"


def _check_in(column: str, values: tuple[str, ...]) -> str:
    """Render a portable ``column IN ('a', 'b', ...)`` CHECK body."""
    quoted = ", ".join(f"'{v}'" for v in values)
    return f"{column} IN ({quoted})"


def upgrade() -> None:
    """Add the two notification columns + the ``notify_min_state`` CHECK."""
    with op.batch_alter_table("check_dashboards") as batch_op:
        batch_op.add_column(sa.Column("notify_email", sa.Text(), nullable=True))
        batch_op.add_column(
            sa.Column(
                "notify_min_state",
                sa.Text(),
                nullable=False,
                server_default=_NOTIFY_MIN_STATE_DEFAULT,
            )
        )
        batch_op.create_check_constraint(
            "ck_check_dashboards_notify_min_state",
            _check_in("notify_min_state", _NOTIFY_MIN_STATES),
        )


def downgrade() -> None:
    """Drop the CHECK + both columns added in :func:`upgrade` (reverse order)."""
    with op.batch_alter_table("check_dashboards") as batch_op:
        batch_op.drop_constraint("ck_check_dashboards_notify_min_state", type_="check")
        batch_op.drop_column("notify_min_state")
        batch_op.drop_column("notify_email")
