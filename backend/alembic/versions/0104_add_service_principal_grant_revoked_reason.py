# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Add ``revoked_reason`` to ``service_principal_grant`` (#3489).

Revision ID: 0104
Revises: 0103
Create Date: 2026-10-15

Creating a service-principal grant requires a ``reason``. Revoking one
recorded no reason at all. This adds one nullable ``Text`` column next to
``revoked_by_sub`` so the optional revoke reason is kept on the grant row.

The column is nullable with no backfill. Existing rows read back NULL,
which means "no reason was given", so nothing changes for them.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0104"
down_revision: str | None = "0103"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "service_principal_grant",
        sa.Column("revoked_reason", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("service_principal_grant", "revoked_reason")
