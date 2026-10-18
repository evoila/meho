# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Add a commit-order sequence to ``agent_announcement`` (#3748).

Revision ID: 0114
Revises: 0113
Create Date: 2026-10-18

Readers now page and watch the announcement archive by a per-tenant
sequence number instead of the Valkey stream id. This adds:

* ``agent_announcement_sequence`` -- one counter row per tenant
  (``tenant_id`` PK, ``last_seq``). The publisher raises ``last_seq`` in
  the same transaction as the announcement INSERT, so the number follows
  commit order.
* ``agent_announcement.seq`` -- the row's sequence number, nullable.
* ``agent_announcement_tenant_seq_uq`` -- unique ``(tenant_id, seq)``.

Existing rows get numbers 1..n per tenant, oldest first (``created_at``,
then ``id``), and each tenant's counter starts at its highest number.

``seq`` stays nullable. A pod that still runs the older code during a
rolling upgrade writes rows without a number; the next read gives those
rows the next numbers. Making the column ``NOT NULL`` is a later,
separate migration.

``downgrade()`` drops the index, the column and the counter table.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0114"
down_revision: str | None = "0113"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "agent_announcement"
_COUNTER = "agent_announcement_sequence"
_INDEX = "agent_announcement_tenant_seq_uq"

#: Number existing rows 1..n per tenant, oldest first. ``UPDATE ... FROM``
#: works on PostgreSQL and on SQLite 3.33+.
_BACKFILL_SEQ = sa.text(
    """
    UPDATE agent_announcement
    SET seq = numbered.seq
    FROM (
        SELECT id,
               ROW_NUMBER() OVER (PARTITION BY tenant_id ORDER BY created_at, id) AS seq
        FROM agent_announcement
    ) AS numbered
    WHERE agent_announcement.id = numbered.id
    """
)

_SEED_COUNTERS = sa.text(
    """
    INSERT INTO agent_announcement_sequence (tenant_id, last_seq)
    SELECT tenant_id, MAX(seq) FROM agent_announcement GROUP BY tenant_id
    """
)


def upgrade() -> None:
    op.create_table(
        _COUNTER,
        sa.Column("tenant_id", sa.Uuid(), sa.ForeignKey("tenant.id"), primary_key=True),
        sa.Column("last_seq", sa.BigInteger(), nullable=False, server_default="0"),
    )
    op.add_column(_TABLE, sa.Column("seq", sa.BigInteger(), nullable=True))
    op.execute(_BACKFILL_SEQ)
    op.execute(_SEED_COUNTERS)
    op.create_index(_INDEX, _TABLE, ["tenant_id", "seq"], unique=True)


def downgrade() -> None:
    op.drop_index(_INDEX, table_name=_TABLE)
    op.drop_column(_TABLE, "seq")
    op.drop_table(_COUNTER)
