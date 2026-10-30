# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Add the service-principal read scope table (#4992).

Revision ID: 0117
Revises: 0116
Create Date: 2026-10-27

One additive table, ``service_principal_read_scope``: one row per
``(tenant_id, principal_sub)`` holding the JSON list of targets a service
principal may read. A principal without a row is not limited, so an
existing install behaves as before until a person sets a scope. An older
build never reads the table, so after a rollback every service principal
reads every target again, as it did before this release.

``downgrade()`` drops the table.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0117"
down_revision: str | None = "0116"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "service_principal_read_scope"


def upgrade() -> None:
    """Create the read scope table."""
    op.create_table(
        _TABLE,
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("tenant_id", sa.Uuid(), sa.ForeignKey("tenant.id"), nullable=False),
        sa.Column("principal_sub", sa.Text(), nullable=False),
        sa.Column(
            "entries",
            sa.JSON().with_variant(postgresql.JSONB(), "postgresql"),
            nullable=False,
        ),
        sa.Column("set_by_sub", sa.Text(), nullable=False),
        sa.Column("set_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("tenant_id", "principal_sub", name=f"uq_{_TABLE}_principal"),
    )


def downgrade() -> None:
    """Drop the read scope table."""
    op.drop_table(_TABLE)
