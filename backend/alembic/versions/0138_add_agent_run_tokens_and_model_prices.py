# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Add token counts to ``agent_run`` and model prices to ``tenant`` (#5853).

Revision ID: 0138
Revises: 0137
Create Date: 2026-11-06

To compare models, each agent run needs its token counts, not only its
cost. This migration adds four nullable ``BIGINT`` columns to
``agent_run``: ``input_tokens``, ``output_tokens``, ``cache_read_tokens``
and ``cache_write_tokens``. Old rows read ``NULL``: their counts were
never stored.

A model that is not in MEHO's own price table costs 0. A tenant admin
can now give such a model a price. The prices live in the new nullable
JSON column ``tenant.agent_model_prices`` (``JSONB`` on PostgreSQL).
Old rows read ``NULL``: no prices.

All five columns are nullable, so no row is changed and no default is
needed. An older build does not know the columns and never reads them.

``downgrade()`` drops the five columns.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0138"
down_revision: str | None = "0137"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Alembic reads these names itself. List them in __all__, so the code-quality
# check does not report them as unused globals. Keep a plain list of strings
# with no type annotation: the check reads only that form.
__all__ = [
    "branch_labels",
    "depends_on",
    "down_revision",
    "downgrade",
    "revision",
    "upgrade",
]

_TOKEN_COLUMNS = ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens")
_PRICES_COLUMN = "agent_model_prices"


def upgrade() -> None:
    """Add the four token columns and the model price column."""
    for name in _TOKEN_COLUMNS:
        op.add_column("agent_run", sa.Column(name, sa.BigInteger(), nullable=True))
    op.add_column(
        "tenant",
        sa.Column(
            _PRICES_COLUMN,
            sa.JSON().with_variant(postgresql.JSONB(), "postgresql"),
            nullable=True,
        ),
    )


def downgrade() -> None:
    """Drop the model price column and the four token columns."""
    with op.batch_alter_table("tenant") as batch:
        batch.drop_column(_PRICES_COLUMN)
    with op.batch_alter_table("agent_run") as batch:
        for name in _TOKEN_COLUMNS:
            batch.drop_column(name)
