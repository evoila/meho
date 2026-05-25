# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Create the topology graph (``graph_node`` + ``graph_edge``) tables.

Revision ID: 0007
Revises: 0006
Create Date: 2026-05-14

This migration is the schema foundation of Initiative #363 (G9.1 Graph
schema + auto-discovery + 3 verbs), Task #448 (T1). It adds the two
structural pieces every subsequent G9.1 task -- refresh service (T3),
recursive-CTE traversal helpers (T4), API + CLI + MCP fronts (T5--T7),
and the cross-tenant integration acceptance (T8) -- reads or writes
against:

* ``graph_node`` -- the per-tenant inventory of every object an agent
  may need to traverse from or to: targets, VMs, hosts, networks,
  datastores, namespaces, pods, services, ingresses, principals, vault
  mounts, vault roles, volumes. A node optionally ``REFERENCES`` a
  ``targets.id`` when the node *is* a target (vCenter, k8s cluster,
  vault server); for inner-graph nodes (a VM, a pod, a datastore) the
  column stays NULL.
* ``graph_edge`` -- a directed adjacency-list edge between two
  ``graph_node`` rows. Carries the per-edge ``kind`` (the relationship
  vocabulary) and ``source`` (auto-discovered vs. operator-curated).
  Adjacency-list shape is what lets PG 16's ``WITH RECURSIVE ... CYCLE``
  clause (§7.8.2.2 of the PG manual; identical in PG 16, which is the
  chassis floor) walk dependents / dependencies / paths without a graph
  extension. T4 builds those queries; this migration ships only the
  storage.

T1 ships **the tables and their indexes only**. Population is
deliberately out of scope:

* Insert / update / soft-delete -- T3's refresh service (#450)
  territory; calls each connector's ``discover_topology`` and diffs
  against existing ``(tenant_id, target_id)`` rows.
* Traversal reads -- T4's ``service.py`` + ``graph.py`` (#451);
  composes the recursive CTE with the ``CYCLE`` clause.

Why real foreign keys here (and not the soft-FK discipline)
-----------------------------------------------------------

Migrations 0002 / 0004 / 0006 deliberately omit FK clauses on
``audit_log.tenant_id`` / ``audit_log.target_id`` /
``audit_log.parent_audit_id`` because those columns were added to a
populated table whose chassis-era rows have no real parent to point at;
adding the FK would require coordinating a backfill, a NOT VALID +
VALIDATE CONSTRAINT cycle, and a separate tightening migration.

The topology graph is a clean-slate substrate. There are no chassis-era
``graph_node`` / ``graph_edge`` rows; the downgrade drops the whole pair
of tables; there is no backfill or cascade decision to defer. Mirroring
the discipline ``documents.tenant_id`` already established (see 0003),
every FK is enforced at the DB layer:

* ``graph_node.tenant_id`` ``REFERENCES tenant(id)`` -- NOT NULL.
  Tenant deletion is a major operation that must clear the tenant's
  graph first; default NO ACTION blocks the cascade.
* ``graph_node.target_id`` ``REFERENCES targets(id) ON DELETE SET NULL``
  -- nullable. When the operator removes a registered target, the
  topology node (a VM that *was* a registered target, say) should
  outlive the target row in a "no longer a target" form rather than
  cascade-deleting all the topology data the agent may still want to
  reason about. SET NULL is the safe forward-compat shape.
* ``graph_edge.tenant_id`` ``REFERENCES tenant(id)`` -- NOT NULL,
  same rationale as the node-side.
* ``graph_edge.from_node_id`` and ``graph_edge.to_node_id`` both
  ``REFERENCES graph_node(id) ON DELETE CASCADE``. Hard-deleting a node
  hard-deletes its edges. Refresh-driven *soft-deletes* set
  ``last_seen=NULL`` on the node and leave the edges alone, so cascade
  is invisible during normal operation; the cascade exists for the rare
  hard-delete (tenant purge, test cleanup, operator forced clear).
  Without cascade, hard-delete of a node would error on dependent edges
  -- exactly the wrong UX for what's already a rare administrative op.

Closed-enum CHECK constraints -- portable enforcement
-----------------------------------------------------

``graph_node.kind``, ``graph_edge.kind`` and ``graph_edge.source`` are
bounded enums. PostgreSQL ``ENUM`` types are dialect-specific and force
migrations to manage a named type lifecycle (``CREATE TYPE`` /
``ALTER TYPE ADD VALUE`` / ``DROP TYPE``) that SQLite cannot mirror. The
portable alternative -- already used by 0004 (``ck_targets_auth_model``)
and 0005 (``ck_operation_group_review_status`` /
``ck_endpoint_descriptor_source_kind`` /
``ck_endpoint_descriptor_safety_level``) -- is ``TEXT NOT NULL`` with a
``CHECK (column IN (...))`` constraint. Identical enforcement on both
dialects, no named type to track, no ``ALTER TYPE ADD VALUE`` friction
when the v0.2 enum widens into v0.2.next.

Enum membership:

* ``graph_node.kind`` -- ``target``, ``vm``, ``host``, ``network``,
  ``datastore``, ``namespace``, ``pod``, ``service``, ``ingress``,
  ``node``, ``principal``, ``vault-role``, ``vault-mount``, ``volume``.
* ``graph_edge.kind`` -- the v0.2 auto-discoverable subset:
  ``runs-on``, ``mounts``, ``routes-through``, ``belongs-to``. The
  wider operator-curated vocabulary (``authenticates-via``,
  ``depends-on``, ``replicates-to``, ``backed-up-by``) lands in G9.2
  via a follow-up migration that widens the IN(...) list.
* ``graph_edge.source`` -- ``auto`` (probe-derived, T3) or ``curated``
  (operator-asserted, G9.2). v0.2 writes ``auto`` exclusively.

Index discipline
----------------

* ``graph_node_tenant_kind_name_idx`` -- unique b-tree on
  ``(tenant_id, kind, name)``. Enforces the "one (kind, name) per
  tenant" invariant; names are case-sensitive within a tenant+kind.
  Uniqueness is on the named index, not the column triple, so PG does
  not auto-create a duplicate anonymous index (same discipline as
  ``targets_tenant_name_idx`` in 0004).
* ``graph_edge_tenant_endpoints_kind_idx`` -- unique b-tree on
  ``(tenant_id, from_node_id, to_node_id, kind)``. At most one edge of
  a given ``kind`` between a pair of nodes within a tenant. A v0.3
  multi-edge model can replace this with a partial unique if the use
  case ever materialises; v0.2 keeps it strict.
* ``graph_edge_tenant_from_idx`` -- b-tree on
  ``(tenant_id, from_node_id)``. Drives the *dependencies* (forward)
  recursive-CTE traversal in T4.
* ``graph_edge_tenant_to_idx`` -- b-tree on ``(tenant_id, to_node_id)``.
  Drives the *dependents* (reverse) recursive-CTE traversal in T4.

Dialect-portability decisions
-----------------------------

Mirrors the discipline 0001 through 0006 established. The migration
runs cleanly against both PostgreSQL (production, pgvector image) and
SQLite (dev/test via aiosqlite).

* ``id`` server default -- ``gen_random_uuid()`` on PG (built-in since
  PG 13, same assumption every prior migration makes); SQLite leaves it
  to the ORM ``default=uuid.uuid4``.
* ``first_seen`` server default -- ``now()`` on PG; SQLite leaves it to
  the ORM ``default=lambda: datetime.now(UTC)``. ``last_seen`` is
  nullable with no default on either dialect -- the refresh service
  (T3) populates it on every observation, and *sets it back to NULL*
  once a node has been absent past the configured threshold (the
  spec's soft-delete signal, kept queryable by the G9.3 history surface
  and still reachable by the traversal verbs, though excluded by
  default from the list verbs ``list_nodes`` / ``list_edges``).
* ``properties`` -- portable JSON -> JSONB via
  :func:`sqlalchemy.JSON.with_variant`. Server default ``'{}'::jsonb``
  on PG matches the ORM ``default=dict`` so out-of-band PG inserts
  that omit the column still satisfy NOT NULL.

FK ordering in upgrade / downgrade
----------------------------------

``upgrade()`` creates ``graph_node`` before ``graph_edge`` so the
edge-side ``REFERENCES graph_node(id)`` resolves at table-create time.
``downgrade()`` drops the edge indexes and the edge table first, then
the node indexes and the node table -- the inverse order of dependence,
so SQLite (which does not auto-cascade FK drops on ``DROP TABLE``) can
still tear everything down cleanly.

Reversibility contract
----------------------

``downgrade()`` undoes everything ``upgrade()`` created in reverse
order: edge indexes -> edge table -> node indexes -> node table.
Explicit index drops keep the inverse symmetric on SQLite (which does
not always cascade indexes on ``drop_table``) as well as PostgreSQL.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


#: Closed enum of ``graph_node.kind`` values. Listed once here so the
#: CHECK constraint, the documentation, and any future widening
#: migration share a single source of truth at the migration layer.
_NODE_KINDS: tuple[str, ...] = (
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

#: Closed enum of ``graph_edge.kind`` -- v0.2 auto-discoverable subset.
#: G9.2 will ship a follow-up migration that widens this list with the
#: operator-curated cross-system vocabulary.
_EDGE_KINDS: tuple[str, ...] = (
    "runs-on",
    "mounts",
    "routes-through",
    "belongs-to",
)

#: Closed enum of ``graph_edge.source`` -- v0.2 writes ``auto``
#: exclusively; ``curated`` is the G9.2 hook for operator-asserted
#: edges.
_EDGE_SOURCES: tuple[str, ...] = ("auto", "curated")


def _check_in(column: str, values: tuple[str, ...]) -> str:
    """Render a ``column IN ('a', 'b', ...)`` clause for a CHECK constraint."""
    quoted = ", ".join(f"'{v}'" for v in values)
    return f"{column} IN ({quoted})"


def upgrade() -> None:
    """Create ``graph_node`` and ``graph_edge`` tables + indexes."""
    bind = op.get_bind()
    is_postgres = bind.dialect.name == "postgresql"

    # Portable JSONB -> JSON variant; same pattern audit_log.payload /
    # documents.metadata / endpoint_descriptor.tags use. PG gets binary
    # JSONB (GIN-friendly, indexable by ``@>``); SQLite gets text JSON.
    properties_type = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")

    # ---------- graph_node ----------
    op.create_table(
        "graph_node",
        sa.Column(
            "id",
            sa.Uuid(),
            primary_key=True,
            nullable=False,
            server_default=sa.text("gen_random_uuid()") if is_postgres else None,
        ),
        # Real FK -- no chassis-era rows, see module docstring.
        sa.Column(
            "tenant_id",
            sa.Uuid(),
            sa.ForeignKey("tenant.id"),
            nullable=False,
        ),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        # Nullable: graph_node may exist *without* being a registered
        # target (a VM, a pod, a datastore). Real FK with ON DELETE
        # SET NULL so target removal does not cascade-delete the
        # topology -- the node lives on as a non-target row.
        sa.Column(
            "target_id",
            sa.Uuid(),
            sa.ForeignKey("targets.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "properties",
            properties_type,
            nullable=False,
            server_default=sa.text("'{}'::jsonb") if is_postgres else sa.text("'{}'"),
        ),
        sa.Column("discovered_by", sa.Text(), nullable=False),
        sa.Column(
            "first_seen",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()") if is_postgres else None,
        ),
        # Nullable -- NULL is the soft-delete signal. Refresh writes a
        # timestamp on every observation and resets it to NULL once a
        # node has been absent past the absence threshold (T3).
        sa.Column(
            "last_seen",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        # Closed enum -- portable IN(...) CHECK. v0.2 vocabulary;
        # extending requires a widening migration so the type and the
        # check move in lock-step.
        sa.CheckConstraint(
            _check_in("kind", _NODE_KINDS),
            name="ck_graph_node_kind",
        ),
    )

    # Unique b-tree on (tenant_id, kind, name) -- "one (kind, name) per
    # tenant" invariant. Uniqueness on the named index, not the column
    # triple, so PG does not auto-create a duplicate anonymous index.
    op.create_index(
        "graph_node_tenant_kind_name_idx",
        "graph_node",
        ["tenant_id", "kind", "name"],
        unique=True,
        postgresql_using="btree",
    )

    # ---------- graph_edge ----------
    op.create_table(
        "graph_edge",
        sa.Column(
            "id",
            sa.Uuid(),
            primary_key=True,
            nullable=False,
            server_default=sa.text("gen_random_uuid()") if is_postgres else None,
        ),
        sa.Column(
            "tenant_id",
            sa.Uuid(),
            sa.ForeignKey("tenant.id"),
            nullable=False,
        ),
        # Hard-delete a node -> hard-delete its edges. Refresh-driven
        # soft-deletes set graph_node.last_seen=NULL and leave the
        # edges alone, so cascade is invisible during normal operation;
        # it exists for tenant purges, operator forced clears, and the
        # test cleanup path.
        sa.Column(
            "from_node_id",
            sa.Uuid(),
            sa.ForeignKey("graph_node.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "to_node_id",
            sa.Uuid(),
            sa.ForeignKey("graph_node.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column(
            "properties",
            properties_type,
            nullable=False,
            server_default=sa.text("'{}'::jsonb") if is_postgres else sa.text("'{}'"),
        ),
        sa.Column("discovered_by", sa.Text(), nullable=False),
        sa.Column(
            "first_seen",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()") if is_postgres else None,
        ),
        sa.Column(
            "last_seen",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.CheckConstraint(
            _check_in("kind", _EDGE_KINDS),
            name="ck_graph_edge_kind",
        ),
        sa.CheckConstraint(
            _check_in("source", _EDGE_SOURCES),
            name="ck_graph_edge_source",
        ),
    )

    # Unique b-tree on (tenant_id, from_node_id, to_node_id, kind) --
    # at most one edge of a given kind between a pair within a tenant.
    op.create_index(
        "graph_edge_tenant_endpoints_kind_idx",
        "graph_edge",
        ["tenant_id", "from_node_id", "to_node_id", "kind"],
        unique=True,
        postgresql_using="btree",
    )
    # Forward (dependencies) traversal index.
    op.create_index(
        "graph_edge_tenant_from_idx",
        "graph_edge",
        ["tenant_id", "from_node_id"],
        postgresql_using="btree",
    )
    # Reverse (dependents) traversal index.
    op.create_index(
        "graph_edge_tenant_to_idx",
        "graph_edge",
        ["tenant_id", "to_node_id"],
        postgresql_using="btree",
    )


def downgrade() -> None:
    """Reverse the upgrade -- drop edges, then nodes, in dependency order."""
    # Edge indexes + table first (children before parents).
    op.drop_index("graph_edge_tenant_to_idx", table_name="graph_edge")
    op.drop_index("graph_edge_tenant_from_idx", table_name="graph_edge")
    op.drop_index("graph_edge_tenant_endpoints_kind_idx", table_name="graph_edge")
    op.drop_table("graph_edge")

    # Node index + table.
    op.drop_index("graph_node_tenant_kind_name_idx", table_name="graph_node")
    op.drop_table("graph_node")
