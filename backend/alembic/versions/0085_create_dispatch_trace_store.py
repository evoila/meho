# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Dispatch flight-recorder trace store + capture config (#3212, F1/F4/F6).

Revision ID: 0085
Revises: 0084
Create Date: 2026-08-30

Task #3212 under Initiative #3207. The storage + config substrate of the
dispatch flight recorder (decision of record:
``docs/decisions/dispatch-flight-recorder.md``). This migration ships the
**storage half** (F6) and the **capture-config half** (F1/F4); the capture
code and the redaction engine that write into it are sibling Tasks (#3213 /
#3214), unimplemented here.

Additive-only, per the migration-compatibility house rule -- two brand-new
tables plus three ``ADD COLUMN``s, no ALTER of an existing column and no
data rewrite.

What this migration adds
------------------------

1. ``dispatch_trace`` -- the per-dispatch trace header. Referenced from the
   dispatch's ``audit_log`` row via the soft-FK ``audit_id`` (no DB-level
   ``REFERENCES`` -- the trace is downstream of the audit write and has an
   independent, far shorter lifecycle, mirroring ``BroadcastEvent.audit_id``
   / ``addon_step_event.audit_id``). ``audit_log`` itself is **not touched**:
   it stays slim / append-only, the permanent record of account. ``tenant_id``
   is a real ``REFERENCES tenant(id)`` FK (brand-new-substrate precedent:
   Document / BroadcastOverride / addon_step_event). ``expires_at`` is the
   retention deadline the reaper sweeps on (F4), stamped at write time as
   ``created_at + resolved_retention_days`` so the sweep is a portable
   ``WHERE expires_at < now()`` and the per-tenant window math lives in the
   resolver.

2. ``dispatch_trace_span`` -- the ordered span child table. Real
   ``REFERENCES dispatch_trace(id) ON DELETE CASCADE`` FK so header retention
   takes the spans with it. ``attributes`` is portable JSON -> JSONB, the
   redacted/capped span detail container (inner shape owned by the seam).

3. ``tenant.flight_recorder_enabled`` -- per-tenant capture default (F1).
   Boolean NOT NULL DEFAULT ``false`` -- OFF by default, following the
   ``announce_gate_enabled`` precedent (migration ``0077``) exactly. A
   lab-class tenant is one an operator flips this ON for at seeding; there
   is no "lab-class" marker in the schema. ``server_default=false`` backfills
   pre-#3212 rows so the ``NOT NULL`` add-column is safe on a populated table.

4. ``tenant.flight_recorder_retention_days`` -- per-tenant retention override
   (F4). Integer nullable; ``NULL`` -> use the global default
   (``FLIGHT_RECORDER_RETENTION_DAYS_DEFAULT``, 7 days). A lab-class tenant is
   configured with ``14``. Nullable, so the add-column needs no backfill.

5. ``targets.flight_recorder_capture`` -- per-target override in both
   directions (F1). Boolean nullable tri-state: ``NULL`` inherit,
   ``True`` force on, ``False`` force off. Nullable, so no backfill.

Dialect-portability decisions
-----------------------------

* Portable-UUID PK via :class:`sqlalchemy.Uuid` (``UUID`` on PG,
  ``CHAR(32)`` on SQLite). No ``gen_random_uuid()`` server default -- the
  ORM (:func:`meho_backplane.flight_recorder.store.record_trace`) always
  supplies ``id`` on insert, matching the ``addon_step_event`` (``0083``)
  precedent.
* ``created_at`` gets a PG-side ``now()`` server default; on SQLite the ORM
  ``default`` covers it. ``expires_at`` has no server default -- it is a
  brand-new empty table and the write API always stamps it.
* ``attributes`` is ``JSON().with_variant(JSONB(), "postgresql")`` -- binary
  JSONB on PG, generic JSON (text) on SQLite.
* :class:`sqlalchemy.Boolean` renders ``BOOLEAN`` on PG and an ``INTEGER``
  0/1 CHECK on SQLite; ``server_default=sa.false()`` maps to ``false`` / ``0``
  per dialect, so the tenant flag backfill is portable (``0077`` precedent).

Reversibility contract
----------------------

``downgrade()`` drops the two tables (spans before header for FK order) and
the three columns. SQLite ALTER-TABLE drop-column is supported since 3.35.0
(we run 3.45+), so Alembic batch-mode is not required.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0085"
down_revision: str | None = "0084"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    is_postgres = bind.dialect.name == "postgresql"

    # (1) The per-dispatch trace header.
    op.create_table(
        "dispatch_trace",
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        # Soft-FK to audit_log.id (no REFERENCES): downstream of the audit
        # write with an independent lifecycle -- see module docstring.
        sa.Column("audit_id", sa.Uuid(), nullable=False),
        sa.Column(
            "tenant_id",
            sa.Uuid(),
            sa.ForeignKey("tenant.id"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()") if is_postgres else None,
        ),
        # Retention deadline (F4); the reaper sweeps ``expires_at < now()``.
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "redaction_uncertain",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    # One trace header per dispatch.
    op.create_index(
        "dispatch_trace_audit_id_idx",
        "dispatch_trace",
        ["audit_id"],
        unique=True,
        postgresql_using="btree",
    )
    # Drives the retention reaper's bounded delete.
    op.create_index(
        "dispatch_trace_expires_at_idx",
        "dispatch_trace",
        ["expires_at"],
        postgresql_using="btree",
    )

    # (2) The ordered span child table.
    attributes_type = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")
    op.create_table(
        "dispatch_trace_span",
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column(
            "trace_id",
            sa.Uuid(),
            sa.ForeignKey("dispatch_trace.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("span_kind", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("duration_ms", sa.Numeric(10, 2), nullable=True),
        sa.Column("status", sa.Text(), nullable=True),
        sa.Column(
            "attributes",
            attributes_type,
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
    )
    # Ordered read within a trace + the index PG wants behind the FK cascade.
    op.create_index(
        "dispatch_trace_span_trace_seq_idx",
        "dispatch_trace_span",
        ["trace_id", "seq"],
        postgresql_using="btree",
    )

    # (3) Per-tenant capture default (F1) -- OFF by default, server_default
    # backfills existing rows so the NOT NULL add-column is safe.
    op.add_column(
        "tenant",
        sa.Column(
            "flight_recorder_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    # (4) Per-tenant retention override (F4) -- NULL = use global default.
    op.add_column(
        "tenant",
        sa.Column("flight_recorder_retention_days", sa.Integer(), nullable=True),
    )
    # (5) Per-target override both directions (F1) -- NULL inherit / True on /
    # False off.
    op.add_column(
        "targets",
        sa.Column("flight_recorder_capture", sa.Boolean(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("targets", "flight_recorder_capture")
    op.drop_column("tenant", "flight_recorder_retention_days")
    op.drop_column("tenant", "flight_recorder_enabled")

    op.drop_index("dispatch_trace_span_trace_seq_idx", table_name="dispatch_trace_span")
    op.drop_table("dispatch_trace_span")

    op.drop_index("dispatch_trace_expires_at_idx", table_name="dispatch_trace")
    op.drop_index("dispatch_trace_audit_id_idx", table_name="dispatch_trace")
    op.drop_table("dispatch_trace")
