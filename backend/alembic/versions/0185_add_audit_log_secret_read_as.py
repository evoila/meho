# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Add ``audit_log.secret_read_as``.

Revision ID: 0185
Revises: 0184
Create Date: 2026-11-22

One nullable ``Text`` column, with no default. It holds one of these values:

* the id of the login that a secret store accepted for the call;
* ``meho`` when the store saw MEHO's own identity;
* ``check-runner`` when the check runner read the secret and its token has
  no readable id;
* ``kept-login:reader-unknown`` when the call used a login MEHO kept in
  memory and it is not known whose login read that secret.

It is empty when the call read no secret.

Old rows
--------

The migration runs no ``UPDATE``, so the ``audit_log`` append-only trigger
(``0100`` / ``0103``) does not fire. Adding a nullable column without a
default only changes the catalog. Rows that exist already read as empty.

Rolling upgrades
----------------

An older MEHO version that runs during a rolling upgrade does not know the
column. Its rows leave it empty. ``downgrade()`` drops the column;
``upgrade()`` only adds, so the migration compat guard accepts it.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0185"
down_revision: str | None = "0184"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Alembic reads these names itself. List them in __all__, so the code-quality
# check does not report them as unused globals.
__all__ = [
    "branch_labels",
    "depends_on",
    "down_revision",
    "downgrade",
    "revision",
    "upgrade",
]


def upgrade() -> None:
    """Add the column."""
    op.add_column("audit_log", sa.Column("secret_read_as", sa.Text(), nullable=True))


def downgrade() -> None:
    """Drop the column."""
    op.drop_column("audit_log", "secret_read_as")
