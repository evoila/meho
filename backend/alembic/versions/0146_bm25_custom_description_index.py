# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Add a keyword index on the admin's operation description (#4753).

Revision ID: 0146
Revises: 0145
Create Date: 2026-11-08

Why
---

An admin can replace an operation's ``description`` with
``custom_description``. When the switch ``AGENT_READS_CUSTOM_DESCRIPTION``
is on, the keyword (BM25) half of ``search_operations`` reads the admin's
text in place of ``description`` (``operations/_search.py``,
``BM25_CUSTOM_DOCUMENT_SQL``). PostgreSQL uses an expression index only
when the query has the same expression, so this migration adds a second
index, ``endpoint_descriptor_bm25_custom_idx``, on the new text:

    to_tsvector('english', coalesce(summary, '') || ' '
        || coalesce(nullif(custom_description, ''), description, ''))

The index of migration ``0005`` (``endpoint_descriptor_bm25_idx``, on
``summary`` and ``description``) stays as it is. The keyword query of the
switch's off state (the default) still uses it, so nothing changes for an
install that leaves the switch off.

PostgreSQL only, like ``0005``. A plain ``CREATE INDEX`` inside the
migration transaction: it blocks writes to ``endpoint_descriptor`` while
it builds. ``CONCURRENTLY`` cannot run inside a transaction.

Rollback
--------

No data changes. ``downgrade()`` drops the new index. An older image never
queries it.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0146"
down_revision: str | None = "0145"
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

INDEX = "endpoint_descriptor_bm25_custom_idx"

#: Frozen copy of the index expression. It must match
#: ``BM25_CUSTOM_DOCUMENT_SQL`` in ``operations/_search.py``;
#: ``tests/migrations/test_migration_0146_bm25_custom_description_index.py``
#: checks that.
CUSTOM_DOCUMENT_SQL = (
    "to_tsvector('english', coalesce(summary, '') || ' ' "
    "|| coalesce(nullif(custom_description, ''), description, ''))"
)


def upgrade() -> None:
    """Add the keyword index on the summary and the admin's description."""
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute(f"DROP INDEX IF EXISTS {INDEX}")
    op.execute(f"CREATE INDEX {INDEX} ON endpoint_descriptor USING GIN ({CUSTOM_DOCUMENT_SQL})")


def downgrade() -> None:
    """Drop the keyword index on the admin's description."""
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute(f"DROP INDEX IF EXISTS {INDEX}")
