# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Add ``event_outbox.dedupe_key`` + ``origin`` columns (#2879).

Revision ID: 0073
Revises: 0072
Create Date: 2026-08-17

Task #2879 under Initiative #2877. Two additive, nullable columns plus one
partial unique index on the ``event_outbox`` table (migration ``0027``),
the substrate for at-least-once external event ingest (the ingest handler
that populates them lands in #2881 -- out of scope here, schema only):

* ``dedupe_key`` -- Text, nullable. Producer-supplied idempotency token.
  Webhook senders retry by nature, so at-least-once ingest needs
  DB-enforced idempotency: a duplicate ``(tenant_id, dedupe_key)`` must
  collide at insert instead of double-firing a subscriber. NULL for the
  existing internal producers (agent-run completion, ...), which leave it
  unset -- so this migration is behaviour-preserving on its own and the
  ``events.publish`` call site (operations/agent_run.py) is untouched.

* ``origin`` -- Text, nullable. Event provenance: NULL means an internal
  MEHO producer, a non-NULL value is the external event-source id. Gives
  audit / policy surfaces a column to key "external input" on instead of
  parsing the free-text ``event_kind``.

Partial unique index -- and why partial
---------------------------------------

``event_outbox_tenant_dedupe_idx`` is ``UNIQUE (tenant_id, dedupe_key)
WHERE dedupe_key IS NOT NULL``. The ``WHERE`` predicate keeps every
NULL-``dedupe_key`` internal-producer row (the overwhelming majority) out
of the index entirely, so the index carries only the externally-ingested
rows the dedupe check actually consults and its size stays flat as the
internal outbox grows. Uniqueness is per tenant: two tenants may reuse the
same ``dedupe_key`` string. A duplicate insert raises ``IntegrityError``,
which the #2881 ingest endpoint maps to an idempotent ``200``.

Dialect portability
-------------------

The partial predicate is emitted on **both** dialects via the
``postgresql_where`` / ``sqlite_where`` keyword pair on
:func:`op.create_index` -- the same pattern migration ``0072`` uses for
``targets_tenant_name_idx``. PostgreSQL supports partial indexes natively;
SQLite has since 3.8.0 (we run 3.45+), so the unit-test path enforces the
exact same partial-unique shape production does. (This is a deliberate
departure from ``0027``'s original ``event_outbox_unprocessed_idx``, which
predates the repo's move to ``sqlite_where`` and fell back to a plain
b-tree on SQLite.)

Additive-only forward, reversible back
--------------------------------------

``upgrade()`` only adds nullable columns and creates an index -- purely
additive, so it clears ``scripts/ci/check_migration_compat.py`` and an
older image reading the newer schema simply never references the two new
columns (the ``helm rollback`` = image-revert + forward-compat-schema
contract, ``docs/codebase/migrations.md`` / #1607, holds).

``downgrade()`` drops the index, then drops the two columns inside a
``batch_alter_table`` block (reverse add order). Batch mode is required
because SQLite adds column-drop support only via the table-recreate
cookbook; the index is dropped first, outside the batch, so the recreate
reflects a table that no longer carries it.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0073"
down_revision: str | None = "0072"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add the two nullable columns + the partial unique dedupe index."""
    op.add_column("event_outbox", sa.Column("dedupe_key", sa.Text(), nullable=True))
    op.add_column("event_outbox", sa.Column("origin", sa.Text(), nullable=True))
    op.create_index(
        "event_outbox_tenant_dedupe_idx",
        "event_outbox",
        ["tenant_id", "dedupe_key"],
        unique=True,
        postgresql_using="btree",
        postgresql_where=sa.text("dedupe_key IS NOT NULL"),
        sqlite_where=sa.text("dedupe_key IS NOT NULL"),
    )


def downgrade() -> None:
    """Drop the dedupe index, then the two columns (reverse add order)."""
    op.drop_index("event_outbox_tenant_dedupe_idx", table_name="event_outbox")
    with op.batch_alter_table("event_outbox") as batch_op:
        batch_op.drop_column("origin")
        batch_op.drop_column("dedupe_key")
