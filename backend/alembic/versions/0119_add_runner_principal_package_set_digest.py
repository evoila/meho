# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Add the runner's connector package set digest to ``runner_principal`` (#5038).

Revision ID: 0119
Revises: 0118
Create Date: 2026-10-28

One nullable column, ``runner_principal.package_set_digest``: the digest
of the connector packages a satellite runner sent on its last call. The
runner list shows it, so an operator can see a runner whose connector
packages differ from the central MEHO. Existing rows start as ``NULL`` and
get a value on the runner's next call. An older build never reads the
column.

``downgrade()`` drops the column.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0119"
down_revision: str | None = "0118"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add the package set digest column."""
    op.add_column("runner_principal", sa.Column("package_set_digest", sa.Text(), nullable=True))


def downgrade() -> None:
    """Drop the package set digest column."""
    op.drop_column("runner_principal", "package_set_digest")
