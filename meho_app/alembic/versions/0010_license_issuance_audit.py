# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""Add license_issuance audit table.

Records every signed enterprise token minted by
``scripts/issue-license.py`` (Initiative #505 Task #519, not yet shipped)
for compliance and forensics. ``license_id`` is the primary key, so
duplicate issuances fail at the database boundary; the matching
repository class at ``meho_app/modules/licensing/audit.py`` translates
the SQLSTATE 23505 violation into ``DuplicateLicenseIDError``.

``revoked_at`` / ``revocation_reason`` columns are reserved for future
revocation tooling -- ship the schema once so revocation lands without
a migration. Not in scope for this migration.

Revision ID: 0010_license_issuance_audit
Revises: 0009_doc_family
Create Date: 2026-05-03
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0010_license_issuance_audit"
down_revision = "0009_doc_family"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "license_issuance",
        sa.Column("license_id", sa.Text(), primary_key=True, nullable=False),
        sa.Column("org", sa.Text(), nullable=False),
        sa.Column("tier", sa.Text(), nullable=False),
        sa.Column("features", postgresql.JSONB, nullable=False),
        sa.Column("issued_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("expires_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("max_tenants", sa.Integer(), nullable=True),
        sa.Column("issuer", sa.Text(), nullable=False),
        sa.Column("issuer_type", sa.Text(), nullable=False),
        sa.Column("revoked_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("revocation_reason", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
    )
    # Composite index matches the only query pattern this module ships:
    # `list_by_org` filters by org and orders by issued_at. Postgres can
    # also use the leading column to satisfy filter-only queries on org,
    # so a separate single-column org index would be redundant.
    op.create_index(
        "ix_license_issuance_org_issued_at",
        "license_issuance",
        ["org", "issued_at"],
    )
    # Cross-org date-range queries ("all issuances last quarter") are a
    # likely v0.2 reporting use case and need the standalone issued_at
    # index since they don't filter by org.
    op.create_index(
        "ix_license_issuance_issued_at",
        "license_issuance",
        ["issued_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_license_issuance_issued_at", table_name="license_issuance")
    op.drop_index("ix_license_issuance_org_issued_at", table_name="license_issuance")
    op.drop_table("license_issuance")
