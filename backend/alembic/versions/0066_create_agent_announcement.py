# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Create the ``agent_announcement`` table (append-only durable announcements).

Revision ID: 0066
Revises: 0065
Create Date: 2026-07-17

Broadcast v2 Initiative #2543, Task #2547 (T2). Gives agent-authored
announcements a durable home. Until now an announcement lived only on
the per-tenant Valkey stream ``meho:feed:{tenant_id}`` -- count-trimmed
at ``BROADCAST_MAXLEN`` = 10000 and wiped on restart because the
broadcast subchart runs with persistence disabled (``save ""`` /
``appendonly no``, "streams are ephemeral by design"). Operations
persist forever in ``audit_log``; coordination *intent* evaporated
within ~a day. This table is the archive half of the split (hot stream +
durable table), the same division Kubernetes draws between its bounded
Events TTL and the durable records it expects to live elsewhere.

Append-only mold from ``0012_create_topology_history`` -- the only
DELETE against this table is the retention prune
(:mod:`meho_backplane.broadcast.announcement_retention`), which drops
rows older than ``broadcast_announcement_retention_days`` in one bounded
audited batch per tick.

Table shape
-----------

* ``id`` -- ``UUID`` PK. Minted Python-side at publish (the
  :class:`~meho_backplane.broadcast.agent_events.AgentAnnouncementEvent`
  ``event_id`` default), written both here and onto the stream entry's
  JSON so the announce return's ``event_id`` is a genuine, stable UUID
  distinct from the stream ``cursor`` (#2479 / #2547). A UUID (not a
  ``BIGSERIAL`` like the history tables) because the value must be known
  before the row is written so the same identity can ride the stream
  entry without a read-back round-trip.
* ``tenant_id`` -- ``UUID`` NOT NULL, real ``REFERENCES tenant(id)`` FK.
  Brand-new substrate, no chassis-era rows -- same rationale as the
  history tables' ``tenant_id``.
* ``principal_sub`` -- ``TEXT`` NOT NULL. The announcing operator's JWT
  ``sub``.
* ``activity`` -- ``TEXT`` NOT NULL. The free-text body. Persisted raw;
  the untrusted-content envelope is applied on serve, not in storage.
* ``target`` / ``scope`` -- ``TEXT`` nullable. Legacy single-target
  attribution + optional scope hint.
* ``targets`` -- portable ``JSON`` -> native ``TEXT[]`` on PG NOT NULL
  DEFAULT ``[]``. The typed multi-target claim list (T1 #2544).
* ``phase`` -- ``TEXT`` NOT NULL. No DB-side CHECK -- the pydantic
  ``Literal`` on the event is the boundary guard and a future phase
  value should not require a migration to reach the archive.
* ``planned_op_class`` -- ``TEXT`` nullable. Declared intent op-class.
* ``ttl_minutes`` -- ``INTEGER`` nullable. Claim lifetime.
* ``work_ref`` -- ``TEXT`` nullable. Opaque change-ticket reference,
  same convention as ``agent_run.work_ref``.
* ``run_id`` -- ``UUID`` nullable. Soft reference to the agent run (no
  FK -- the run may age out on a different cadence than the archive).
* ``created_at`` -- ``timestamptz`` NOT NULL. The event's server-side
  ``ts``, so the archive timeline matches the stream timeline. PG-side
  ``now()`` server default; the ORM also declares a Python default for
  the SQLite dev/test path.

Index discipline
----------------

Two named indexes (the same discipline the history tables follow):

* ``agent_announcement_tenant_created_at_idx`` -- composite b-tree on
  ``(tenant_id, created_at DESC)``. Drives the recent-window archive
  backfill (newest-first tenant scan) and the retention prune's bounded
  ``created_at < cutoff`` delete (the filter rides the index in
  reverse). DESC ordering uses ``sa.text("created_at DESC")`` because
  the table is created in the same migration -- there is no
  :class:`sqlalchemy.Column` reference yet.
* ``agent_announcement_tenant_work_ref_idx`` -- composite b-tree on
  ``(tenant_id, work_ref)``. Drives the exact-match ``work_ref`` filter,
  mirroring ``agent_run_tenant_work_ref_idx``.

Reversibility
-------------

``downgrade()`` drops both indexes (explicitly, so SQLite -- which does
not always cascade indexes on ``drop_table`` -- stays clean) then the
table. No chassis-era rows exist (the table ships ahead of any
announcement write), so the downgrade is a clean substrate teardown.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0066"
down_revision: str | None = "0065"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the ``agent_announcement`` table and its two indexes."""
    bind = op.get_bind()
    is_postgres = bind.dialect.name == "postgresql"

    # Portable multi-target list: native ``TEXT[]`` on PG, JSON array on
    # SQLite -- mirrors ``meho_backplane.db.models._PORTABLE_ARRAY``. The
    # empty-list server default must be dialect-correct: a PG ``text[]``
    # column takes the ``'{}'::text[]`` array literal (a ``'[]'::jsonb``
    # literal is a datatype mismatch), while the SQLite JSON column takes
    # the JSON empty-array string ``'[]'``.
    targets_type = sa.JSON().with_variant(postgresql.ARRAY(sa.Text()), "postgresql")
    targets_default = sa.text("'{}'::text[]") if is_postgres else sa.text("'[]'")

    op.create_table(
        "agent_announcement",
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column(
            "tenant_id",
            sa.Uuid(),
            sa.ForeignKey("tenant.id"),
            nullable=False,
        ),
        sa.Column("principal_sub", sa.Text(), nullable=False),
        sa.Column("activity", sa.Text(), nullable=False),
        sa.Column("target", sa.Text(), nullable=True),
        sa.Column("scope", sa.Text(), nullable=True),
        sa.Column(
            "targets",
            targets_type,
            nullable=False,
            server_default=targets_default,
        ),
        sa.Column("phase", sa.Text(), nullable=False),
        sa.Column("planned_op_class", sa.Text(), nullable=True),
        sa.Column("ttl_minutes", sa.Integer(), nullable=True),
        sa.Column("work_ref", sa.Text(), nullable=True),
        sa.Column("run_id", sa.Uuid(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()") if is_postgres else None,
        ),
    )

    # (tenant_id, created_at DESC) -- newest-first tenant scan for the
    # recent-window archive backfill + the cutoff-bounded prune delete.
    op.create_index(
        "agent_announcement_tenant_created_at_idx",
        "agent_announcement",
        ["tenant_id", sa.text("created_at DESC")],
        postgresql_using="btree",
    )
    # (tenant_id, work_ref) -- exact-match work_ref filter.
    op.create_index(
        "agent_announcement_tenant_work_ref_idx",
        "agent_announcement",
        ["tenant_id", "work_ref"],
        postgresql_using="btree",
    )


def downgrade() -> None:
    """Drop the two indexes then the table (reverse of :func:`upgrade`)."""
    op.drop_index(
        "agent_announcement_tenant_work_ref_idx",
        table_name="agent_announcement",
    )
    op.drop_index(
        "agent_announcement_tenant_created_at_idx",
        table_name="agent_announcement",
    )
    op.drop_table("agent_announcement")
