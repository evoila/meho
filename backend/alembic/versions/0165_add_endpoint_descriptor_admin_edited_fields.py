# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Remember which fields an admin edited on an operation.

Revision ID: 0165
Revises: 0164
Create Date: 2026-11-13

Adds one nullable JSON column, ``endpoint_descriptor.admin_edited_fields``:
the list of fields (``custom_description``, ``llm_instructions``) that an
admin edited. Typed registration at start leaves those fields alone, so
the edit survives a restart. Existing rows keep ``NULL``. An older image
never reads the column, so a rollback is safe.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

# revision identifiers, used by Alembic.
revision: str = "0165"
down_revision: str | None = "0164"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

__all__ = [
    "branch_labels",
    "depends_on",
    "down_revision",
    "downgrade",
    "revision",
    "upgrade",
]

_JSON = sa.JSON().with_variant(JSONB(), "postgresql")


def upgrade() -> None:
    """Add the admin-edited fields column."""
    op.add_column("endpoint_descriptor", sa.Column("admin_edited_fields", _JSON, nullable=True))


def downgrade() -> None:
    """Drop the admin-edited fields column."""
    op.drop_column("endpoint_descriptor", "admin_edited_fields")
