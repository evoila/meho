# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Create the ``agent_permission`` table for the G11.2-T3 permission model.

Revision ID: 0019
Revises: 0018
Create Date: 2026-05-25

This migration is the schema substrate of Task #820 (G11.2-T3) under
Initiative #803 (the P3 agent identity + RBAC + approval gate). It
adds the ``agent_permission`` table -- one row per per-(principal,
op-pattern, target-scope) permission grant, carrying a three-state
verdict: ``auto-execute``, ``needs-approval``, or ``deny``.

What this migration adds
------------------------

* The ``agent_permission`` table -- per-tenant permission grants keyed
  on ``(tenant_id, principal_sub, op_pattern, target_scope)``.
* One index: ``agent_permission_tenant_principal_idx`` -- a b-tree on
  ``(tenant_id, principal_sub)`` that drives the dominant query ("all
  grants for principal P in tenant T"). The permission resolver loads
  all matching rows into memory, then evaluates op_pattern globs
  in-process.
* One CHECK constraint: ``ck_agent_permission_verdict`` -- enforces the
  closed ``verdict IN ('auto-execute', 'needs-approval', 'deny')``
  vocabulary at the DB layer.

Why a real FK to ``tenant.id``
------------------------------

Identical rationale to ``agent_definition.tenant_id`` (0016): a
brand-new table with no chassis-era rows and a clean downgrade that
drops the whole table has no backfill or cascade decision to defer.
Enforcing the FK at the DB layer makes the ownership invariant
unbreakable -- a malformed JWT-claim contextvar surfaces as
:class:`IntegrityError` at insert time rather than as a
never-resolving permission grant at dispatch time.

Why ``principal_sub`` has no FK
--------------------------------

The agent-principal / Keycloak-client table is G11.2-T1 (#815)'s
scope. T3 ships before T1 settles the schema. The same soft-FK
discipline :attr:`~meho_backplane.db.models.AgentDefinition.identity_ref`
uses applies here: the reference is opaque text (the JWT ``sub``
claim), which is already the stable, Keycloak-issued principal
identifier. A tightening migration can add the FK once T1 lands.

Why ``verdict`` gets a DB-level CHECK
--------------------------------------

The verdict vocabulary is intentionally closed and small (three
values). A new verdict would require both a code change and a
migration -- the cheapest possible way to prevent drift between the
DB row and the policy engine. This mirrors the
:attr:`~meho_backplane.db.models.EndpointDescriptor.safety_level`
CHECK on ``endpoint_descriptor`` (0005).

Dialect-portability decisions
-----------------------------

Mirrors the discipline 0001-0017 established:

* ``id`` server default -- PG gets ``gen_random_uuid()``; SQLite
  leaves the column without a server default and relies on the ORM
  ``default=uuid.uuid4`` Python-side at insert time.
* ``created_at`` / ``updated_at`` server defaults -- PG gets
  ``now()``; SQLite leaves it to the ORM
  ``default=lambda: datetime.now(UTC)``.
* Index -- b-tree explicitly via ``postgresql_using="btree"`` (a
  no-op on SQLite).
* CHECK -- ``sa.CheckConstraint`` with a named constraint; portable
  across both dialects.

Reversibility contract
----------------------

``downgrade()`` undoes everything ``upgrade()`` created in reverse
order: drop the index, then the table. The index is dropped
explicitly so the reversal is clean on SQLite (which does not always
cascade indexes on ``drop_table``) as well as PG. The CI guard
inspects only ``upgrade()``; destructive ops in ``downgrade()`` are
allowed by design.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0019"
down_revision: str | None = "0018"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create ``agent_permission`` + its index + verdict CHECK."""
    bind = op.get_bind()
    is_postgres = bind.dialect.name == "postgresql"

    op.create_table(
        "agent_permission",
        sa.Column(
            "id",
            sa.Uuid(),
            primary_key=True,
            nullable=False,
            server_default=sa.text("gen_random_uuid()") if is_postgres else None,
        ),
        # Real REFERENCES tenant(id) FK -- see module docstring.
        sa.Column(
            "tenant_id",
            sa.Uuid(),
            sa.ForeignKey("tenant.id"),
            nullable=False,
        ),
        # JWT ``sub`` of the principal being granted the permission.
        # Soft reference -- no FK (agent principal table is T1's scope).
        sa.Column("principal_sub", sa.Text(), nullable=False),
        # fnmatch-compatible glob string. "*" = every op.
        sa.Column("op_pattern", sa.Text(), nullable=False),
        # NULL or "*" = any target; UUID string = exactly one target.
        sa.Column("target_scope", sa.Text(), nullable=True),
        # Three-state verdict: "auto-execute" | "needs-approval" | "deny".
        sa.Column("verdict", sa.Text(), nullable=False),
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
        # DB-layer closed-vocabulary check on verdict.
        sa.CheckConstraint(
            "verdict IN ('auto-execute', 'needs-approval', 'deny')",
            name="ck_agent_permission_verdict",
        ),
    )

    # b-tree on (tenant_id, principal_sub) -- drives the dominant
    # "all grants for principal P in tenant T" query.
    op.create_index(
        "agent_permission_tenant_principal_idx",
        "agent_permission",
        ["tenant_id", "principal_sub"],
        postgresql_using="btree",
    )


def downgrade() -> None:
    """Reverse the upgrade -- drop the index, then the table.

    Symmetric inverse of :func:`upgrade`. The index is dropped
    explicitly so the migration is reversible cleanly on SQLite (which
    does not always cascade indexes on ``drop_table``) as well as
    PostgreSQL.
    """
    op.drop_index(
        "agent_permission_tenant_principal_idx",
        table_name="agent_permission",
    )
    op.drop_table("agent_permission")
