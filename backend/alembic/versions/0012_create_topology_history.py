# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Create the topology-history (``graph_node_history`` + ``graph_edge_history``) tables.

Revision ID: 0012
Revises: 0011
Create Date: 2026-05-22

This migration is the schema substrate of Initiative #365 (G9.3
Discovery audit log -> graph history), Task #856 (T1). It adds the
two append-only history tables that mirror the live ``graph_node`` /
``graph_edge`` pair from migration ``0007`` with temporal columns,
plus an ``audit_id`` soft-link back to ``audit_log.id`` so an auditor
can pivot from a topology mutation row to the request that caused it.

T1 ships **the tables, their indexes, and their ORM models only**.
The diff-on-write hook in the refresh + annotate paths is T2 (#857);
the temporal-query verbs are T3/T4/T5 (#859/#860/#861); the retention
prune task is T6 (#858).

ADR-style note: application-managed history vs PG system-versioning
-------------------------------------------------------------------

PostgreSQL has no first-class system-versioning equivalent to SQL
Server's / DB2's ``SYSTEM_VERSIONING`` -- there is no built-in
``PERIOD FOR SYSTEM_TIME`` clause, and no ``ALTER TABLE ... ADD
SYSTEM VERSIONING`` ceremony that auto-maintains a paired history
table. The ecosystem alternatives are:

1. **Application-managed history tables.** A separate ``*_history``
   table per live table; the application writes a paired history row
   on every mutation inside the same transaction. The chassis's
   :class:`~meho_backplane.db.models.AuditLog` (migration ``0001``)
   is the same recipe (append-only, indexed by tenant + time, JSONB
   payload). v0.2 picks this path: it composes with the audit trail
   the rest of the chassis already standardises on, ships in a single
   coordinated migration, and requires zero new PG extensions on the
   production image.
2. **PG system-period temporal extensions** (``temporal_tables``,
   ``periods``, or the SQL standard period extension when it lands).
   Out of scope per Initiative #365 (decision #6) -- adds a third PG
   extension after pgvector (migration ``0003``) and any future
   policy-engine prerequisites, for marginal ergonomic gain over the
   application-managed pattern v0.2 already standardises on. The same
   queries (history-of-resource, point-in-time diff, tenant timeline)
   compose cleanly against application-managed history; the chassis
   keeps its PG dependency surface lean.
3. **Triggers writing into the same table or a shadow table.** Adds
   per-table trigger maintenance to every connector / refresh code
   path; an UPDATE that misses the trigger leaves history desynced,
   and the trigger has no access to the request's ``audit_id`` (which
   lives in a Python contextvar, not a session variable). Rejected.

Same-transaction guarantee (the load-bearing T2 property) is enforced
**at the application layer** -- the diff-on-write hook lands the
history row through the same :class:`~sqlalchemy.ext.asyncio.AsyncSession`
the live-row mutation uses, so a connector-side failure aborts the
whole unit of work atomically. T2 ships and tests that contract; T1
only ships the storage.

Table shape
-----------

Both history tables mirror :class:`~meho_backplane.db.models.AuditLog`'s
append-only recipe:

* ``history_id`` -- ``BIGSERIAL`` (PG) / autoincrementing ``INTEGER``
  (SQLite) primary key. The append-only semantics make a 64-bit
  monotonic counter the right shape: insert-ordered, cheap, ample
  headroom for the ~90-day retention window the prune task (T6)
  enforces. The dialect-portable shape is
  ``BigInteger().with_variant(Integer(), "sqlite")`` -- SQLite treats
  ``INTEGER PRIMARY KEY`` as the rowid alias (auto-incrementing)
  while ``BIGINT PRIMARY KEY`` is **not** rowid and would force the
  caller to assign ``history_id`` explicitly. The Integer variant on
  SQLite is functionally equivalent for the test path; production
  runs PG and gets ``BIGSERIAL``.
* ``node_id`` / ``edge_id`` -- ``UUID`` NOT NULL with a real
  ``REFERENCES graph_node(id) ON DELETE SET NULL`` /
  ``REFERENCES graph_edge(id) ON DELETE SET NULL`` FK. ``SET NULL``
  rather than ``CASCADE`` is the load-bearing decision: history rows
  must survive the deletion of the live row they reference. A hard
  cascade would drop the entire history of a removed node / edge --
  exactly the data G9.3 exists to preserve. The columns are nullable
  in the ORM signature so the SET NULL transition compiles; the
  insert path always populates them.
* ``tenant_id`` -- ``UUID`` NOT NULL with a real
  ``REFERENCES tenant(id)`` FK. Same brand-new-substrate rationale as
  ``graph_node.tenant_id`` (migration ``0007``) and
  ``broadcast_override.tenant_id`` (migration ``0008``) -- no
  chassis-era rows, clean downgrade drops the whole table, so the FK
  is enforced at the substrate boundary instead of being deferred to
  a tightening migration.
* ``change_kind`` -- ``TEXT`` NOT NULL with a DB-layer
  ``CHECK change_kind IN ('created', 'updated', 'removed')``
  constraint. Mirrors the portable closed-enum CHECK pattern
  migrations ``0004`` (``targets.auth_model``), ``0005``
  (``operation_group.review_status``,
  ``endpoint_descriptor.source_kind`` / ``safety_level``), and
  ``0007`` / ``0010`` (``graph_node.kind`` / ``graph_edge.kind``)
  already use. PostgreSQL native ``ENUM`` types would force an
  ``ALTER TYPE ADD VALUE`` ceremony SQLite cannot mirror; the
  ``TEXT + CHECK`` shape is dialect-portable and version-safe.
* ``snapshot`` -- portable ``JSON`` -> ``JSONB`` NOT NULL DEFAULT
  ``{}``. The ``{before, after}`` projection T2 writes -- ``before``
  is NULL-shaped for ``created``, the full pre-change row JSON for
  ``updated`` / ``removed``; ``after`` is the post-change row JSON
  for ``created`` / ``updated``, NULL-shaped for ``removed``.
  Bidirectional reconstruction is the use case ``meho topology diff``
  (T4) solves; one-sided snapshots would not suffice.
* ``audit_id`` -- ``UUID`` nullable. References the
  :class:`~meho_backplane.db.models.AuditLog` row whose request
  caused the mutation (the audit middleware pre-generates the audit
  id and the diff-on-write hook in T2 reads it from the contextvar
  the same way :func:`meho_backplane.broadcast.publisher.publish` does).
  No FK clause -- the soft-FK discipline ``audit_log.tenant_id`` /
  ``audit_log.target_id`` / ``audit_log.parent_audit_id`` already
  established (migrations ``0002`` / ``0004`` / ``0006``) avoids a
  cascade decision against a populated audit table. Audit rows are
  retained on a different cadence than topology history; a real FK
  with ``ON DELETE CASCADE`` would couple the two retention policies,
  while ``ON DELETE SET NULL`` would force a backfill / cascade cycle
  the chassis avoids on the audit table by design.
* ``valid_from`` -- ``timestamptz`` NOT NULL. PG-side ``now()``
  server default; the ORM also declares
  ``default=lambda: datetime.now(UTC)`` so SQLite dev/test paths
  populate the column without relying on the dialect.

Index discipline
----------------

Three indexes per table, all named so later migrations / operators
can reference them stably (the same discipline migrations ``0004``,
``0005``, ``0007`` follow):

* ``graph_node_history_tenant_node_valid_from_idx`` /
  ``graph_edge_history_tenant_edge_valid_from_idx`` -- composite
  b-tree on ``(tenant_id, node_id|edge_id, valid_from DESC)``. Drives
  the "history of this node / edge" query (verb T3): a tenant-scoped
  walk of a single resource's chronology, newest-first. The DESC
  ordering on ``valid_from`` lets PG's index-only scan return the
  latest revisions without a post-scan sort.
* ``graph_node_history_tenant_valid_from_idx`` /
  ``graph_edge_history_tenant_valid_from_idx`` -- composite b-tree on
  ``(tenant_id, valid_from DESC)``. Drives the tenant-wide timeline
  scan (verb T5 ``meho topology timeline``): "what changed in this
  tenant's graph between two timestamps" walks this index for the
  ``valid_from`` range bounds.
* ``graph_node_history_tenant_removed_idx`` /
  ``graph_edge_history_tenant_removed_idx`` -- **partial** b-tree on
  ``(tenant_id, valid_from DESC) WHERE change_kind = 'removed'``.
  Drives the tombstone-replay query (the "was this resource ever
  removed and re-discovered?" shape that ``meho topology history``
  surfaces). Without the partial, the same query would walk the full
  tenant timeline and filter at scan time; the partial index keeps
  only the tombstone rows (typically << 5% of the table volume on a
  healthy refresh cadence) and gives the query a single indexed scan.

Partial indexes are portable across PG and SQLite via the
``postgresql_where`` + ``sqlite_where`` keyword pair on
:func:`op.create_index`; the same pattern migration ``0005``
established for ``operation_group_global_idx`` /
``operation_group_tenant_idx``. SQLite has supported partial indexes
since 3.8.0 (2013); both dev/test (aiosqlite) and prod (PG 16+) are
above the floor.

DESC index ordering uses the ``sa.text("col DESC")`` shape rather
than a Python-side :class:`sqlalchemy.UnaryExpression` (``col.desc()``)
because the migration is operating on string column names rather than
:class:`sqlalchemy.Column` references -- the table is being created in
the same migration and we cannot reference a not-yet-created column
object. The text shape compiles identically against both dialects.

Reversibility contract
----------------------

``downgrade()`` undoes everything ``upgrade()`` created in reverse
order: drop the six indexes (three per table) -> drop the
``graph_edge_history`` table -> drop the ``graph_node_history``
table. Indexes are dropped explicitly so the reversal stays clean on
SQLite (which does not always cascade indexes on ``drop_table``) as
well as PG. There are no chassis-era rows in either table -- the
migration ships in v0.2 ahead of any history writes -- so the
downgrade is a clean substrate teardown with no backfill / cascade
trade-offs to defer.

The CI guard at ``scripts/ci/check_migration_compat.py`` inspects
only ``upgrade()`` paths; destructive ops in ``downgrade()`` are
permitted by design (the forward-compat property the guard enforces
is one-directional).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


#: Closed enum of ``change_kind`` values for both history tables.
#: Mirrored in :class:`meho_backplane.db.models.GraphHistoryChangeKind`;
#: the drift guard in :mod:`tests.test_topology_history_migration`
#: asserts the equality at unit-test time. Inlined here (rather than
#: imported) because migrations must be self-contained -- importing
#: from the ORM layer would break ``alembic upgrade`` against any
#: historical revision's model graph.
_CHANGE_KINDS: tuple[str, ...] = ("created", "updated", "removed")


def _check_in(column: str, values: tuple[str, ...]) -> str:
    """Render a ``column IN ('a', 'b', ...)`` clause for a CHECK constraint.

    Mirrors the helper in migrations ``0007`` and ``0010`` -- migrations
    must be self-contained, so the helper is inlined rather than
    imported from the ORM layer.
    """
    quoted = ", ".join(f"'{v}'" for v in values)
    return f"{column} IN ({quoted})"


def _create_history_table(
    *,
    table_name: str,
    ref_column: str,
    ref_table: str,
    is_postgres: bool,
) -> None:
    """Create one history table (``graph_node_history`` or ``graph_edge_history``).

    Factored to keep the two table-creation calls symmetric -- the
    only differences between the two history tables are the table
    name, the referenced live table, and the FK column name. Keeping
    the body shared makes the symmetry self-evident on read and
    prevents drift between the two tables on subsequent edits.
    """
    # Portable JSONB -> JSON variant; same pattern audit_log.payload /
    # graph_node.properties / graph_edge.properties use. PG gets
    # binary JSONB (GIN-friendly, indexable by ``@>``); SQLite gets
    # text JSON.
    snapshot_type = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")
    op.create_table(
        table_name,
        # BIGSERIAL on PG, autoincrementing INTEGER on SQLite -- see
        # module docstring for the dialect-portable BigInteger / Integer
        # variant rationale.
        sa.Column(
            "history_id",
            sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
            primary_key=True,
            autoincrement=True,
            nullable=False,
        ),
        # Real REFERENCES live-table FK with ON DELETE SET NULL --
        # history rows must survive the deletion of the live row they
        # reference. Nullable in the column signature so the SET NULL
        # transition compiles; the insert path always populates it.
        sa.Column(
            ref_column,
            sa.Uuid(),
            sa.ForeignKey(f"{ref_table}.id", ondelete="SET NULL"),
            nullable=True,
        ),
        # Real REFERENCES tenant(id) FK -- brand-new substrate, no
        # chassis-era rows, see module docstring.
        sa.Column(
            "tenant_id",
            sa.Uuid(),
            sa.ForeignKey("tenant.id"),
            nullable=False,
        ),
        sa.Column("change_kind", sa.Text(), nullable=False),
        sa.Column(
            "snapshot",
            snapshot_type,
            nullable=False,
            server_default=sa.text("'{}'::jsonb") if is_postgres else sa.text("'{}'"),
        ),
        # Soft-FK to audit_log.id -- same discipline as
        # audit_log.tenant_id / target_id / parent_audit_id, see module
        # docstring for the retention-coupling rationale.
        sa.Column("audit_id", sa.Uuid(), nullable=True),
        sa.Column(
            "valid_from",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()") if is_postgres else None,
        ),
        sa.CheckConstraint(
            _check_in("change_kind", _CHANGE_KINDS),
            name=f"ck_{table_name}_change_kind",
        ),
    )


def _create_history_indexes(
    *,
    table_name: str,
    ref_column: str,
) -> None:
    """Create the three indexes for a history table.

    Three indexes per history table (see module docstring "Index
    discipline" for the rationale):

    1. ``<table>_tenant_<ref>_valid_from_idx`` -- composite b-tree on
       ``(tenant_id, <ref_column>, valid_from DESC)``. Drives the
       per-resource history walk (T3).
    2. ``<table>_tenant_valid_from_idx`` -- composite b-tree on
       ``(tenant_id, valid_from DESC)``. Drives the tenant-wide
       timeline scan (T5).
    3. ``<table>_tenant_removed_idx`` -- **partial** b-tree on
       ``(tenant_id, valid_from DESC) WHERE change_kind = 'removed'``.
       Drives the tombstone-replay query.

    DESC ordering uses the ``sa.text("col DESC")`` form because we
    are operating on string column names (the table was created in
    the same migration; there is no :class:`sqlalchemy.Column`
    reference yet).
    """
    # Build short reference slug for index names: "node" / "edge".
    ref_slug = ref_column.removesuffix("_id")

    # Per-resource history walk -- (tenant_id, ref, valid_from DESC).
    op.create_index(
        f"{table_name}_tenant_{ref_slug}_valid_from_idx",
        table_name,
        ["tenant_id", ref_column, sa.text("valid_from DESC")],
        postgresql_using="btree",
    )

    # Tenant-wide timeline scan -- (tenant_id, valid_from DESC).
    op.create_index(
        f"{table_name}_tenant_valid_from_idx",
        table_name,
        ["tenant_id", sa.text("valid_from DESC")],
        postgresql_using="btree",
    )

    # Partial -- tombstone replay only. Portable WHERE clause via the
    # postgresql_where / sqlite_where keyword pair (see migration
    # 0005 for the precedent).
    op.create_index(
        f"{table_name}_tenant_removed_idx",
        table_name,
        ["tenant_id", sa.text("valid_from DESC")],
        postgresql_using="btree",
        postgresql_where=sa.text("change_kind = 'removed'"),
        sqlite_where=sa.text("change_kind = 'removed'"),
    )


def upgrade() -> None:
    """Create both history tables and their three indexes each."""
    bind = op.get_bind()
    is_postgres = bind.dialect.name == "postgresql"

    _create_history_table(
        table_name="graph_node_history",
        ref_column="node_id",
        ref_table="graph_node",
        is_postgres=is_postgres,
    )
    _create_history_indexes(
        table_name="graph_node_history",
        ref_column="node_id",
    )

    _create_history_table(
        table_name="graph_edge_history",
        ref_column="edge_id",
        ref_table="graph_edge",
        is_postgres=is_postgres,
    )
    _create_history_indexes(
        table_name="graph_edge_history",
        ref_column="edge_id",
    )


def downgrade() -> None:
    """Reverse the upgrade -- drop edge history first, then node history.

    Symmetric inverse of :func:`upgrade`. Each table's three indexes
    are dropped explicitly so the reversal stays clean on SQLite
    (which does not always cascade indexes on ``drop_table``) as well
    as PG. Edge history goes first so the dependency direction in the
    reversal matches what the dialects can mechanically tear down.
    """
    # ---------- graph_edge_history ----------
    op.drop_index(
        "graph_edge_history_tenant_removed_idx",
        table_name="graph_edge_history",
    )
    op.drop_index(
        "graph_edge_history_tenant_valid_from_idx",
        table_name="graph_edge_history",
    )
    op.drop_index(
        "graph_edge_history_tenant_edge_valid_from_idx",
        table_name="graph_edge_history",
    )
    op.drop_table("graph_edge_history")

    # ---------- graph_node_history ----------
    op.drop_index(
        "graph_node_history_tenant_removed_idx",
        table_name="graph_node_history",
    )
    op.drop_index(
        "graph_node_history_tenant_valid_from_idx",
        table_name="graph_node_history",
    )
    op.drop_index(
        "graph_node_history_tenant_node_valid_from_idx",
        table_name="graph_node_history",
    )
    op.drop_table("graph_node_history")
