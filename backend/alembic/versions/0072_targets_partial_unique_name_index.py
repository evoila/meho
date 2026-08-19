# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Rebuild ``targets_tenant_name_idx`` as a partial unique index.

Revision ID: 0072
Revises: 0071
Create Date: 2026-08-17

Fixes #2874 (G0.39). ``DELETE /api/v1/targets/{name}`` soft-deletes by
stamping ``deleted_at`` (migration ``0029``, G0.14-T4 #1145) and every
read path filters ``deleted_at IS NULL`` -- but ``targets_tenant_name_idx``
was created by migration ``0004`` as a **FULL** unique b-tree on
``(tenant_id, name)`` two weeks before soft-delete existed, and ``0029``
never converted it. The tombstone therefore keeps occupying the unique
slot forever: invisible to every read, yet permanently blocking a
re-create of the same name with a 409 (``create_target`` inserts and maps
the flush-time ``IntegrityError`` to the 409 -- there is no app-level
uniqueness check to relax). An operator who does DELETE + POST to get a
fresh row is stuck: GET says 404, POST says 409, with no path out.

The fix is to make uniqueness apply only to **live** rows:

    UNIQUE (tenant_id, name) WHERE deleted_at IS NULL

A live duplicate still collides (both rows have ``deleted_at IS NULL``),
so ``create_target``'s 409 path is unchanged; an insert whose only
same-name neighbours are tombstones no longer collides and simply
succeeds. No handler code changes.

Why drop + recreate (not alter)
-------------------------------

Neither PostgreSQL nor SQLite can alter an existing index's ``WHERE``
predicate in place -- the only way to add the partial predicate is to
drop the index and recreate it. ``op.drop_index`` is **not** a
destructive operation under the additive-only rollback contract
(``docs/codebase/migrations.md``): it removes no data and no column, and
``scripts/ci/check_migration_compat.py`` bans ``drop_column`` /
``drop_table`` / ``rename`` / ``alter ... nullable=False`` but not
``drop_index``. The rebuild is forward-compatible: an older image still
running against the new partial index enforces live-duplicate uniqueness
exactly as before and still 409s on a live duplicate -- it merely also
tolerates an insert over a tombstone, which is harmless. The
``helm rollback`` = image-revert + forward-compat-schema contract
(#1607) therefore holds.

No backfill, and every wedged name frees immediately
----------------------------------------------------

The upgrade needs **no** data migration. The old FULL unique index was
strictly *stricter* than the partial one -- it already guaranteed at
most one row per ``(tenant_id, name)`` regardless of ``deleted_at`` --
so the ``targets`` table trivially satisfies the partial constraint and
``CREATE UNIQUE INDEX ... WHERE deleted_at IS NULL`` cannot fail on any
existing data. Every name currently wedged by a tombstone frees the
moment this migration lands: the tombstone rows stay in the table (so
``audit_log.target_id`` soft-FKs keep resolving and ``query_audit
target=<name>`` still spans the deleted incarnation), they just stop
participating in the uniqueness check.

``targets`` is an operator-scale table (tens to low hundreds of rows per
tenant), so a plain ``CREATE UNIQUE INDEX`` inside the migration
transaction is fine; ``CONCURRENTLY`` (which cannot run in a transaction)
is unnecessary and would break the single-transaction migration model.

Dialect-portability
-------------------

The partial predicate is emitted on both dialects via the
``postgresql_where`` / ``sqlite_where`` keyword pair on
:func:`op.create_index` -- the same pattern migration ``0005`` uses for
the ``operation_group`` / ``endpoint_descriptor`` partial unique indexes.
PostgreSQL supports partial indexes natively; SQLite has since 3.8.0 (we
run 3.45+). ``postgresql_using="btree"`` is preserved from ``0004``.

Reversibility contract -- and its caveat
----------------------------------------

``downgrade()`` drops the partial index and recreates the original FULL
unique index. **Caveat:** the downgrade raises ``IntegrityError`` if any
``(tenant_id, name)`` pair has been legitimately re-used after the
upgrade -- i.e. a live row plus one or more tombstones sharing the pair,
which is exactly the state this migration makes reachable. The FULL
index cannot be built over such data. This is an accepted, documented
limitation: production **never** runs ``alembic downgrade`` (rollback is
image-revert + forward-compat schema discipline, ``migrations.md``); the
downgrade exists for development-time symmetry and manual operator use,
where the operator must first resolve any re-used names (hard-delete the
tombstones) before downgrading. Silent partial recovery would be worse
than an explicit failure.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0072"
down_revision: str | None = "0071"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Rebuild ``targets_tenant_name_idx`` as ``UNIQUE ... WHERE deleted_at IS NULL``."""
    op.drop_index("targets_tenant_name_idx", table_name="targets")
    op.create_index(
        "targets_tenant_name_idx",
        "targets",
        ["tenant_id", "name"],
        unique=True,
        postgresql_using="btree",
        postgresql_where=sa.text("deleted_at IS NULL"),
        sqlite_where=sa.text("deleted_at IS NULL"),
    )


def downgrade() -> None:
    """Restore the original FULL unique index (see the reused-name caveat).

    Fails with ``IntegrityError`` if a ``(tenant_id, name)`` pair was
    re-used post-upgrade (a live row coexisting with a tombstone). Resolve
    such duplicates before downgrading.
    """
    op.drop_index("targets_tenant_name_idx", table_name="targets")
    op.create_index(
        "targets_tenant_name_idx",
        "targets",
        ["tenant_id", "name"],
        unique=True,
        postgresql_using="btree",
    )
