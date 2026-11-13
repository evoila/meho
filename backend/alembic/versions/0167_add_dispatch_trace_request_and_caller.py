# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Add ``dispatch_trace.request_id`` and ``dispatch_trace.operator_sub``.

Revision ID: 0167
Revises: 0166
Create Date: 2026-11-13

Two nullable columns on the trace header: the request id of the call and
the caller. With them a stored trace can be found without its audit row,
by request id or by time and caller (the trace search route
``GET /api/v1/audit/traces``). Traces stored before this change keep NULL
in both columns and are found by their audit id as before.

An older build never reads the columns, so a rollback only loses them.

``downgrade()`` drops the indexes and the columns.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0167"
down_revision: str | None = "0166"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Alembic reads these names itself. List them in __all__, so the code-quality
# check does not report them as unused globals.
__all__ = [
    "branch_labels",
    "depends_on",
    "down_revision",
    "downgrade",
    "revision",
    "upgrade",
]

_TABLE = "dispatch_trace"
_REQUEST_INDEX = "dispatch_trace_request_id_idx"
_CALLER_INDEX = "dispatch_trace_operator_created_idx"


def upgrade() -> None:
    """Add the two columns and the two indexes that serve the search."""
    with op.batch_alter_table(_TABLE) as batch:
        batch.add_column(sa.Column("request_id", sa.Uuid(), nullable=True))
        batch.add_column(sa.Column("operator_sub", sa.Text(), nullable=True))
    op.create_index(_REQUEST_INDEX, _TABLE, ["request_id"], postgresql_using="btree")
    op.create_index(
        _CALLER_INDEX, _TABLE, ["tenant_id", "operator_sub", "created_at"], postgresql_using="btree"
    )


def downgrade() -> None:
    """Drop the indexes and the two columns."""
    op.drop_index(_CALLER_INDEX, table_name=_TABLE)
    op.drop_index(_REQUEST_INDEX, table_name=_TABLE)
    with op.batch_alter_table(_TABLE) as batch:
        batch.drop_column("operator_sub")
        batch.drop_column("request_id")
