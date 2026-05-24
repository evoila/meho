# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Create the tenant_conventions and tenant_convention_history tables.

Revision ID: 0015
Revises: 0014
Create Date: 2026-05-24

This is the schema foundation of Initiative #229 (G7.1 Tenant
conventions + Layer 2 starter), Task #313 (T1). The migration adds
two structural pieces that every subsequent G7.1 sibling task
relies on:

* ``tenant_conventions`` -- the current-state table for tenant-scoped
  operational / workflow / reference rules. T2 (#314) wires the API
  routes against this table; T3 (#315) the CLI verbs; T4 (#316) the
  session-preamble assembler reads the ``kind='operational'`` subset
  ordered by ``priority DESC, created_at ASC``; T5 (#317) seeds 8-12
  rows for the ``rdc-internal`` tenant.
* ``tenant_convention_history`` -- the diff trail for every convention
  edit. The history row carries the before/after body, actor sub, and
  a soft-FK to the audit row that triggered the change. T2's PATCH /
  POST / DELETE routes insert a history row in the same DB
  transaction as the conventions write; T2's ``GET /{slug}/history``
  surfaces this table chronologically with the audit cross-reference.

Why ``priority`` is non-nullable on ``tenant_conventions``
---------------------------------------------------------

T4's preamble assembler packs operational conventions
**highest-priority-first** and drops lowest-priority entries whole
when over the token budget (never mid-entry truncation of an
operational rule). The column must exist with ``NOT NULL`` semantics
before the T5 seed migration writes rows -- retrofitting a NOT NULL
column after seeded data exists is a needless multi-step migration
(add column nullable -> backfill -> tighten to NOT NULL). The PG
server default of ``0`` and the ORM-side Python default of ``0``
keep the T2 ``ConventionCreate`` contract backward-compatible
(priority optional, defaults to 0) without forcing every API caller
to set it explicitly.

The Pydantic / API layer (T2) will validate the value range; the
DB-layer ``SMALLINT`` already bounds the column to -32768..32767
which is more than enough for the ranking key. Mirrors MCP
2025-06-18's own resource ``priority`` annotation where 1.0 is
"effectively required" -- the same semantic, on the SMALLINT
column instead of the floating-point annotation, to avoid wasting
a real-number comparison on what is fundamentally an ordering key.

Why two tables instead of one
-----------------------------

``tenant_conventions`` carries current state for fast reads (the
hot path: every session preamble assembly + every CLI list/show
query). ``tenant_convention_history`` carries the diff trail
without bloating the read path. Splitting the two means the
preamble assembler can ``SELECT * FROM tenant_conventions WHERE
tenant_id = ? AND kind = 'operational'`` without scanning historical
rows, and ``meho conventions history <slug>`` queries the dedicated
history table with a per-convention index. The ``audit_id`` soft-FK
on history rows lets G8's audit query cross-reference the original
audit row that authored the change.

Why soft FKs (no ``REFERENCES tenant(id)`` clause)
--------------------------------------------------

The issue body specifies soft FKs throughout:

  "Soft FKs everywhere matches the chassis convention -- column
   types match the referenced tables but no REFERENCES ...
   ON DELETE CASCADE clauses. Simplifies migration reversibility;
   v0.2.next can tighten."

This is a deliberate author choice, not an oversight: it keeps the
two new tables aligned with the ``tenant_convention_history``
soft-FK columns (``convention_id`` -> ``tenant_conventions.id``,
``audit_id`` -> ``audit_log.id``) and defers cascade-policy
decisions to a v0.2.next tightening migration once the surrounding
delete semantics (does deleting a tenant cascade its conventions?
its history?) are exercised in production. The application layer
(T2's CRUD) enforces referential integrity at insert time until
the tightening migration adds the FK clauses.

Why ``kind`` is not a DB-level enum
-----------------------------------

The Out-of-Scope on the issue body explicitly defers DB-level enum
enforcement for ``kind`` to the Pydantic / application layer:

  "DB-level enum for kind -- Pydantic + application-level validation
   is enough."

Mirrors :class:`Target.auth_model` (free-form text + Pydantic
validation) rather than :class:`OperationGroup.review_status`
(DB-level CHECK constraint via portable enum-shape). Operators
adding a new kind don't need a migration; the Pydantic layer is the
single source of truth for the bounded set.

Dialect-portability decisions
-----------------------------

Mirrors the discipline ``0001_create_audit_log.py`` and
``0002_create_tenant_and_audit_tenant_id.py`` established. The
migration runs cleanly against both PostgreSQL (production) and
SQLite (dev/test via aiosqlite).

* ``id`` server default -- PG 13+ ships ``gen_random_uuid()``
  built-in (same assumption every prior migration makes); SQLite
  leaves the column without a server default and lets the ORM
  ``default=uuid.uuid4`` populate it Python-side.
* ``created_at`` / ``updated_at`` / ``ts`` server defaults -- PG
  gets ``now()``; SQLite leaves it to ORM-side
  ``default=lambda: datetime.now(UTC)``. The ORM also declares
  ``onupdate=lambda: datetime.now(UTC)`` on ``tenant_conventions.updated_at``
  so ORM UPDATEs bump the timestamp; raw-SQL UPDATEs against PG do
  not fire this hook (acceptable in v0.2 because T2's CRUD is the
  sole writer path).
* ``priority`` server default -- PG gets ``'0'`` (integer literal);
  SQLite gets the same text literal. The portable
  :class:`SmallInteger` type compiles to ``SMALLINT`` on both
  dialects, so the server default text ``"0"`` parses identically.
  The ORM also declares ``default=0`` so out-of-band inserts that
  omit ``priority`` survive without depending on the dialect.
* Indexes -- both the unique composite ``tenant_conventions_tenant_slug_idx``
  on ``(tenant_id, slug)`` and the per-convention btree
  ``tenant_convention_history_convention_idx`` on ``(convention_id, ts)``
  declare ``postgresql_using="btree"`` explicitly so PG gets the
  named btree (the dominant access shape) and SQLite gets a btree
  no-op (the only index type SQLite supports). The unique index
  enforces uniqueness exclusively; we deliberately omit ``unique=True``
  on the ``slug`` column so PG does not auto-create a second unique
  index next to the named one (same discipline as ``tenant_slug_idx``
  in 0002).

Reversibility contract
----------------------

``downgrade()`` removes everything ``upgrade()`` creates, in
reverse order: drop the history index -> drop the history table ->
drop the conventions unique index -> drop the conventions table.
Both tables are brand-new in this migration so dropping them on
downgrade is the only reversal needed; no chassis-era rows to
preserve. The CI guard (``scripts/ci/check_migration_compat.py``)
inspects only ``upgrade()`` -- the destructive operations in
``downgrade()`` are allowed by design.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0015"
down_revision: str | None = "0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create ``tenant_conventions`` + ``tenant_convention_history`` + indexes."""
    bind = op.get_bind()
    is_postgres = bind.dialect.name == "postgresql"

    op.create_table(
        "tenant_conventions",
        sa.Column(
            "id",
            sa.Uuid(),
            primary_key=True,
            nullable=False,
            server_default=sa.text("gen_random_uuid()") if is_postgres else None,
        ),
        # ``tenant_id`` -- soft FK to ``tenant.id`` per the issue body.
        # NOT NULL because every convention belongs to exactly one
        # tenant; uniqueness on ``(tenant_id, slug)`` enforced by the
        # named index below.
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("slug", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        # ``kind`` -- free-form text, validated by Pydantic at the API
        # layer (T2). DB-level enum deferred per the issue's Out of scope.
        sa.Column("kind", sa.Text(), nullable=False),
        # ``priority`` -- the ranking key T4's preamble assembler reads.
        # Higher = packed first; over-budget drops drop lowest first.
        # ``NOT NULL DEFAULT 0`` so T2's ``ConventionCreate`` contract
        # stays backward-compatible (priority optional). SMALLINT
        # bounds it to -32768..32767 -- more than enough for an
        # ordering key.
        sa.Column(
            "priority",
            sa.SmallInteger(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        # ``created_by_sub`` -- the JWT ``sub`` claim of the creator.
        # Nullable for migration-seeded rows (T5's seed migration has
        # no operator context); T2's POST route populates it from the
        # request principal.
        sa.Column("created_by_sub", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()") if is_postgres else None,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()") if is_postgres else None,
        ),
    )
    # Named **unique** composite btree on ``(tenant_id, slug)``. Single
    # source of uniqueness enforcement -- we deliberately do not set
    # ``unique=True`` on the ``slug`` column itself (PG would otherwise
    # auto-create a second unique index alongside the named one, doubling
    # per-write index maintenance for zero benefit). The named index is
    # the stable identifier later migrations and the application's
    # ``ON CONFLICT (tenant_id, slug)`` upsert target will reference.
    op.create_index(
        "tenant_conventions_tenant_slug_idx",
        "tenant_conventions",
        ["tenant_id", "slug"],
        unique=True,
        postgresql_using="btree",
    )

    op.create_table(
        "tenant_convention_history",
        sa.Column(
            "id",
            sa.Uuid(),
            primary_key=True,
            nullable=False,
            server_default=sa.text("gen_random_uuid()") if is_postgres else None,
        ),
        # ``convention_id`` -- soft FK to ``tenant_conventions.id``.
        # NOT NULL because every history row attaches to exactly one
        # convention; the index below makes the per-convention
        # chronological query (``meho conventions history <slug>``) a
        # btree probe instead of a table scan.
        sa.Column("convention_id", sa.Uuid(), nullable=False),
        # ``body_before`` -- nullable because the first history row
        # (the CREATE event) has no prior state. T2's POST inserts a
        # history row with ``body_before=NULL`` and
        # ``body_after=<initial body>``; subsequent PATCHes shift the
        # previous body into ``body_before``.
        sa.Column("body_before", sa.Text(), nullable=True),
        sa.Column("body_after", sa.Text(), nullable=False),
        # ``actor_sub`` -- the JWT ``sub`` claim of the editor. NOT NULL
        # because every history row must record who made the change;
        # T5's seed migration uses a synthetic sub (``"system:seed"``)
        # for the initial seed rows.
        sa.Column("actor_sub", sa.Text(), nullable=False),
        sa.Column(
            "ts",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()") if is_postgres else None,
        ),
        # ``audit_id`` -- soft FK to ``audit_log.id``. Nullable for
        # migration-seeded rows (T5 has no audit_log row to point at);
        # T2's CRUD routes populate it from the audit middleware's
        # contextvar so G8's audit-query path can cross-reference the
        # convention change back to the originating request.
        sa.Column("audit_id", sa.Uuid(), nullable=True),
    )
    # ``(convention_id, ts)`` btree -- drives the ``meho conventions
    # history <slug>`` query which fetches all history rows for a
    # convention in chronological order. ``ts`` second means the index
    # is also useful for "last N edits for this convention" probes
    # without an extra ORDER BY scan.
    op.create_index(
        "tenant_convention_history_convention_idx",
        "tenant_convention_history",
        ["convention_id", "ts"],
        postgresql_using="btree",
    )


def downgrade() -> None:
    """Reverse the upgrade -- drop both tables and their indexes.

    Symmetric inverse of :func:`upgrade`. Indexes are dropped
    explicitly even though ``op.drop_table`` cascades them on PG;
    the explicit drops keep the reversal clean on SQLite (which does
    not always cascade indexes on ``drop_table``) and survive dialect
    drift. The CI guard (``scripts/ci/check_migration_compat.py``)
    inspects only ``upgrade()`` -- the drops here are allowed by
    design.
    """
    op.drop_index(
        "tenant_convention_history_convention_idx",
        table_name="tenant_convention_history",
    )
    op.drop_table("tenant_convention_history")
    op.drop_index(
        "tenant_conventions_tenant_slug_idx",
        table_name="tenant_conventions",
    )
    op.drop_table("tenant_conventions")
