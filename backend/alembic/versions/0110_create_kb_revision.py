# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Create the ``kb_revision`` table and copy in each current kb body (#3753).

Revision ID: 0110
Revises: 0109
Create Date: 2026-10-17

A kb entry is one ``documents`` row (``source='kb'``). A new body
overwrites that row, so the old text was lost. This table keeps each
body a kb write stores, so an exact earlier revision can still be read
by its id. The ``documents`` row stays the latest head; nothing about
it changes.

What this migration adds
------------------------

* The ``kb_revision`` table, with a foreign key to ``documents.id``
  (``ON DELETE CASCADE``) and to ``tenant.id``.
* A unique index ``kb_revision_entry_number_idx`` on
  ``(entry_id, revision_number)``.
* A CHECK constraint that pins ``origin`` to ``write`` or
  ``backfill-current``.

Backfill
--------

Each existing kb entry (``source='kb'``, ``kind='kb-entry'``) gets one
row: revision 1, a copy of the current body, hash and provenance block,
``origin='backfill-current'``. The migration does not know which model
or chunking rule indexed the body, so ``embedding_model`` and
``chunker_version`` stay ``NULL``. It never makes up older versions:
there is no stored history to copy.

The copy runs in Python so it works the same on PostgreSQL and SQLite.
A kb corpus is small (hundreds of entries), and the rows are inserted
in batches.

Reversibility
-------------

Purely additive. An older image never reads the table, so a rollback
is safe. ``downgrade()`` drops the table, which discards the retained
revisions; the ``documents`` rows are untouched.
"""

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0110"
down_revision: str | None = "0109"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "kb_revision"
_BATCH = 500


def upgrade() -> None:
    """Create ``kb_revision`` and copy one current revision per kb entry."""
    bind = op.get_bind()
    is_postgres = bind.dialect.name == "postgresql"
    # ``none_as_null`` stores Python ``None`` as SQL ``NULL``, not JSON ``null``.
    json_type = sa.JSON(none_as_null=True).with_variant(
        postgresql.JSONB(none_as_null=True), "postgresql"
    )

    revisions = op.create_table(
        _TABLE,
        sa.Column(
            "id",
            sa.Uuid(),
            primary_key=True,
            nullable=False,
            server_default=sa.text("gen_random_uuid()") if is_postgres else None,
        ),
        sa.Column("tenant_id", sa.Uuid(), sa.ForeignKey("tenant.id"), nullable=False),
        sa.Column(
            "entry_id",
            sa.Uuid(),
            sa.ForeignKey("documents.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("revision_number", sa.Integer(), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("body_hash", sa.Text(), nullable=False),
        sa.Column("provenance", json_type, nullable=True),
        sa.Column("origin", sa.Text(), nullable=False),
        sa.Column("embedding_model", sa.Text(), nullable=True),
        sa.Column("chunker_version", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "origin IN ('write', 'backfill-current')",
            name="ck_kb_revision_origin",
        ),
    )
    op.create_index(
        "kb_revision_entry_number_idx",
        _TABLE,
        ["entry_id", "revision_number"],
        unique=True,
    )
    _backfill_current(bind, revisions, json_type)


def _backfill_current(bind: sa.Connection, revisions: sa.Table, json_type: Any) -> None:
    """Insert revision 1 (``backfill-current``) for every existing kb entry."""
    documents = sa.table(
        "documents",
        sa.column("id", sa.Uuid()),
        sa.column("tenant_id", sa.Uuid()),
        sa.column("source", sa.Text()),
        sa.column("kind", sa.Text()),
        sa.column("body", sa.Text()),
        sa.column("body_hash", sa.Text()),
        sa.column("metadata", json_type),
    )
    rows = bind.execute(
        sa.select(
            documents.c.id,
            documents.c.tenant_id,
            documents.c.body,
            documents.c.body_hash,
            documents.c.metadata,
        )
        .where(documents.c.source == "kb", documents.c.kind == "kb-entry")
        .order_by(documents.c.id)
    ).all()
    now = datetime.now(UTC)
    batch: list[dict[str, Any]] = []
    for row in rows:
        metadata = row.metadata if isinstance(row.metadata, dict) else {}
        provenance = metadata.get("provenance")
        batch.append(
            {
                "id": uuid.uuid4(),
                "tenant_id": row.tenant_id,
                "entry_id": row.id,
                "revision_number": 1,
                "body": row.body,
                "body_hash": row.body_hash,
                "provenance": provenance if isinstance(provenance, dict) else None,
                "origin": "backfill-current",
                "embedding_model": None,
                "chunker_version": None,
                "created_at": now,
            }
        )
        if len(batch) >= _BATCH:
            op.bulk_insert(revisions, batch)
            batch = []
    if batch:
        op.bulk_insert(revisions, batch)


def downgrade() -> None:
    """Drop the index, then the table (retained revisions are discarded)."""
    op.drop_index("kb_revision_entry_number_idx", table_name=_TABLE)
    op.drop_table(_TABLE)
