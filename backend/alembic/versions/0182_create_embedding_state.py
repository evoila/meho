# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Create ``embedding_state``: which embedding model made the stored vectors.

Revision ID: 0182
Revises: 0181
Create Date: 2026-11-22

One table with one row (``id = 1``): the model name, the vector size and
the time of the last change. The upgrade writes the model the install runs
now (``RETRIEVAL_EMBEDDING_MODEL``, or the shipped default). It embeds
nothing. MEHO compares the row with the setting at every start, and the
command ``python -m meho_backplane.retrieval.reembed`` updates it.

Reversibility
-------------

Purely additive. An older image never reads the table, so a rollback is
safe. ``downgrade()`` drops the table.
"""

import os
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0182"
down_revision: str | None = "0181"
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

_TABLE = "embedding_state"

# The shipped default model and the size of ``documents.embedding``, as
# literals: a migration never imports application code.
_DEFAULT_MODEL = "BAAI/bge-small-en-v1.5"
_DIMENSION = 384


def upgrade() -> None:
    """Create the table and store the model this install runs now."""
    op.create_table(
        _TABLE,
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=False),
        sa.Column("model", sa.Text(), nullable=False),
        sa.Column("dimension", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("id = 1", name="ck_embedding_state_single_row"),
    )
    model = os.environ.get("RETRIEVAL_EMBEDDING_MODEL") or _DEFAULT_MODEL
    op.execute(
        sa.text(
            "INSERT INTO embedding_state (id, model, dimension, updated_at) "
            "VALUES (1, :model, :dimension, CURRENT_TIMESTAMP)"
        ).bindparams(model=model, dimension=_DIMENSION)
    )


def downgrade() -> None:
    """Drop the table."""
    op.drop_table(_TABLE)
