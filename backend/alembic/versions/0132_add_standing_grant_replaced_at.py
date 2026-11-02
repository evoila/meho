# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Add ``replaced_at`` to both standing-grant tables (#5424).

Revision ID: 0132
Revises: 0131
Create Date: 2026-11-02

A grant that has ended (``expires_at`` in the past) but was never revoked
still held its key in the two partial unique indexes, because an index
cannot compare with the current time. So a new grant with the same key was
refused with "already exists; revoke it first".

Now the grant service sets ``replaced_at`` on such an ended row in the same
transaction that adds the new grant. This migration, for
``service_principal_grant`` and ``sandbox_delete_grant``:

* adds the nullable column ``replaced_at``. Existing rows read ``NULL``;
* builds ``uq_<table>_targeted`` and ``uq_<table>_targetless`` again with
  ``AND replaced_at IS NULL`` added to their ``WHERE``. Every existing row
  has ``replaced_at`` ``NULL``, so the indexes cover the same rows as
  before.

An older build does not know the column. After a rollback without
``downgrade()`` it still writes rows (the column stays ``NULL``) and the
indexes still refuse a live duplicate.

``downgrade()`` first marks each replaced row as revoked (``revoked_at`` =
``replaced_at``), because the old indexes would refuse the ended row and its
new grant side by side. Then it builds the old indexes and drops the column.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0132"
down_revision: str | None = "0131"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLES = ("service_principal_grant", "sandbox_delete_grant")
_COLUMN = "replaced_at"
_KEY = ["tenant_id", "principal_sub", "op_id", "connector_id"]
_TARGETLESS = "target_id IS NULL AND target_product IS NULL AND target_name_pattern IS NULL"
_TARGETED = "target_id IS NOT NULL"


def _build_unique_indexes(table: str, live: str) -> None:
    """Drop and build the two partial unique indexes of *table* with *live* in ``WHERE``."""
    for suffix, columns, shape in (
        ("targeted", [*_KEY, "target_id"], _TARGETED),
        ("targetless", _KEY, _TARGETLESS),
    ):
        name = f"uq_{table}_{suffix}"
        where = sa.text(f"{shape} AND {live}")
        op.drop_index(name, table_name=table)
        op.create_index(
            name,
            table,
            columns,
            unique=True,
            postgresql_where=where,
            sqlite_where=where,
        )


def upgrade() -> None:
    """Add ``replaced_at`` and leave replaced rows out of the unique indexes."""
    for table in _TABLES:
        op.add_column(table, sa.Column(_COLUMN, sa.DateTime(timezone=True), nullable=True))
        _build_unique_indexes(table, "revoked_at IS NULL AND replaced_at IS NULL")


def downgrade() -> None:
    """Mark replaced rows revoked, build the old indexes, drop ``replaced_at``."""
    for table in _TABLES:
        op.execute(
            sa.text(
                f"UPDATE {table} SET revoked_at = replaced_at, "
                "revoked_reason = COALESCE(revoked_reason, 'replaced after it ended') "
                "WHERE replaced_at IS NOT NULL AND revoked_at IS NULL"
            )
        )
        _build_unique_indexes(table, "revoked_at IS NULL")
        with op.batch_alter_table(table) as batch:
            batch.drop_column(_COLUMN)
