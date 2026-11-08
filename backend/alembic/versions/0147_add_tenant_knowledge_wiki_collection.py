# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Add the tenant setting ``knowledge_wiki_collection`` (#5976).

Revision ID: 0147
Revises: 0146
Create Date: 2026-11-08

The setting names the docs collection where a tenant keeps its lessons: the
team wiki, loaded into a knowledge index. ``search_knowledge`` and ``meho kb
search`` also search that collection, and ``add_to_knowledge`` tells the
caller that the lesson also goes to the wiki as a candidate page.

One nullable ``Text`` column on ``tenant``. ``NULL`` means lessons live in
MEHO only, as before, so the column needs no backfill. An older build never
reads it, so a rollback is safe. ``downgrade()`` drops the column.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

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


def upgrade() -> None:
    """Add the nullable column; existing tenants keep ``NULL``."""
    op.add_column("tenant", sa.Column("knowledge_wiki_collection", sa.Text(), nullable=True))


def downgrade() -> None:
    """Drop the column."""
    op.drop_column("tenant", "knowledge_wiki_collection")
