# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Add ``audit_log.caller_kind``: the kind of caller of the row.

Revision ID: 0191
Revises: 0190
Create Date: 2026-11-27

One nullable text column and one CHECK, ``ck_audit_log_caller_kind``. The
CHECK allows only the five kinds of
:func:`meho_backplane.auth.caller_kind.caller_kind`: ``person``,
``personal_agent``, ``service_agent``, ``runner`` and ``system``, or
``NULL``. Nothing writes the column yet, so every row keeps ``NULL``.

The append-only trigger on ``audit_log`` (migrations ``0100`` and ``0103``)
fires only on ``UPDATE`` and ``DELETE``, so adding a column works. A
nullable column with no default does not rewrite the rows.

On PostgreSQL the CHECK is added ``NOT VALID``, the same as migration
``0184``: the upgrade does not read the whole table, and the database
checks every new write at once. Every old row is ``NULL``, so it passes
the CHECK anyway.

SQLite adds the column with the CHECK in the same ``ALTER TABLE``. It does
not rebuild the table: a rebuild in ``batch_alter_table`` loses the
expression index of migration ``0179``, because SQLAlchemy cannot read an
expression index back from SQLite. SQLite drops a column together with its
own CHECK, so the downgrade does not rebuild the table either.

The kinds are written out here, not imported, so this migration keeps
working when the code changes later. A test pins them to
``CALLER_KINDS``.

``downgrade()`` drops the CHECK and the column. An older build never reads
the column, so a rollback is safe.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0191"
down_revision: str | None = "0190"
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

_TABLE = "audit_log"
_COLUMN = "caller_kind"
_CHECK = "ck_audit_log_caller_kind"
_CONDITION = (
    "caller_kind IN ('person', 'personal_agent', 'service_agent', 'runner', 'system') "
    "OR caller_kind IS NULL"
)


def _is_sqlite() -> bool:
    return op.get_bind().dialect.name == "sqlite"


def upgrade() -> None:
    """Add the nullable column and its CHECK."""
    if _is_sqlite():
        op.execute(
            sa.text(
                f"ALTER TABLE {_TABLE} ADD COLUMN {_COLUMN} TEXT "
                f"CONSTRAINT {_CHECK} CHECK ({_CONDITION})"
            )
        )
        return
    op.add_column(_TABLE, sa.Column(_COLUMN, sa.Text(), nullable=True))
    op.create_check_constraint(_CHECK, _TABLE, _CONDITION, postgresql_not_valid=True)


def downgrade() -> None:
    """Drop the CHECK and the column."""
    if not _is_sqlite():
        op.drop_constraint(_CHECK, _TABLE, type_="check")
    op.drop_column(_TABLE, _COLUMN)
