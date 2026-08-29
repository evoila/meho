# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Create the ``addon_pairing`` table (#3025).

The registry of *active* add-on pairings — the foundation of the add-on
pairing contract (Initiative #2900). One row is one live pairing between the
backplane and a sibling add-on product: a Keycloak client-credentials
service principal plus the negotiated integration-contract version. Unpair
hard-deletes the row (an unpaired backplane is byte-identical to a
never-paired one), so the table only ever holds live pairings; the
append-only ``audit_log`` keeps the pair/unpair history.

Additive-only (migration-compat contract): a fresh table plus its two
unique indexes, no ALTER of an existing object.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0079"
down_revision: str | None = "0078"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    is_postgres = bind.dialect.name == "postgresql"

    op.create_table(
        "addon_pairing",
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("tenant_id", sa.Uuid(), sa.ForeignKey("tenant.id"), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("keycloak_client_id", sa.Text(), nullable=False),
        sa.Column("keycloak_internal_id", sa.Text(), nullable=False),
        sa.Column("owner_sub", sa.Text(), nullable=False),
        sa.Column("contract_version", sa.Integer(), nullable=False),
        sa.Column("addon_contract_version", sa.Integer(), nullable=False),
        sa.Column("addon_min_backplane_version", sa.Integer(), nullable=False),
        sa.Column("created_by_sub", sa.Text(), nullable=False),
        sa.Column(
            "paired_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()") if is_postgres else None,
        ),
        # NULL until the add-on's liveness heartbeat first stamps it.
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()") if is_postgres else None,
        ),
    )

    # Per-tenant name uniqueness + the tenant-scoped list / by-name lookup.
    op.create_index(
        "addon_pairing_tenant_name_idx",
        "addon_pairing",
        ["tenant_id", "name"],
        unique=True,
        postgresql_using="btree",
    )
    # Globally-unique Keycloak clientId (no per-tenant client-id namespace).
    op.create_index(
        "addon_pairing_keycloak_client_id_idx",
        "addon_pairing",
        ["keycloak_client_id"],
        unique=True,
        postgresql_using="btree",
    )


def downgrade() -> None:
    op.drop_index(
        "addon_pairing_keycloak_client_id_idx",
        table_name="addon_pairing",
    )
    op.drop_index(
        "addon_pairing_tenant_name_idx",
        table_name="addon_pairing",
    )
    op.drop_table("addon_pairing")
