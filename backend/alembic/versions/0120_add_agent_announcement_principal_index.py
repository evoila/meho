# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Index ``agent_announcement`` by tenant, principal and time (#5005).

Revision ID: 0120
Revises: 0119
Create Date: 2026-10-29

Why
---

The claim lookup (``broadcast/announce_claim_lookup.py``, #4091) runs on
every write from an agent session, and twice when the announce gate is on.
It reads the caller's own announcements of the last day:

    WHERE tenant_id = ? AND principal_sub = ? AND created_at >= ?
    ORDER BY created_at DESC LIMIT 500

The only index that fitted was ``(tenant_id, created_at DESC)``. With it,
PostgreSQL walks the tenant's announcements newest first and skips other
principals' rows, so on a busy tenant it can read every announcement of
the last day to find the caller's few rows. The new index starts with the same two equality
columns as the filter, so the lookup reads only the caller's rows, already
in the order it wants.

* ``agent_announcement_tenant_principal_created_at_idx`` -- b-tree on
  ``(tenant_id, principal_sub, created_at DESC)``.

``created_at DESC`` is written as ``sa.text`` because the sort order does
not round-trip through Alembic autogenerate; migration ``0067`` does the
same for ``agent_announcement_tenant_created_at_idx``. The index is not
declared on the model for the same reason.

A plain ``CREATE INDEX`` inside the migration transaction, the same as
migration ``0113``. It blocks writes to the table while it builds. The
table is small today (#5005: the index is for when it grows), so the build
is short. ``CONCURRENTLY`` cannot run inside a transaction.

Reversibility
-------------

``downgrade()`` drops the index. No data changes.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0120"
down_revision: str | None = "0119"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "agent_announcement"
_INDEX = "agent_announcement_tenant_principal_created_at_idx"


def upgrade() -> None:
    """Create the tenant + principal + time index."""
    op.create_index(
        _INDEX,
        _TABLE,
        ["tenant_id", "principal_sub", sa.text("created_at DESC")],
        postgresql_using="btree",
    )


def downgrade() -> None:
    """Drop the tenant + principal + time index."""
    op.drop_index(_INDEX, table_name=_TABLE)
