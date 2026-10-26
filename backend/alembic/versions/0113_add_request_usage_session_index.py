# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Index ``request_usage_event.session_id`` (#3757).

Revision ID: 0113
Revises: 0112
Create Date: 2026-10-17

Why
---

The reflex advisory asks, on every dispatch from an agent session,
"did this session already read the broadcast feed?". It looked only at
the ``meho_broadcast_recent`` wrapper row in ``audit_log``. After the
wrapper rows are retired (``AUDIT_WRAPPER_ROWS_RETIRED=true``), that
read leaves only a ``request_finished`` usage event, so the advisory now
also looks there by ``session_id``. Without this index that lookup reads
the whole table.

* ``request_usage_event_session_id_idx`` -- non-unique b-tree on
  ``session_id``.

Reversibility
-------------

``downgrade()`` drops the index. No data changes.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0113"
down_revision: str | None = "0112"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "request_usage_event"
_INDEX = "request_usage_event_session_id_idx"


def upgrade() -> None:
    """Create the session index."""
    op.create_index(_INDEX, _TABLE, ["session_id"])


def downgrade() -> None:
    """Drop the session index."""
    op.drop_index(_INDEX, table_name=_TABLE)
