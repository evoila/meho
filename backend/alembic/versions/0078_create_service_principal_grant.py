# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Create the ``service_principal_grant`` table (#3151 / #3152).

An operator-managed **standing scoped auto-approval grant** for a
non-agent **service principal** (OAuth2 client-credentials, ``principal_kind
= service``). One row authorises exactly one
``(principal_sub, op_id, connector_id, target_id)`` tuple in a tenant to
run **unattended** — the non-agent policy gate
(:func:`meho_backplane.operations._validate._non_agent_verdict`) consults
these rows for a service principal that would otherwise park a
``requires_approval`` op or a mutating ``caution`` / ``dangerous`` op
(#3152 option 1). No wildcards on ``principal_sub`` or ``op_id``: creating
a grant IS the operator's upfront review, so every scope is explicit.

Additive-only (migration-compat contract): a fresh table plus its
indexes, no ALTER of an existing object.

Uniqueness is enforced by two **partial** unique indexes rather than one
constraint because ``target_id`` is nullable (a targetless / tenant-wide
op keys ``target_id IS NULL``) and Postgres treats ``NULL != NULL``, so a
single unique index would not catch duplicate targetless grants. Both
indexes are scoped to ``revoked_at IS NULL`` so a revoked grant does not
block re-granting the same scope (revocation is a soft-delete — the row
is retained for audit visibility).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0078"
down_revision: str | None = "0077"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    is_postgres = bind.dialect.name == "postgresql"

    op.create_table(
        "service_principal_grant",
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("tenant_id", sa.Uuid(), sa.ForeignKey("tenant.id"), nullable=False),
        sa.Column("principal_sub", sa.Text(), nullable=False),
        sa.Column("op_id", sa.Text(), nullable=False),
        sa.Column("connector_id", sa.Text(), nullable=False),
        # Nullable: a targetless / tenant-wide op keys ``target_id IS NULL``.
        sa.Column("target_id", sa.Uuid(), nullable=True),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("created_by_sub", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()") if is_postgres else None,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_by_sub", sa.Text(), nullable=True),
    )

    # Dominant dispatch-time query: "a live grant for principal P, op O in
    # tenant T" (connector_id + target_id are matched in the WHERE tail).
    op.create_index(
        "service_principal_grant_lookup_idx",
        "service_principal_grant",
        ["tenant_id", "principal_sub", "op_id"],
        postgresql_using="btree",
    )
    # At most one ACTIVE grant per fully-scoped key. Two partial indexes so
    # the nullable ``target_id`` does not defeat uniqueness on the
    # targetless case (NULL != NULL in Postgres).
    op.create_index(
        "uq_service_principal_grant_targeted",
        "service_principal_grant",
        ["tenant_id", "principal_sub", "op_id", "connector_id", "target_id"],
        unique=True,
        postgresql_where=sa.text("target_id IS NOT NULL AND revoked_at IS NULL"),
        sqlite_where=sa.text("target_id IS NOT NULL AND revoked_at IS NULL"),
    )
    op.create_index(
        "uq_service_principal_grant_targetless",
        "service_principal_grant",
        ["tenant_id", "principal_sub", "op_id", "connector_id"],
        unique=True,
        postgresql_where=sa.text("target_id IS NULL AND revoked_at IS NULL"),
        sqlite_where=sa.text("target_id IS NULL AND revoked_at IS NULL"),
    )
    # Drives listing / dispatch filtering of still-live grants.
    op.create_index(
        "service_principal_grant_expires_at_idx",
        "service_principal_grant",
        ["expires_at"],
        postgresql_using="btree",
    )


def downgrade() -> None:
    op.drop_index(
        "service_principal_grant_expires_at_idx",
        table_name="service_principal_grant",
    )
    op.drop_index(
        "uq_service_principal_grant_targetless",
        table_name="service_principal_grant",
    )
    op.drop_index(
        "uq_service_principal_grant_targeted",
        table_name="service_principal_grant",
    )
    op.drop_index(
        "service_principal_grant_lookup_idx",
        table_name="service_principal_grant",
    )
    op.drop_table("service_principal_grant")
