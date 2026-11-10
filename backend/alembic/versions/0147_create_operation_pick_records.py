# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Create ``operation_search_record`` and ``operation_call_record``.

Revision ID: 0147
Revises: 0146
Create Date: 2026-11-08

MEHO keeps a short record of each ``search_operations`` call and of each
``call_operation`` call, and links a call to the search it followed
(#4750). A report then shows per connector and day how often the agent
called an operation that the search returned, and whether it ran.

* ``operation_search_record`` -- the connector, group, limit, outcome,
  the returned operation ids in rank order, and a hash of the query. The
  query text only when a setting turns it on.
* ``operation_call_record`` -- the connector, ``op_id``, outcome, the
  linked search (``search_id``) and the rank of ``op_id`` in it.

Each table has a ``(tenant_id, connector_id, occurred_at)`` index for the
link lookup and the report, and an ``occurred_at`` index for the daily
retention delete. The tables are new and empty, so the migration changes
no behaviour on its own. ``downgrade()`` drops the indexes and the tables.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

# revision identifiers, used by Alembic.
revision: str = "0147"
down_revision: str | None = "0146"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Alembic reads these names itself. List them in __all__, so the code-quality
# check does not report them as unused globals. Keep a plain list of strings
# with no type annotation: the check reads only that form.
__all__ = [
    "branch_labels",
    "depends_on",
    "down_revision",
    "downgrade",
    "revision",
    "upgrade",
]

_SEARCH = "operation_search_record"
_CALL = "operation_call_record"


def _common_columns() -> list[sa.Column[object]]:
    """The columns both tables share."""
    return [
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("principal_sub", sa.Text(), nullable=False),
        sa.Column("session_id", sa.Uuid(), nullable=True),
        sa.Column("request_id", sa.Uuid(), nullable=True),
        sa.Column("transport", sa.Text(), nullable=False),
        sa.Column("connector_id", sa.Text(), nullable=False),
    ]


def _indexes(table: str) -> None:
    """Create the link/report index and the retention index of *table*."""
    op.create_index(
        f"{table}_tenant_connector_idx",
        table,
        ["tenant_id", "connector_id", "occurred_at"],
    )
    op.create_index(f"{table}_occurred_at_idx", table, ["occurred_at"])


def upgrade() -> None:
    """Create both tables and their indexes."""
    op.create_table(
        _SEARCH,
        *_common_columns(),
        sa.Column("mode", sa.Text(), nullable=False),
        sa.Column("group_key", sa.Text(), nullable=True),
        sa.Column("search_limit", sa.Integer(), nullable=True),
        sa.Column("outcome", sa.Text(), nullable=False),
        sa.Column("hit_count", sa.Integer(), nullable=False),
        sa.Column("op_ids", sa.JSON().with_variant(JSONB(), "postgresql"), nullable=False),
        sa.Column("query_hash", sa.Text(), nullable=True),
        sa.Column("query_text", sa.Text(), nullable=True),
    )
    _indexes(_SEARCH)
    op.create_table(
        _CALL,
        *_common_columns(),
        sa.Column("op_id", sa.Text(), nullable=False),
        sa.Column("outcome", sa.Text(), nullable=False),
        sa.Column("search_id", sa.Uuid(), nullable=True),
        sa.Column("rank", sa.Integer(), nullable=True),
    )
    _indexes(_CALL)


def downgrade() -> None:
    """Drop the indexes, then the tables."""
    for table in (_CALL, _SEARCH):
        op.drop_index(f"{table}_occurred_at_idx", table_name=table)
        op.drop_index(f"{table}_tenant_connector_idx", table_name=table)
        op.drop_table(table)
