# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Add ``idempotency_key`` to ``approval_request`` (#3486).

Revision ID: 0105
Revises: 0104
Create Date: 2026-10-16

A client may send an ``idempotency_key`` on ``call_operation``. When the
call waits for approval, the key is stored on the new approval row. If the
same principal sends the same key again, MEHO returns the existing row
instead of creating a second one.

This adds:

* one nullable ``Text`` column, ``idempotency_key``. Existing rows read
  back NULL, which means "no key was sent", so nothing changes for them.
* one partial unique index on ``(tenant_id, principal_sub,
  idempotency_key)``. It only covers rows that have a key and are still
  ``pending`` or ``approved``. So two live rows can never share a key, even
  when two requests race. A rejected or expired row leaves the index, and
  the key can be used again for a new request.

The partial predicate is emitted on both dialects with the
``postgresql_where`` / ``sqlite_where`` pair, the same pattern as
migrations ``0072`` and ``0073``. ``upgrade()`` is additive only.
``downgrade()`` drops the index first, then the column in a batch block
(SQLite needs the table-recreate path to drop a column).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0105"
down_revision: str | None = "0104"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_INDEX = "approval_request_idempotency_key_idx"
_PREDICATE = "idempotency_key IS NOT NULL AND status IN ('pending', 'approved')"


def upgrade() -> None:
    op.add_column("approval_request", sa.Column("idempotency_key", sa.Text(), nullable=True))
    op.create_index(
        _INDEX,
        "approval_request",
        ["tenant_id", "principal_sub", "idempotency_key"],
        unique=True,
        postgresql_using="btree",
        postgresql_where=sa.text(_PREDICATE),
        sqlite_where=sa.text(_PREDICATE),
    )


def downgrade() -> None:
    op.drop_index(_INDEX, table_name="approval_request")
    with op.batch_alter_table("approval_request") as batch_op:
        batch_op.drop_column("idempotency_key")
