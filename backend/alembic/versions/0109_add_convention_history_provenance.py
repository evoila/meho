# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Add ``change_kind`` and ``request_id`` to ``tenant_convention_history`` (#3747).

Revision ID: 0109
Revises: 0108
Create Date: 2026-10-17

Why
---

A convention history row pointed at the REST request wrapper row in
``audit_log`` (``audit_id``, pre-allocated by the conventions service).
That wrapper row is routine request activity, not a record of the edit,
and it is going away. It was also the only place that said whether the
edit was a create, an update or a delete.

This migration lets the history row stand on its own:

* ``change_kind`` -- nullable text: ``created``, ``updated`` or
  ``deleted``. The history row now says what the edit was.
  ``ck_tenant_convention_history_change_kind`` allows only those three
  values (or ``NULL``).
* ``request_id`` -- nullable UUID. The request correlation id of the
  call that made the edit. It links the edit to the request usage event
  and, while they still exist, to the wrapper row. It is optional: a
  call without a usable request id stores ``NULL``.

Existing rows
-------------

Old rows are not rewritten. Their ``change_kind`` and ``request_id`` stay
``NULL``; nothing is guessed from ``body_before`` / ``body_after``. They
keep their ``audit_id``, which still points at the old wrapper row.

Reversibility
-------------

``downgrade()`` drops the CHECK and both columns. The values written
since the upgrade are lost; old rows are unchanged.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0109"
down_revision: str | None = "0108"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: Mirrors ``ConventionHistoryChangeKind`` in ``db/models.py``; written out
#: here so the migration does not change if the model changes.
_CHANGE_KIND_CHECK: str = "change_kind IN ('created', 'updated', 'deleted')"


def upgrade() -> None:
    """Add the two nullable columns and the ``change_kind`` CHECK."""
    with op.batch_alter_table("tenant_convention_history") as batch_op:
        batch_op.add_column(sa.Column("change_kind", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("request_id", sa.Uuid(), nullable=True))
        batch_op.create_check_constraint(
            "ck_tenant_convention_history_change_kind",
            _CHANGE_KIND_CHECK,
        )


def downgrade() -> None:
    """Drop the CHECK and both columns."""
    with op.batch_alter_table("tenant_convention_history") as batch_op:
        batch_op.drop_constraint("ck_tenant_convention_history_change_kind", type_="check")
        batch_op.drop_column("request_id")
        batch_op.drop_column("change_kind")
