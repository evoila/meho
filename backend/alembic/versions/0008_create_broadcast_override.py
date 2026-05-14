# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Create the broadcast_override table for G6.3 PII opt-in/opt-out controls.

Revision ID: 0007
Revises: 0006
Create Date: 2026-05-14

This migration is the schema substrate of Task #378 (G6.3-T1) under
Initiative #376. T1 is the schema-only first step; no call sites change
in this PR and no publish path runs through the new model yet -- that's
T2 (#379). Splitting the schema work off keeps the migration reviewable
on its own (same discipline migration ``0002`` followed for the
tenant table).

Numbering note
--------------

The Initiative body in #376 and the Task body in #378 reference this
migration as ``0004_create_broadcast_override.py`` revising ``0003``.
That was the issue author's draft assumption at filing time, when only
migrations 0001 / 0002 / 0003 existed. By 2026-05-14 the chassis has
shipped through ``0006_add_audit_log_parent_audit_id`` (the G0.6-T7
composite-recursion column), so the next sequential revision is
``0007``. The Task's acceptance criteria are about migration *existence*
and *behaviour*, not filename literal; renumbering to 0007 satisfies the
contract without papering over the in-tree state.

What this migration adds
------------------------

* The ``broadcast_override`` table -- per-tenant rules that downgrade
  normally-full-detail operations to ``aggregate``-only on the SSE
  broadcast feed. Resolved at publish time by T2's
  :func:`compute_effective_broadcast_detail`, populated via T4's
  tenant-admin verbs (REST / CLI / MCP).
* Two indexes:

  - ``broadcast_override_tenant_unique_idx`` -- unique composite
    b-tree on ``(tenant_id, op_id_pattern, scope_field,
    scope_value)``. The natural-key uniqueness contract for T4's
    upserts; prevents racing tenant-admin CRUD calls from landing
    duplicate rules for the same scope.
  - ``broadcast_override_tenant_idx`` -- b-tree on ``tenant_id``.
    Drives the resolver's tenant-scoped rule pull at publish time
    (T2's per-tenant cache hydrates with one indexed lookup per
    publish).

Why a real FK to ``tenant.id`` in v0.2
--------------------------------------

Identical rationale to ``documents.tenant_id`` (0003): this is a
brand-new table with no chassis-era rows and a clean downgrade that
drops the whole table -- there is no backfill or cascade decision to
defer. Enforcing the FK at the DB layer is the cheapest point to make
the ownership invariant unbreakable: T4's CRUD verbs cannot silently
insert orphan rules for a typo'd / deleted / replayed tenant id, and a
malformed JWT-claim contextvar surfaces as :class:`IntegrityError` at
insert time instead of as a never-resolving override row at publish
time. The soft-FK discipline that ``audit_log.tenant_id`` /
``audit_log.target_id`` follow is for columns added to *existing*
populated tables; it does not apply here.

Why no CHECK on ``scope_field`` or ``detail``
---------------------------------------------

Both fields are bounded enums -- ``scope_field`` is one of
``NULL`` / ``"namespace"`` / ``"target_name"``, ``detail`` is
``"full"`` / ``"aggregate"`` -- and the chassis precedent
(``targets.auth_model`` 0004, ``operation_group.review_status`` 0005,
``endpoint_descriptor.source_kind`` / ``safety_level`` 0005) favours a
DB-side ``CHECK`` for that shape. T1 deliberately departs from the
precedent on this one axis:

* ``scope_field`` is explicitly extensible -- the Initiative body
  flags configmap names, lab-private VM names, and future scope
  vocabularies as in-scope add-ons. A DB ``CHECK`` would mean a
  migration per new scope field; a Pydantic ``Literal`` at the API
  layer means a code change. The forward-compat argument flips the
  default precedent.
* ``detail`` could carry a CHECK -- the set is stable at
  ``{"full", "aggregate"}`` -- but doing so without the same
  treatment for ``scope_field`` would split the enforcement model
  across the two fields. Choosing one consistency point at the
  Pydantic layer (T4) keeps the table free of value-policy
  constraints and the API layer authoritative on what a tenant
  admin may submit.

Dialect-portability decisions
-----------------------------

Mirrors the discipline 0001 / 0002 / 0003 / 0004 / 0005 / 0006
established:

* ``id`` server default -- PG gets ``gen_random_uuid()``; SQLite leaves
  the column without a server default and relies on the ORM
  ``default=uuid.uuid4`` Python-side at insert time.
* ``created_at`` / ``updated_at`` server defaults -- PG gets ``now()``;
  SQLite leaves it to ORM-side ``default=lambda: datetime.now(UTC)``.
  The ORM also declares ``onupdate=lambda: datetime.now(UTC)`` on
  ``updated_at`` so ORM-side row edits bump the timestamp; raw-SQL
  UPDATEs against PG would not fire the hook, which is acceptable in
  v0.2 because the substrate's only writer is the ORM-backed T4 CRUD
  layer.
* ``scope_field`` and ``scope_value`` are nullable on both dialects --
  ``NULL`` means an op-wide rule. No server default needed.
* Indexes -- both new indexes use b-tree explicitly via
  ``postgresql_using="btree"`` (the kwarg is a no-op on SQLite, which
  only has b-tree indexes). Uniqueness on the composite index is
  declared exclusively via the named index (``unique=True``); the
  column tuple omits ``unique=True`` so PG does not auto-generate a
  second, redundantly-named unique index for the same composite.

Reversibility contract
----------------------

``downgrade()`` undoes everything ``upgrade()`` created in reverse
order: drop the tenant index → drop the unique composite index → drop
the table. Indexes are dropped explicitly so the reversal is clean on
SQLite (which does not always cascade indexes on ``drop_table``) as
well as PG. The CI guard
(``scripts/ci/check_migration_compat.py``) inspects only
``upgrade()``; destructive ops in ``downgrade()`` are allowed by
design.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create ``broadcast_override`` + two indexes."""
    bind = op.get_bind()
    is_postgres = bind.dialect.name == "postgresql"

    op.create_table(
        "broadcast_override",
        sa.Column(
            "id",
            sa.Uuid(),
            primary_key=True,
            nullable=False,
            server_default=sa.text("gen_random_uuid()") if is_postgres else None,
        ),
        # Real REFERENCES tenant(id) FK -- see module docstring for the
        # Document-precedent rationale (brand-new table, no chassis-era
        # rows, FK enforcement at the substrate boundary).
        sa.Column(
            "tenant_id",
            sa.Uuid(),
            sa.ForeignKey("tenant.id"),
            nullable=False,
        ),
        sa.Column("op_id_pattern", sa.Text(), nullable=False),
        # Nullable -- NULL means an op-wide rule. The small allowlist
        # ("namespace" / "target_name") is enforced at the API layer in
        # T4, not via a DB CHECK -- see module docstring.
        sa.Column("scope_field", sa.Text(), nullable=True),
        sa.Column("scope_value", sa.Text(), nullable=True),
        # "full" | "aggregate" -- Pydantic Literal at the API layer.
        sa.Column("detail", sa.Text(), nullable=False),
        sa.Column("created_by_sub", sa.Text(), nullable=False),
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

    # Named unique composite b-tree on (tenant_id, op_id_pattern,
    # scope_field, scope_value) -- the natural-key target for T4's
    # upserts. Uniqueness is enforced exclusively by this named index
    # (no per-column unique=True) so PG does not auto-create a
    # duplicate unique index alongside it; the named identifier stays
    # stable for later migrations / operators to reference.
    op.create_index(
        "broadcast_override_tenant_unique_idx",
        "broadcast_override",
        ["tenant_id", "op_id_pattern", "scope_field", "scope_value"],
        unique=True,
        postgresql_using="btree",
    )
    # Tenant-scoped lookup index -- the resolver's per-tenant cache in
    # T2 hydrates with one indexed scan per publish path. Indexed
    # because the lookup is per-tenant and on the publish hot path.
    op.create_index(
        "broadcast_override_tenant_idx",
        "broadcast_override",
        ["tenant_id"],
        postgresql_using="btree",
    )


def downgrade() -> None:
    """Reverse the upgrade -- drop both indexes, then the table.

    Symmetric inverse of :func:`upgrade`. Indexes are dropped
    explicitly so the migration is reversible cleanly on SQLite
    (which does not always cascade indexes on ``drop_table``) as
    well as PostgreSQL.
    """
    op.drop_index("broadcast_override_tenant_idx", table_name="broadcast_override")
    op.drop_index("broadcast_override_tenant_unique_idx", table_name="broadcast_override")
    op.drop_table("broadcast_override")
