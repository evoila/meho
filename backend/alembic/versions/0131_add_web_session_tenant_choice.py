# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Add ``web_session.tenant_chosen`` and ``web_session.can_switch_tenant`` (#5240).

Revision ID: 0131
Revises: 0130
Create Date: 2026-10-31

Two boolean columns for the web interface's current tenant:

* ``tenant_chosen`` -- ``false`` while a person with several tenants has
  not picked one yet after login. The web interface then shows only the
  tenant picker. Existing rows get ``true``: they were made for one
  tenant, as before.
* ``can_switch_tenant`` -- ``true`` when the person has more than one
  tenant or is a platform admin. The sidebar then shows the tenant box as
  a switch. Existing rows get ``false``: the sidebar looks as before until
  the next login.

Both are ``NOT NULL`` with a server default, so the add is safe on a
filled table and needs no backfill. An older build never reads them.

``downgrade()`` drops both columns.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0131"
down_revision: str | None = "0130"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add the two tenant-choice columns to ``web_session``."""
    op.add_column(
        "web_session",
        sa.Column("tenant_chosen", sa.Boolean(), nullable=False, server_default=sa.true()),
    )
    op.add_column(
        "web_session",
        sa.Column("can_switch_tenant", sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade() -> None:
    """Drop the two tenant-choice columns."""
    op.drop_column("web_session", "can_switch_tenant")
    op.drop_column("web_session", "tenant_chosen")
