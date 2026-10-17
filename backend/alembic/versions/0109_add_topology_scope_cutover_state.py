# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Add the state and manifest tables for the topology scope cut-over (#3749).

Revision ID: 0109
Revises: 0108
Create Date: 2026-10-17

Why
---

Migration ``0108`` added ``graph_node.source_scope`` but kept the old
unique key ``(tenant_id, kind, name)``. While that key exists, two
targets cannot each own a ``default`` namespace. The fix needs fresh
evidence from every target, so it cannot run inside a migration. It is
an operator step instead (``python -m meho_backplane.topology.scope_repair``):

1. **Stage.** Ask every target what it sees, without writing to the
   graph. Store the result as a *manifest*: which rows keep their id,
   which new rows appear, which old rows are set aside, and why.
2. **Cut over.** In one PostgreSQL transaction: check that the graph has
   not changed since staging, drop the old unique key, write the staged
   rows, make ``source_scope`` required, add the new unique key
   ``(tenant_id, source_scope, kind, name)`` and switch the writers.

This migration only adds the two small tables that step needs. It does
not touch ``graph_node``.

* ``topology_identity_state`` -- exactly one row (``id = 1``). ``mode``
  is ``legacy`` (old key, writers leave ``source_scope`` empty) or
  ``scoped`` (new key, writers set ``source_scope``). The row starts as
  ``legacy``. Writers read it with a shared row lock, and the cut-over
  takes an update lock on it first, so no writer runs half in one mode
  and half in the other.
* ``topology_scope_manifest`` -- one row per staging run: ``status``
  (``staged``, ``incomplete``, ``cut_over``, ``refused``), the graph
  ``snapshot_version`` it was planned against, and the plan in ``body``.
  The plan holds keys, scopes, row ids and reasons. It holds no node or
  edge properties.

Reversibility
-------------

``downgrade()`` drops both tables. It refuses (``RuntimeError``) once the
cut-over has run (``mode = 'scoped'``): the graph then holds rows that
only differ by scope, and the old unique key cannot come back. Recover
by rolling forward. ``0108``'s own downgrade refuses for the same reason.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0109"
down_revision: str | None = "0108"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_STATE = "topology_identity_state"
_MANIFEST = "topology_scope_manifest"


def upgrade() -> None:
    """Create the state table (seeded ``legacy``) and the manifest table."""
    op.create_table(
        _STATE,
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=False),
        sa.Column("mode", sa.Text(), nullable=False),
        sa.Column("manifest_id", sa.Uuid(), nullable=True),
        sa.Column("changed_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("id = 1", name="ck_topology_identity_state_single_row"),
        sa.CheckConstraint("mode IN ('legacy', 'scoped')", name="ck_topology_identity_state_mode"),
    )
    op.execute(
        sa.text(
            "INSERT INTO topology_identity_state (id, mode, manifest_id, changed_at) "
            "VALUES (1, 'legacy', NULL, CURRENT_TIMESTAMP)"
        )
    )
    op.create_table(
        _MANIFEST,
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("snapshot_version", sa.Text(), nullable=False),
        sa.Column(
            "body",
            sa.JSON().with_variant(postgresql.JSONB(), "postgresql"),
            nullable=False,
        ),
        sa.Column("created_by", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("refusal_reason", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "status IN ('staged', 'incomplete', 'cut_over', 'refused')",
            name="ck_topology_scope_manifest_status",
        ),
    )


def downgrade() -> None:
    """Drop both tables; refuse once the cut-over has run."""
    mode = (
        op.get_bind()
        .execute(sa.text("SELECT mode FROM topology_identity_state WHERE id = 1"))
        .scalar_one_or_none()
    )
    if mode == "scoped":
        raise RuntimeError(
            "Refusing to downgrade 0109: the topology scope cut-over has run. "
            "graph_node now holds rows that differ only by source_scope, so the "
            "old (tenant_id, kind, name) key cannot come back. Roll forward instead."
        )
    op.drop_table(_MANIFEST)
    op.drop_table(_STATE)
