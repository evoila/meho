# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Add ``graph_node.source`` + backfill curated seeds (#2536).

Revision ID: 0064
Revises: 0063
Create Date: 2026-07-16

Initiative #2533 (Topology v2), Task #2536 (T2, the second and final
migration of the Initiative). Curated *edges* have carried a
``source`` column with ``ck_graph_edge_source`` since migration
``0007``, and the refresh service uses it to protect operator-owned
rows from probe overwrites. Curated *nodes* had no equivalent column:
:func:`meho_backplane.topology.refresh._update_existing_node`
overwrote ``properties`` wholesale and adopted the row onto the
refreshing target whenever any probe snapshot re-asserted a matching
``(kind, name)``, and the adopting target's later refreshes could then
soft-delete the row. This migration adds the discriminator the node
half of that discipline needs::

    source TEXT NOT NULL DEFAULT 'auto'
    CHECK (source IN ('auto', 'curated'))   -- ck_graph_node_source

mirroring ``ck_graph_edge_source`` (migration ``0007``).

Backfill
--------

Pre-existing manually-seeded rows are recognisable by the
``properties.seeded_by`` stamp
(:func:`meho_backplane.topology.nodes.create_or_get_node` writes
``seeded_by=operator.sub`` on every manual seed); those rows backfill
to ``source='curated'``. Every other row (probe-discovered) keeps the
column default ``'auto'``. The key-existence probe is dialect-branched:
PostgreSQL uses the JSONB ``?`` operator, the SQLite unit-test driver
uses ``json_extract(...) IS NOT NULL`` (``seeded_by`` is always a
non-null JWT ``sub`` string when present, so the two predicates agree
on real data).

SQLite batch-mode CHECK caveat
------------------------------

Adding the CHECK constraint uses :func:`op.batch_alter_table` (SQLite
has no ``ALTER TABLE ... ADD CONSTRAINT``); the rebuild preserves the
sibling named CHECK ``ck_graph_node_kind`` per the Alembic batch
documentation (named CHECK constraints participate in the recreate —
the same behaviour migration ``0063`` pinned for ``graph_edge``'s
sibling CHECK). The plain ``op.add_column`` before it is portable as-is
because SQLite allows ``ADD COLUMN`` with a NOT NULL constraint when a
constant non-NULL default is supplied.

Reversibility contract
----------------------

``downgrade()`` drops the CHECK and the column. The auto/curated
distinction is lost (rows survive; the ``properties.seeded_by``
convention remains recoverable), which restores the pre-0064 state
exactly — no pre-check is needed because no row can violate the
narrowed schema.

The migration is self-contained: the source vocabulary is inlined
(never imported from :mod:`meho_backplane.db.models`) because Alembic
must run against any historical revision's model graph.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0064"
down_revision: str | None = "0063"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


#: The closed source vocabulary — mirrors ``_GRAPH_SOURCES`` in
#: :mod:`meho_backplane.db.models` and the values ``ck_graph_edge_source``
#: has enforced since migration ``0007``; inlined per the
#: self-containment rule.
_GRAPH_NODE_SOURCES: tuple[str, ...] = ("auto", "curated")


def _check_in(column: str, values: tuple[str, ...]) -> str:
    """Render a ``column IN ('a', 'b', ...)`` clause for a CHECK constraint.

    Mirrors the helper in ``0007`` / ``0063`` — migrations are
    self-contained, so the helper is inlined rather than imported.
    """
    quoted = ", ".join(f"'{v}'" for v in values)
    return f"{column} IN ({quoted})"


def upgrade() -> None:
    """Add ``graph_node.source``, backfill curated seeds, add the CHECK.

    Three steps, in order:

    1. ``ADD COLUMN source TEXT NOT NULL DEFAULT 'auto'`` — the
       constant server default stamps every pre-existing row ``'auto'``
       (and keeps out-of-band inserts valid), so no NOT NULL scrub is
       needed.
    2. Backfill ``source='curated'`` where ``properties`` carries the
       manual-seed ``seeded_by`` stamp.
    3. Create ``ck_graph_node_source`` (batch mode for the SQLite
       table rebuild; PostgreSQL runs a native ``ADD CONSTRAINT``).
    """
    op.add_column(
        "graph_node",
        sa.Column(
            "source",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'auto'"),
        ),
    )

    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        # JSONB key-existence operator — matches the issue contract
        # (`properties ? 'seeded_by'`) exactly.
        op.execute(
            sa.text("UPDATE graph_node SET source = 'curated' WHERE properties ? 'seeded_by'")
        )
    else:
        # SQLite stores the portable JSON column as TEXT;
        # ``json_extract`` returns NULL for a missing key. ``seeded_by``
        # is always a non-null string when present (the operator's JWT
        # ``sub``), so this predicate matches the PG one on real data.
        op.execute(
            sa.text(
                "UPDATE graph_node SET source = 'curated' "
                "WHERE json_extract(properties, '$.seeded_by') IS NOT NULL"
            )
        )

    with op.batch_alter_table("graph_node") as batch_op:
        batch_op.create_check_constraint(
            "ck_graph_node_source",
            _check_in("source", _GRAPH_NODE_SOURCES),
        )


def downgrade() -> None:
    """Drop ``ck_graph_node_source`` and the ``source`` column.

    Loses the auto/curated distinction (the ``properties.seeded_by``
    convention remains as the recoverable signal); rows are untouched
    otherwise. Batch mode for the SQLite table rebuild — the sibling
    named CHECK ``ck_graph_node_kind`` survives the recreate.
    """
    with op.batch_alter_table("graph_node") as batch_op:
        batch_op.drop_constraint("ck_graph_node_source", type_="check")
        batch_op.drop_column("source")
