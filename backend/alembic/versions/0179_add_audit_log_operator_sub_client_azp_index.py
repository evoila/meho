# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Index ``audit_log`` by person, login client and time.

Revision ID: 0179
Revises: 0178
Create Date: 2026-11-16

Why
---

The "Your tools" section of the account page shows the last use of each
tool: the newest audit row of the signed-in person for that tool's login
client. The client is in the row's ``client`` block
(``payload -> 'client' ->> 'azp'``). With this index, that read finds the
newest row of one (person, client) pair directly, however many rows the
person has. Without it, the read walks all newer rows of the person.

* ``audit_log_operator_sub_client_azp_occurred_at_idx`` -- non-unique
  b-tree on ``(operator_sub, ((payload -> 'client') ->> 'azp'),
  occurred_at)``. The query must use the same expression:
  :data:`meho_backplane.db.models.AUDIT_LOG_CLIENT_AZP`.

Upgrade
-------

``CREATE INDEX`` reads the whole ``audit_log`` table. While the index is
built, calls that write an audit row wait. Measured on PostgreSQL 16:
about 7 seconds for 3.3 million rows. The time grows with the size of
the table. Reads do not wait.

Reversibility
-------------

``downgrade()`` drops the index. No data changes.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0179"
down_revision: str | None = "0178"
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

_TABLE = "audit_log"
_INDEX = "audit_log_operator_sub_client_azp_occurred_at_idx"
# The same expression as ``AUDIT_LOG_CLIENT_AZP`` in ``db/models.py``.
# PostgreSQL (JSONB) and SQLite (3.38 and newer) both know it.
_CLIENT_AZP = "((payload -> 'client') ->> 'azp')"


def upgrade() -> None:
    """Create the person, client and time index."""
    op.create_index(_INDEX, _TABLE, ["operator_sub", sa.text(_CLIENT_AZP), "occurred_at"])


def downgrade() -> None:
    """Drop the person, client and time index."""
    op.drop_index(_INDEX, table_name=_TABLE)
