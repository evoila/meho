# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Open the graph node / edge kind vocabularies (#2534).

Revision ID: 0063
Revises: 0062
Create Date: 2026-07-16

Initiative #2533 (Topology v2), Task #2534 (T1, the keystone). The
closed kind vocabularies -- ``ck_graph_node_kind``'s 14-member IN-list
(migration ``0007``) and ``ck_graph_edge_kind``'s 10-member IN-list
(migration ``0010``) -- are replaced with one portable **minimal shape
CHECK** per table::

    length(kind) >= 2 AND length(kind) <= 63 AND kind = lower(kind)

Rationale (reverses #364's vocabulary lock and #593): the lock existed
so a v0.2.next policy engine parsing ``kind`` would keep a portable
grammar across tenants. That consumer never shipped and nothing tracks
it, while the closed set makes routine cross-system traces
(``dns-record``, ``database``, ``certificate``, ``resolves-to``,
``same-as``) unrepresentable without a migration each. Post-0063 the
kind space is open: full slug validation (pattern
``^[a-z0-9]+(?:[._-][a-z0-9]+)*$``, 2--63 chars) runs Python-side at
every write boundary (:mod:`meho_backplane.topology.nodes`,
:mod:`meho_backplane.topology.annotate`, the REST body models, the MCP
inputSchemas), and the old members survive as the *documented
well-known set* (``WELL_KNOWN_NODE_KINDS`` / ``GraphEdgeKind`` in
:mod:`meho_backplane.db.models`). The shape CHECK is deliberately
weaker than the slug pattern because regex CHECKs are not portable
across PostgreSQL 16 and the SQLite unit suite; it exists as the
DB-layer backstop against out-of-band inserts landing
obviously-malformed kinds.

Backward compatibility is automatic on upgrade: all 14 node kinds and
all 10 edge kinds are lowercase slugs of length 2--17, so every
pre-migration row satisfies the widened constraint and no backfill or
scrub is needed.

SQLite batch-mode CHECK caveat
------------------------------

``graph_edge`` carries a second CHECK, ``ck_graph_edge_source``, that
this migration must not disturb. Under the SQLite dialect
:func:`op.batch_alter_table` rebuilds the table (copy rows -> new
schema -> rename); per the Alembic batch documentation
(https://alembic.sqlalchemy.org/en/latest/batch.html#working-with-constraints)
**named** CHECK constraints participate in batch mode like any other
constraint -- only *unnamed* constraints are silently omitted from the
recreate. Both graph CHECKs are named (``ck_...``), so the plain
drop-and-recreate mold from migration ``0010`` is sufficient and no
``table_args`` re-declaration is needed. The survival of
``ck_graph_edge_source`` across this rebuild is pinned by this
migration's behavioural test
(:mod:`tests.migrations.test_migration_0063_open_graph_kind_vocabularies`)
and by :func:`tests.test_topology_schema.test_graph_edge_source_check_constraint_rejects_unknown`,
which runs against a head-migrated DB.

Reversibility contract
----------------------

``downgrade()`` narrows both constraints back to their closed IN-lists.
Mirroring migration ``0010``'s downgrade discipline, it first counts
rows whose ``kind`` falls outside the post-downgrade vocabulary and
raises :class:`RuntimeError` naming the offending kinds and row counts
before any DDL runs -- a clear operator-facing refusal instead of an
opaque mid-DDL ``IntegrityError``.

The migration is self-contained: the closed tuples are inlined verbatim
(never imported from :mod:`meho_backplane.db.models`) because Alembic
must run against any historical revision's model graph.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0063"
down_revision: str | None = "0062"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


#: The closed 14-kind node vocabulary migration ``0007`` shipped --
#: what ``downgrade()`` narrows ``ck_graph_node_kind`` back to.
_NODE_KINDS_CLOSED: tuple[str, ...] = (
    "target",
    "vm",
    "host",
    "network",
    "datastore",
    "namespace",
    "pod",
    "service",
    "ingress",
    "node",
    "principal",
    "vault-role",
    "vault-mount",
    "volume",
)

#: The closed 10-kind edge vocabulary migration ``0010`` shipped --
#: what ``downgrade()`` narrows ``ck_graph_edge_kind`` back to.
_EDGE_KINDS_CLOSED: tuple[str, ...] = (
    "runs-on",
    "mounts",
    "routes-through",
    "belongs-to",
    "authenticates-via",
    "depends-on",
    "replicates-to",
    "backed-up-by",
    "routes-via",
    "policy-binds",
)

#: Kind length bounds for the shape CHECK. Mirror
#: ``KIND_SLUG_MIN_LENGTH`` / ``KIND_SLUG_MAX_LENGTH`` in
#: :mod:`meho_backplane.db.models`; inlined per the self-containment
#: rule.
_KIND_MIN_LENGTH = 2
_KIND_MAX_LENGTH = 63


def _check_in(column: str, values: tuple[str, ...]) -> str:
    """Render a ``column IN ('a', 'b', ...)`` clause for a CHECK constraint.

    Mirrors the helper in ``0007`` / ``0010`` -- migrations are
    self-contained, so the helper is inlined rather than imported.
    """
    quoted = ", ".join(f"'{v}'" for v in values)
    return f"{column} IN ({quoted})"


def _check_kind_shape(column: str) -> str:
    """Render the portable minimal shape CHECK for an open kind column.

    ``length()`` and ``lower()`` are portable across PostgreSQL 16 and
    SQLite; a regex CHECK (PG ``~``) is not. Mirrors
    ``_ck_kind_shape`` in :mod:`meho_backplane.db.models`.
    """
    return (
        f"length({column}) >= {_KIND_MIN_LENGTH} "
        f"AND length({column}) <= {_KIND_MAX_LENGTH} "
        f"AND {column} = lower({column})"
    )


def upgrade() -> None:
    """Replace both closed IN-list kind CHECKs with the minimal shape CHECK.

    Wrapped in :func:`op.batch_alter_table` for SQLite portability
    (same mold as migration ``0010``): SQLite has no
    ``ALTER TABLE ... DROP CONSTRAINT`` DDL, so Alembic batch mode
    rebuilds each table under the SQLite dialect while PostgreSQL runs
    the equivalent native ``ALTER TABLE`` statements. Every
    pre-migration row's ``kind`` is a lowercase slug of length 2--17,
    so no row violates the widened constraint.
    """
    with op.batch_alter_table("graph_node") as batch_op:
        batch_op.drop_constraint("ck_graph_node_kind", type_="check")
        batch_op.create_check_constraint(
            "ck_graph_node_kind",
            _check_kind_shape("kind"),
        )

    with op.batch_alter_table("graph_edge") as batch_op:
        batch_op.drop_constraint("ck_graph_edge_kind", type_="check")
        batch_op.create_check_constraint(
            "ck_graph_edge_kind",
            _check_kind_shape("kind"),
        )


def _refuse_if_rows_outside(table: str, closed_kinds: tuple[str, ...]) -> None:
    """Raise :class:`RuntimeError` when *table* has rows outside *closed_kinds*.

    The downgrade pre-check (migration ``0010``'s mold): narrowing a
    CHECK while non-member rows exist would orphan them on next write
    or crash opaquely mid-DDL. Counting first turns that into an
    actionable "remove or re-classify these rows" message. Core
    ``sa.table`` / ``sa.column`` keep the query parameterised without
    reflecting the ORM model.
    """
    tbl = sa.table(table, sa.column("kind", sa.Text()))
    count_col = sa.func.count().label("n")
    blocking_stmt = (
        sa.select(tbl.c.kind, count_col).where(tbl.c.kind.not_in(closed_kinds)).group_by(tbl.c.kind)
    )

    bind = op.get_bind()
    blocking_rows = bind.execute(blocking_stmt).all()

    if blocking_rows:
        total = sum(row.n for row in blocking_rows)
        details = ", ".join(f"{row.kind}={row.n}" for row in blocking_rows)
        raise RuntimeError(
            f"Cannot downgrade migration 0063: {table} contains "
            f"{total} row(s) with kind(s) outside the closed vocabulary "
            f"({details}) that would be orphaned by the narrowed CHECK "
            "constraint. Remove or re-classify them before running "
            "`alembic downgrade`."
        )


def downgrade() -> None:
    """Narrow both kind CHECKs back to the closed IN-list vocabularies.

    Refuses loudly (per-table pre-check) when rows with novel kinds
    exist -- see :func:`_refuse_if_rows_outside`. Both pre-checks run
    before any DDL so a refusal leaves the schema untouched.
    """
    _refuse_if_rows_outside("graph_node", _NODE_KINDS_CLOSED)
    _refuse_if_rows_outside("graph_edge", _EDGE_KINDS_CLOSED)

    with op.batch_alter_table("graph_node") as batch_op:
        batch_op.drop_constraint("ck_graph_node_kind", type_="check")
        batch_op.create_check_constraint(
            "ck_graph_node_kind",
            _check_in("kind", _NODE_KINDS_CLOSED),
        )

    with op.batch_alter_table("graph_edge") as batch_op:
        batch_op.drop_constraint("ck_graph_edge_kind", type_="check")
        batch_op.create_check_constraint(
            "ck_graph_edge_kind",
            _check_in("kind", _EDGE_KINDS_CLOSED),
        )
