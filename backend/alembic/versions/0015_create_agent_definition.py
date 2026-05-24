# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Create the agent_definition table for the G11.1 agent runtime.

Revision ID: 0015
Revises: 0014
Create Date: 2026-05-24

This migration is the schema substrate of Task #809 (G11.1-T2) under
Initiative #802 (the P1 agent runtime). It creates the
``agent_definition`` table -- the first-class, tenant-scoped record the
runtime (T1 #808) loads to know which agent it is running: identity
reference, logical model tier, system prompt, toolset spec, turn
budget, optional output schema, and an enabled flag.

What this migration adds
------------------------

* The ``agent_definition`` table -- per-tenant agent definitions
  managed via the tenant-admin CRUD surface (REST / MCP / CLI) shipped
  alongside this migration.
* One index: ``agent_definition_tenant_name_idx`` -- a unique composite
  b-tree on ``(tenant_id, name)``. It enforces per-tenant name
  uniqueness (the natural key for the CRUD lookup / upsert) and drives
  the tenant-scoped list query. Uniqueness is declared exclusively via
  the named index (no per-column ``unique=True``) so PG does not
  auto-create a redundant duplicate unique index alongside it.

Why a real FK to ``tenant.id`` in v0.2
--------------------------------------

Identical rationale to ``documents.tenant_id`` (0003) and
``broadcast_override.tenant_id`` (0008): a brand-new table with no
chassis-era rows and a clean downgrade that drops the whole table has
no backfill or cascade decision to defer. Enforcing the FK at the DB
layer is the cheapest point to make the ownership invariant
unbreakable -- the CRUD service cannot silently insert an orphan
definition for a typo'd / deleted / replayed tenant id, and a
malformed JWT-claim contextvar surfaces as :class:`IntegrityError` at
insert time instead of as a never-resolving definition at run time.

Why no CHECK on ``model_tier``
------------------------------

``model_tier`` is a bounded enum (``standard`` / ``fast`` / ``deep``)
and the chassis precedent (``targets.auth_model`` 0004) favours a
DB-side ``CHECK`` for that shape. This migration deliberately departs
on this one axis, mirroring ``broadcast_override.scope_field`` /
``detail`` (0008): the logical tier vocabulary is explicitly
extensible (G11.5's multi-provider resolver may add tiers), so a
Pydantic ``Literal`` at the API layer (a code change) is preferred over
a DB ``CHECK`` (a migration per new tier).

Dialect-portability decisions
-----------------------------

Mirrors the discipline 0001-0014 established:

* ``id`` server default -- PG gets ``gen_random_uuid()``; SQLite leaves
  the column without a server default and relies on the ORM
  ``default=uuid.uuid4`` Python-side at insert time.
* ``created_at`` / ``updated_at`` server defaults -- PG gets ``now()``;
  SQLite leaves it to the ORM ``default=lambda: datetime.now(UTC)``.
  The ORM also declares ``onupdate=lambda: datetime.now(UTC)`` on
  ``updated_at`` so ORM-side row edits bump the timestamp.
* ``toolset`` -- NOT NULL with a server default of ``'{}'`` on PG so a
  raw insert that omits it lands the empty-object form; the ORM
  ``default=dict`` covers the SQLite path.
* ``enabled`` -- NOT NULL with a PG server default of ``true``; the ORM
  ``default=True`` covers SQLite.
* ``output_schema`` -- nullable on both dialects (``NULL`` means
  free-form text output). No server default needed.
* Index -- b-tree explicitly via ``postgresql_using="btree"`` (a no-op
  on SQLite, which only has b-tree indexes). Uniqueness is declared
  exclusively via the named index.

Reversibility contract
----------------------

``downgrade()`` undoes everything ``upgrade()`` created in reverse
order: drop the index, then the table. The index is dropped explicitly
so the reversal is clean on SQLite (which does not always cascade
indexes on ``drop_table``) as well as PG. The CI guard
(``scripts/ci/check_migration_compat.py``) inspects only ``upgrade()``;
destructive ops in ``downgrade()`` are allowed by design.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0015"
down_revision: str | None = "0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create ``agent_definition`` + its unique tenant/name index."""
    bind = op.get_bind()
    is_postgres = bind.dialect.name == "postgresql"

    # Portable JSONB -> JSON variant; same pattern documents.metadata
    # (0003) / graph_node.properties (0007) use: JSONB (GIN-friendly,
    # indexable by ``@>``) on PG, generic text JSON on SQLite.
    json_type = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")

    op.create_table(
        "agent_definition",
        sa.Column(
            "id",
            sa.Uuid(),
            primary_key=True,
            nullable=False,
            server_default=sa.text("gen_random_uuid()") if is_postgres else None,
        ),
        # Real REFERENCES tenant(id) FK -- see module docstring for the
        # Document / BroadcastOverride precedent rationale.
        sa.Column(
            "tenant_id",
            sa.Uuid(),
            sa.ForeignKey("tenant.id"),
            nullable=False,
        ),
        sa.Column("name", sa.Text(), nullable=False),
        # Soft reference to the G11.2 agent principal -- no FK.
        sa.Column("identity_ref", sa.Text(), nullable=False),
        # Logical tier ("standard" | "fast" | "deep"); bounded at the
        # Pydantic layer, not via a DB CHECK -- see module docstring.
        sa.Column("model_tier", sa.Text(), nullable=False),
        sa.Column("system_prompt", sa.Text(), nullable=False),
        sa.Column(
            "toolset",
            json_type,
            nullable=False,
            server_default=sa.text("'{}'::jsonb") if is_postgres else None,
        ),
        sa.Column("turn_budget", sa.Integer(), nullable=False),
        # Optional JSON Schema for structured output; NULL = free-form text.
        sa.Column(
            "output_schema",
            json_type,
            nullable=True,
        ),
        sa.Column(
            "enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true") if is_postgres else None,
        ),
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

    # Named unique composite b-tree on (tenant_id, name) -- enforces
    # per-tenant name uniqueness (the CRUD natural key) and drives the
    # tenant-scoped list query. Uniqueness is enforced exclusively by
    # this named index (no per-column unique=True) so PG does not
    # auto-create a duplicate unique index alongside it.
    op.create_index(
        "agent_definition_tenant_name_idx",
        "agent_definition",
        ["tenant_id", "name"],
        unique=True,
        postgresql_using="btree",
    )


def downgrade() -> None:
    """Reverse the upgrade -- drop the index, then the table.

    Symmetric inverse of :func:`upgrade`. The index is dropped
    explicitly so the migration is reversible cleanly on SQLite (which
    does not always cascade indexes on ``drop_table``) as well as
    PostgreSQL.
    """
    op.drop_index("agent_definition_tenant_name_idx", table_name="agent_definition")
    op.drop_table("agent_definition")
