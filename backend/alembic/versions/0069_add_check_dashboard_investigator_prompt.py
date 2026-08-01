# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Add ``check_dashboards.investigator_prompt`` (#2721).

Revision ID: 0069
Revises: 0068
Create Date: 2026-08-01

Task #2721 under Initiative #2716 (parent goal #221). The diagnose-only
investigator (#2507) builds its briefing entirely from the transition
snapshot, so an operator holding domain knowledge about a Dashboard ("the
storage array behind these datastores flaps on its nightly scrub -- check
the controller before the hosts") has no way to convey it. This column is
that knowledge, read by ``checks.investigate._build_briefing`` and
**appended** after the server-built snapshot in a clearly delimited
section -- never replacing it.

* ``investigator_prompt`` -- ``text`` nullable. ``NULL`` (the backfilled
  state for every pre-#2721 Dashboard) means the briefing is built exactly
  as it is today, so the migration is behaviour-preserving on its own.

Why no CHECK bounding the length
--------------------------------

The ~4 KiB bound lives at the wire boundary
(``dashboard_schemas._INVESTIGATOR_PROMPT_MAX_LENGTH``), not in a DB CHECK.
Three reasons: the column has exactly one writer (the create path -- a
Dashboard is set-at-create-only, there is no PUT), so the boundary schema is
not one guard among many but *the* guard; a boundary rejection is a
structured 422 naming the field and its limit, where a CHECK violation would
surface as an opaque 500; and mould parity -- ``description`` (2048) and
``name`` (128) on this same table are likewise bounded at the schema with a
plain ``Text`` column, and ``0068``'s CHECK constrained a closed *vocabulary*
(``notify_min_state``), which is a different kind of invariant.

Why ``batch_alter_table`` for a single nullable add
---------------------------------------------------

``add_column`` is the one column-level ALTER SQLite supports natively, so
this particular migration would work without the batch block. It is used
anyway for consistency with ``0068`` on the same table: Alembic emits a
plain ``ALTER TABLE ADD COLUMN`` on both backends here (the move-and-copy
workflow is only triggered by operations SQLite cannot do), so the block
costs nothing and keeps the two adjacent migrations reading the same way.

Reversibility contract
----------------------

``downgrade()`` drops the column. Purely additive forward, so a ``helm
rollback`` to a pre-0069 image (which never mentions the column) is safe;
a downgrade discards operator-authored prompt text, which is the expected
cost of removing a config column and is why the column is create-only
rather than something a rollback could silently half-apply.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0069"
down_revision: str | None = "0068"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add the nullable ``investigator_prompt`` column."""
    with op.batch_alter_table("check_dashboards") as batch_op:
        batch_op.add_column(sa.Column("investigator_prompt", sa.Text(), nullable=True))


def downgrade() -> None:
    """Drop the column added in :func:`upgrade`."""
    with op.batch_alter_table("check_dashboards") as batch_op:
        batch_op.drop_column("investigator_prompt")
