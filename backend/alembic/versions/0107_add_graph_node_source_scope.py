# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Add ``graph_node.source_scope`` (expand step only) (#3739).

Revision ID: 0107
Revises: 0106
Create Date: 2026-10-17

Why
---

A topology node is keyed by ``(tenant_id, kind, name)``. Two targets can
report the same name for different things (two Kubernetes clusters both
have a ``default`` namespace), and today they collapse into one row. The
fix is to say *where* a name is unique. This migration is the first,
expand-only step. It adds the place to store that answer and changes
nothing else:

* ``graph_node.source_scope`` -- nullable text. Three forms:
  ``global``, ``target:<uuid>`` (the ``targets.id`` the name belongs to)
  and ``legacy:<uuid>`` (a pre-migration row kept apart; the UUID is the
  row's own ``graph_node.id``, so legacy rows never collide). ``NULL``
  means "not resolved yet".
* ``graph_node.source_scope_provenance`` -- nullable text. What set the
  scope. The backfill below writes ``migration:0107``.
* ``ck_graph_node_source_scope`` -- portable CHECK for the three forms
  (canonical lowercase UUIDs only). Mirrors ``_ck_source_scope`` in
  :mod:`meho_backplane.db.models`.
* ``graph_node_tenant_scope_kind_name_idx`` -- non-unique b-tree on
  ``(tenant_id, source_scope, kind, name)``.
* ``graph_node_scope_snapshot`` -- one row per ``graph_node`` that
  existed at upgrade time: its key, the ``target_id`` that last saw it,
  and whether the scope is known (``global``) or ``unresolved``. Keyed
  by the node id, so two rows with the same name never clash. The
  ``schema_version`` column (``1``) tells the later repair step which
  layout it is reading. The table is created only when it is absent,
  and a node that already has a row keeps it. So the row records the
  first time this migration saw the node, even across a downgrade and
  a later re-upgrade (see *Reversibility*).

What it does not do
-------------------

* It keeps ``graph_node_tenant_kind_name_idx`` (unique on
  ``(tenant_id, kind, name)``). PostgreSQL treats NULLs as distinct in a
  unique index, so a fourth nullable key column alone would let two
  unresolved rows share a name. The old key stays until the staged
  cut-over has given every row a scope.
* It does not guess a scope from ``target_id``. That column only names
  the last target that saw the row (refresh moves it between targets),
  so it is not evidence of where the name is unique.
* It does not write ``target:`` or ``legacy:`` values, split rows, or
  change any writer. New rows written before the cut-over stay ``NULL``.

Backfill
--------

Only rows whose scope is known without guessing get ``global``:
``kind = 'target'`` rows. Such a row is the registered target itself,
named by its target name, and live target names are unique per tenant
(``targets_tenant_name_idx``). Every other row stays ``NULL``.

Reversibility
-------------

``downgrade()`` keeps every ``graph_node`` row and drops the two columns,
the CHECK and the index. It refuses (``RuntimeError``) when
``source_scope`` holds anything this migration did not write itself -- a
``target:`` / ``legacy:`` value or a value from another writer. Such data
cannot be kept in the old schema; recover by rolling forward instead.
When the refusal does not fire, the dropped values are all ``global``
backfills that ``upgrade()`` re-derives.

``downgrade()`` keeps ``graph_node_scope_snapshot``. The snapshot is the
repair input, and it cannot be re-derived: a normal refresh moves
``graph_node.target_id`` to whichever target saw the row last, so a
snapshot re-taken after a downgrade would record the wrong target.
Keeping the table is how the repair metadata is exported before the
columns go. A later ``upgrade()`` finds the table, keeps every row in
it, and adds rows only for nodes that have none.

The migration is self-contained: the CHECK body is inlined, never
imported from :mod:`meho_backplane.db.models`.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0107"
down_revision: str | None = "0106"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_PROVENANCE = "migration:0107"
_SNAPSHOT_SCHEMA_VERSION = 1

#: Inlined copy of ``_ck_source_scope("source_scope")`` in
#: :mod:`meho_backplane.db.models`.
_CK_SOURCE_SCOPE = (
    "source_scope IS NULL "
    "OR source_scope = 'global' "
    "OR (substr(source_scope, 1, 7) IN ('target:', 'legacy:') "
    "AND source_scope LIKE '_______________-____-____-____-____________' "
    "AND length(replace(source_scope, '-', '')) = 39 "
    "AND ltrim(substr(source_scope, 8), '0123456789abcdef-') = '')"
)


def upgrade() -> None:
    """Add the scope columns, CHECK, index and snapshot; backfill ``global``.

    The snapshot is additive. A table left behind by an earlier
    ``downgrade()`` is kept, and only nodes without a row get one.
    """
    op.add_column("graph_node", sa.Column("source_scope", sa.Text(), nullable=True))
    op.add_column("graph_node", sa.Column("source_scope_provenance", sa.Text(), nullable=True))
    with op.batch_alter_table("graph_node") as batch_op:
        batch_op.create_check_constraint("ck_graph_node_source_scope", _CK_SOURCE_SCOPE)
    op.create_index(
        "graph_node_tenant_scope_kind_name_idx",
        "graph_node",
        ["tenant_id", "source_scope", "kind", "name"],
        unique=False,
        postgresql_using="btree",
    )

    op.execute(
        sa.text(
            "UPDATE graph_node SET source_scope = 'global', "
            "source_scope_provenance = :provenance "
            "WHERE kind = 'target' AND source_scope IS NULL"
        ).bindparams(provenance=_PROVENANCE)
    )

    if not sa.inspect(op.get_bind()).has_table("graph_node_scope_snapshot"):
        op.create_table(
            "graph_node_scope_snapshot",
            sa.Column("node_id", sa.Uuid(), primary_key=True, nullable=False),
            sa.Column("tenant_id", sa.Uuid(), nullable=False),
            sa.Column("kind", sa.Text(), nullable=False),
            sa.Column("name", sa.Text(), nullable=False),
            sa.Column("last_target_id", sa.Uuid(), nullable=True),
            sa.Column("source", sa.Text(), nullable=False),
            sa.Column("discovered_by", sa.Text(), nullable=False),
            sa.Column("scope_status", sa.Text(), nullable=False),
            sa.Column("schema_version", sa.Integer(), nullable=False),
            sa.Column("captured_at", sa.DateTime(timezone=True), nullable=False),
            sa.CheckConstraint(
                "scope_status IN ('global', 'unresolved')",
                name="ck_graph_node_scope_snapshot_status",
            ),
        )
    op.execute(
        sa.text(
            "INSERT INTO graph_node_scope_snapshot "
            "(node_id, tenant_id, kind, name, last_target_id, source, "
            "discovered_by, scope_status, schema_version, captured_at) "
            "SELECT n.id, n.tenant_id, n.kind, n.name, n.target_id, n.source, "
            "n.discovered_by, "
            "CASE WHEN n.source_scope = 'global' THEN 'global' ELSE 'unresolved' END, "
            ":schema_version, CURRENT_TIMESTAMP "
            "FROM graph_node AS n "
            "WHERE NOT EXISTS (SELECT 1 FROM graph_node_scope_snapshot AS s "
            "WHERE s.node_id = n.id)"
        ).bindparams(schema_version=_SNAPSHOT_SCHEMA_VERSION)
    )


def downgrade() -> None:
    """Drop the columns, CHECK and index; keep the snapshot; refuse on scoped data.

    The snapshot stays because it is the repair input and cannot be
    re-taken later: refreshes move ``target_id`` after the upgrade.
    """
    bind = op.get_bind()
    foreign = bind.execute(
        sa.text(
            "SELECT count(*) FROM graph_node "
            "WHERE source_scope IS NOT NULL "
            "AND (source_scope <> 'global' "
            "OR COALESCE(source_scope_provenance, '') <> :provenance)"
        ).bindparams(provenance=_PROVENANCE)
    ).scalar_one()
    if foreign:
        raise RuntimeError(
            f"Refusing to downgrade 0107: {foreign} graph_node row(s) carry a "
            "source_scope this migration did not write. The old schema cannot "
            "keep them. Roll forward instead of downgrading."
        )

    op.drop_index("graph_node_tenant_scope_kind_name_idx", table_name="graph_node")
    with op.batch_alter_table("graph_node") as batch_op:
        batch_op.drop_constraint("ck_graph_node_source_scope", type_="check")
        batch_op.drop_column("source_scope_provenance")
        batch_op.drop_column("source_scope")
