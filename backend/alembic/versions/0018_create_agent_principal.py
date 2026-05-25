# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Create the ``agent_principal`` table for G11.2-T1.

Revision ID: 0018
Revises: 0017
Create Date: 2026-05-25

This migration is the schema substrate of Task #815 (G11.2-T1) under
Initiative #803 (G11.2 Agent identity + RBAC + approval). It creates
the ``agent_principal`` table — one row per MEHO-managed agent identity
registered by the ``meho agent-principal register`` lifecycle verb.

Each row shadows a Keycloak client tagged ``kind=agent`` with
``serviceAccountsEnabled=true``. The agent authenticates against the
MEHO backplane via that client's ``client_credentials`` grant, and the
JWT it receives carries ``principal_kind=agent`` (via the protocol
mapper documented in ``docs/cross-repo/keycloak-agent-client.md``),
which sets ``Operator.principal_kind=PrincipalKind.AGENT`` on every
request dispatched under this identity.

Schema
------

* ``id`` -- UUID primary key. PG production gets ``gen_random_uuid()``
  via this migration; ORM ``default=uuid.uuid4`` covers SQLite and
  out-of-band inserts.

* ``tenant_id`` -- UUID NOT NULL, ``REFERENCES tenant(id)``. A real FK
  because ``agent_principal`` is a brand-new clean-slate table (no
  chassis-era rows); the DB layer enforces ownership invariant at insert
  time. ``NO ACTION`` on delete: removing a tenant that still has agent
  principals is blocked at the DB layer — the operator must revoke all
  agents before the tenant can be deleted.

* ``name`` -- Text NOT NULL. Operator-facing handle (e.g.
  ``incident-triage``). Unique within a tenant (``agent_principal_tenant_name_idx``).

* ``keycloak_client_id`` -- Text NOT NULL UNIQUE. The OAuth
  ``clientId`` in Keycloak — conventionally ``agent:<name>``.
  Keycloak global uniqueness is enforced by
  ``agent_principal_keycloak_client_id_idx``.

* ``keycloak_internal_id`` -- Text NOT NULL. Keycloak's internal UUID
  for the client (the ``id`` field in the admin representation). Used
  by the revoke path to issue the ``PUT /clients/{id}`` call without a
  secondary lookup.

* ``owner_sub`` -- Text NOT NULL. The ``sub`` of the operator who owns
  this principal (the NHI governance kill-switch owner; see Initiative #803).

* ``revoked`` -- Boolean NOT NULL DEFAULT false. Set by the revoke path.
  The row is never hard-deleted so the audit trail stays intact.

* ``created_by_sub`` -- Text NOT NULL. The operator sub that pressed
  the register button.

* ``created_at`` / ``updated_at`` -- ``timestamptz`` NOT NULL. PG
  ``now()`` server defaults; ORM ``default=lambda: datetime.now(UTC)``
  for SQLite.

Indexes
-------

* ``agent_principal_tenant_name_idx`` -- unique composite b-tree on
  ``(tenant_id, name)``. Enforces per-tenant name uniqueness; drives the
  tenant-scoped list query.
* ``agent_principal_keycloak_client_id_idx`` -- unique b-tree on
  ``keycloak_client_id``. Enforces Keycloak-side uniqueness and drives
  the revoke-by-name lookup.

Dialect portability
-------------------

Mirrors the discipline migrations ``0016`` / ``0017`` established:

* ``id`` server default -- ``gen_random_uuid()`` on PG; ORM covers SQLite.
* ``created_at`` / ``updated_at`` server defaults -- ``now()`` on PG; ORM
  covers SQLite.
* Portable booleans -- ``sa.Boolean()`` compiles to ``BOOLEAN`` on PG and
  ``INTEGER`` on SQLite (SQLite's standard bool representation).

Reversibility contract
----------------------

``upgrade()`` creates the table then its indexes; ``downgrade()`` drops
the indexes then the table in inverse order. Explicit index drops keep
the inverse symmetric across both dialects (SQLite does not always auto-
cascade on ``DROP TABLE``).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0018"
down_revision: str | None = "0017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the ``agent_principal`` table and its indexes."""
    bind = op.get_bind()
    is_postgres = bind.dialect.name == "postgresql"

    op.create_table(
        "agent_principal",
        sa.Column(
            "id",
            sa.Uuid(),
            primary_key=True,
            nullable=False,
            server_default=sa.text("gen_random_uuid()") if is_postgres else None,
        ),
        # Real FK -- clean-slate substrate, see module docstring.
        sa.Column(
            "tenant_id",
            sa.Uuid(),
            sa.ForeignKey("tenant.id"),
            nullable=False,
        ),
        sa.Column("name", sa.Text(), nullable=False),
        # OAuth clientId in Keycloak -- globally unique.
        sa.Column("keycloak_client_id", sa.Text(), nullable=False),
        # Keycloak internal UUID used by the revoke PUT call.
        sa.Column("keycloak_internal_id", sa.Text(), nullable=False),
        # NHI governance: every agent must have an owner.
        sa.Column("owner_sub", sa.Text(), nullable=False),
        sa.Column(
            "revoked",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false") if is_postgres else sa.text("0"),
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

    op.create_index(
        "agent_principal_tenant_name_idx",
        "agent_principal",
        ["tenant_id", "name"],
        unique=True,
        postgresql_using="btree",
    )
    op.create_index(
        "agent_principal_keycloak_client_id_idx",
        "agent_principal",
        ["keycloak_client_id"],
        unique=True,
        postgresql_using="btree",
    )


def downgrade() -> None:
    """Drop the indexes then the ``agent_principal`` table."""
    op.drop_index(
        "agent_principal_keycloak_client_id_idx",
        table_name="agent_principal",
    )
    op.drop_index(
        "agent_principal_tenant_name_idx",
        table_name="agent_principal",
    )
    op.drop_table("agent_principal")
