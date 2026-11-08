# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Build the operation keyword index on the admin's description (#4753).

Revision ID: 0145
Revises: 0144
Create Date: 2026-11-08

Why
---

An admin can replace an operation's ``description`` with
``custom_description``. The keyword (BM25) half of ``search_operations``
now reads the admin's text in place of ``description`` when it is set and
not empty (``operations/_search.py``, ``BM25_DOCUMENT_SQL``). PostgreSQL
uses an expression index only when the query has the same expression, so
``endpoint_descriptor_bm25_idx`` is built again on the new text:

    to_tsvector('english', coalesce(summary, '') || ' '
        || coalesce(nullif(custom_description, ''), description, ''))

Migration ``0005`` built it on ``summary`` and ``description`` only.

PostgreSQL only, like ``0005``. A plain ``CREATE INDEX`` inside the
migration transaction: it blocks writes to ``endpoint_descriptor`` while
it builds. ``CONCURRENTLY`` cannot run inside a transaction.

Rollback
--------

No data changes. An older image still runs its own query on the new
index: the results are the same, but that query cannot use the index, so
it scans the table. ``downgrade()`` builds the index of ``0005`` again.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0145"
down_revision: str | None = "0144"
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

_INDEX = "endpoint_descriptor_bm25_idx"

#: Frozen copies of the two index expressions. The new one must match
#: ``BM25_DOCUMENT_SQL`` in ``operations/_search.py``;
#: ``tests/migrations/test_migration_0145_bm25_index_uses_custom_description.py``
#: checks that.
NEW_DOCUMENT_SQL = (
    "to_tsvector('english', coalesce(summary, '') || ' ' "
    "|| coalesce(nullif(custom_description, ''), description, ''))"
)
OLD_DOCUMENT_SQL = (
    "to_tsvector('english', coalesce(summary, '') || ' ' || coalesce(description, ''))"
)


def _rebuild(document_sql: str) -> None:
    op.execute(f"DROP INDEX IF EXISTS {_INDEX}")
    op.execute(f"CREATE INDEX {_INDEX} ON endpoint_descriptor USING GIN ({document_sql})")


def upgrade() -> None:
    """Build the keyword index on the summary and the description the agent reads."""
    if op.get_bind().dialect.name != "postgresql":
        return
    _rebuild(NEW_DOCUMENT_SQL)


def downgrade() -> None:
    """Build the keyword index of migration ``0005`` again."""
    if op.get_bind().dialect.name != "postgresql":
        return
    _rebuild(OLD_DOCUMENT_SQL)
