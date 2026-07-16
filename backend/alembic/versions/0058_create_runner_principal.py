# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Create the ``runner_principal`` table for Initiative #2415 (#2502).

Revision ID: 0058
Revises: 0057
Create Date: 2026-07-15

This migration is the schema substrate of Task #2502 (scoped per-runner
service principal) under Initiative #2415 (Remote execution gateway). It
creates the ``runner_principal`` table — one row per satellite runner
identity registered by ``meho runner-principal register``.

This is the **first** migration in the initiative's serialized chain
(#2502 -> #2498 -> #2499 -> #2500 -> #2501); it extends the then-current
single head ``0057``. Per the house Alembic rule, if an unrelated
migration lands on main first, renumber-before-merge.

Each row shadows a Keycloak client tagged ``kind=runner`` with
``serviceAccountsEnabled=true``. The runner authenticates against the
MEHO backplane via that client's ``client_credentials`` grant, and the
JWT it receives carries ``principal_kind=runner`` + ``tenant_role=read_only``
+ a hardcoded ``runner_id`` claim (the row ``id``), which the JWT chain
materialises onto ``Operator.principal_kind`` / ``Operator.runner_id``.

The table is the direct structural twin of ``agent_principal`` (migration
``0019``): identical columns, identical two-index shape. The runner
lifecycle is moulded on the agent lifecycle (#815), but runner tokens are
a distinct, read-only identity kind caged to the gateway path prefixes.

Schema
------

* ``id`` -- UUID primary key. PG production gets ``gen_random_uuid()``
  via this migration; the ORM ``default=uuid.uuid4`` covers SQLite and
  the register path, which stamps an explicit ``id`` equal to the
  Keycloak ``runner_id`` claim so the token's claim and the row's id are
  the same value.

* ``tenant_id`` -- UUID NOT NULL, ``REFERENCES tenant(id)``. A real FK
  because ``runner_principal`` is a brand-new clean-slate table (no
  chassis-era rows). ``NO ACTION`` on delete: removing a tenant that
  still has runner principals is blocked at the DB layer.

* ``name`` -- Text NOT NULL. Operator-facing handle and the wire/route
  identity for the gateway set (#2498's ``{runner}`` path, #2499's
  ``?runner=``). Unique within a tenant (``runner_principal_tenant_name_idx``).

* ``keycloak_client_id`` -- Text NOT NULL UNIQUE. The OAuth ``clientId``
  in Keycloak — conventionally ``runner:<name>``. Keycloak global
  uniqueness is enforced by ``runner_principal_keycloak_client_id_idx``.

* ``keycloak_internal_id`` -- Text NOT NULL. Keycloak's internal UUID for
  the client. Used by the revoke path's ``PUT /clients/{id}`` call.

* ``owner_sub`` -- Text NOT NULL. The ``sub`` of the operator who owns
  this principal (the kill-switch owner).

* ``revoked`` -- Boolean NOT NULL DEFAULT false. Set by the revoke path.
  The row is never hard-deleted so the audit trail stays intact.

* ``created_by_sub`` -- Text NOT NULL. The operator sub that pressed the
  register button.

* ``created_at`` / ``updated_at`` -- ``timestamptz`` NOT NULL. PG
  ``now()`` server defaults; ORM ``default=lambda: datetime.now(UTC)``
  for SQLite.

Indexes
-------

* ``runner_principal_tenant_name_idx`` -- unique composite b-tree on
  ``(tenant_id, name)``. Enforces per-tenant name uniqueness; drives the
  tenant-scoped list query and the gateway guard's name->row lookup.
* ``runner_principal_keycloak_client_id_idx`` -- unique b-tree on
  ``keycloak_client_id``. Enforces Keycloak-side uniqueness and drives
  the revoke-by-name lookup.

Dialect portability
-------------------

Mirrors ``0019``: ``gen_random_uuid()`` / ``now()`` server defaults on PG
with the ORM covering SQLite; ``sa.Boolean()`` compiles to ``BOOLEAN`` on
PG and ``INTEGER`` on SQLite.

Reversibility contract
----------------------

``upgrade()`` creates the table then its indexes; ``downgrade()`` drops
the indexes then the table in inverse order. Explicit index drops keep
the inverse symmetric across both dialects.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0058"
down_revision: str | None = "0057"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the ``runner_principal`` table and its indexes."""
    bind = op.get_bind()
    is_postgres = bind.dialect.name == "postgresql"

    op.create_table(
        "runner_principal",
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
        # Kill-switch owner: every runner must have an owner.
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
        "runner_principal_tenant_name_idx",
        "runner_principal",
        ["tenant_id", "name"],
        unique=True,
        postgresql_using="btree",
    )
    op.create_index(
        "runner_principal_keycloak_client_id_idx",
        "runner_principal",
        ["keycloak_client_id"],
        unique=True,
        postgresql_using="btree",
    )


def downgrade() -> None:
    """Drop the indexes then the ``runner_principal`` table."""
    op.drop_index(
        "runner_principal_keycloak_client_id_idx",
        table_name="runner_principal",
    )
    op.drop_index(
        "runner_principal_tenant_name_idx",
        table_name="runner_principal",
    )
    op.drop_table("runner_principal")
