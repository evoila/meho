# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Write the matched policy rule on audit rows and approval requests.

Revision ID: 0167
Revises: 0166
Create Date: 2026-11-13

Four nullable columns, so anyone who reads the audit log or an approval
request can see which policy rule matched the call:

* ``audit_log.policy_rule`` (text) and ``audit_log.policy_version``
  (integer).
* ``approval_request.policy_rule`` (JSON, JSONB on PostgreSQL) and
  ``approval_request.policy_version`` (integer).

Rows that exist already keep ``NULL``.

The append-only trigger on ``audit_log`` (migrations ``0100`` and
``0103``) fires for each row on ``UPDATE`` and ``DELETE``. It does not
fire for ``ALTER TABLE``, so adding a column works. A nullable column
with no default does not rewrite the rows either. The trigger compares
whole rows, so it covers the new columns too.

Only adds columns. An older image never reads them, so a rollback is
safe. ``downgrade()`` drops them.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

# revision identifiers, used by Alembic.
revision: str = "0167"
down_revision: str | None = "0166"
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

_JSON = sa.JSON().with_variant(JSONB(), "postgresql")


def upgrade() -> None:
    """Add the four nullable columns."""
    op.add_column("audit_log", sa.Column("policy_rule", sa.Text(), nullable=True))
    op.add_column("audit_log", sa.Column("policy_version", sa.Integer(), nullable=True))
    op.add_column("approval_request", sa.Column("policy_rule", _JSON, nullable=True))
    op.add_column("approval_request", sa.Column("policy_version", sa.Integer(), nullable=True))


def downgrade() -> None:
    """Drop the four columns."""
    op.drop_column("approval_request", "policy_version")
    op.drop_column("approval_request", "policy_rule")
    op.drop_column("audit_log", "policy_version")
    op.drop_column("audit_log", "policy_rule")
