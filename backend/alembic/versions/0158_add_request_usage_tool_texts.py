# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Add ``request_usage_event.tool_texts``.

Revision ID: 0158
Revises: 0157
Create Date: 2026-11-11

An MCP client can ask for the short tool texts for one session with
``?texts=short`` on the MCP URL. The ``tools/call`` audit row names that
choice, but with ``AUDIT_WRAPPER_ROWS_RETIRED=true`` a read or a
``call_operation`` writes no such row. The usage event is then the only
record of the request, so it names the choice too.

* ``tool_texts`` -- ``short`` when the session asked for the short texts,
  ``NULL`` otherwise. A CHECK constraint pins the one value.

Reversibility
-------------

Purely additive: a nullable column. An older image never reads it.
``downgrade()`` drops the column, which discards the stored choice.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0158"
down_revision: str | None = "0157"
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

_TABLE = "request_usage_event"
_CHECK = "ck_request_usage_event_tool_texts"

#: Frozen snapshot of ``TOOL_TEXTS`` in
#: :mod:`meho_backplane.request_usage.schema`. A drift test pins it.
_TOOL_TEXTS: tuple[str, ...] = ("short",)


def upgrade() -> None:
    """Add the nullable column and its CHECK constraint."""
    allowed = ", ".join(f"'{value}'" for value in _TOOL_TEXTS)
    with op.batch_alter_table(_TABLE) as batch:
        batch.add_column(sa.Column("tool_texts", sa.Text(), nullable=True))
        batch.create_check_constraint(_CHECK, f"tool_texts IS NULL OR tool_texts IN ({allowed})")


def downgrade() -> None:
    """Drop the CHECK constraint, then the column."""
    with op.batch_alter_table(_TABLE) as batch:
        batch.drop_constraint(_CHECK, type_="check")
        batch.drop_column("tool_texts")
