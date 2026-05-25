# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Add ``audit_log.actor_sub`` for RFC 8693 actor (delegation) attribution.

Revision ID: 0021
Revises: 0020
Create Date: 2026-05-25

Schema foundation of Task #816 (G11.2-T2) under Initiative #803. When a
human triggers an agent run, the synchronous audit row must record *both*
the human initiator (``operator_sub``) and the agent that acted
(``actor_sub``) -- the RFC 8693 ``sub`` + ``act`` two-claim shape. Keycloak
has no delegation token exchange (keycloak#38279 is open), so MEHO
synthesises the binding at the resource server: the acting agent's
principal is bound into the audit context for the agent run's lifetime and
read at each audit write path.

What this migration adds
------------------------

* ``audit_log.actor_sub text`` -- nullable. The acting agent's principal
  reference (the ``AgentDefinition.identity_ref``). Populated only on audit
  rows produced *during a user-initiated agent run*; ``NULL`` for direct
  human requests and for autonomous (``client_credentials``) agent runs,
  where the agent is the subject (``operator_sub``) and there is no separate
  actor. No backfill -- pre-G11.2 rows stay ``NULL``.
* ``audit_log_actor_sub_idx`` -- b-tree index on the new column, for
  "what did agent X do on behalf of whom?" audit queries.

Why no foreign key / NOT NULL in v0.2
-------------------------------------

Same soft-column discipline as ``agent_session_id`` (0014),
``tenant_id`` (0002), and ``target_id`` (0004): nullable, no FK clause, so
the migration is trivially reversible on a populated table. ``actor_sub``
mirrors the opaque-reference shape of ``AgentDefinition.identity_ref`` (a
soft reference, not a row id), so there is no table to point a FK at.

Reversibility contract
----------------------

``downgrade()`` reverses ``upgrade()`` in order: drop the index, then the
column. Pure generic SQLAlchemy DDL, portable across PG and SQLite.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0021"
down_revision: str | None = "0020"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add ``audit_log.actor_sub`` column + named index."""
    op.add_column(
        "audit_log",
        sa.Column("actor_sub", sa.Text(), nullable=True),
    )
    op.create_index(
        "audit_log_actor_sub_idx",
        "audit_log",
        ["actor_sub"],
        postgresql_using="btree",
    )


def downgrade() -> None:
    """Reverse the upgrade -- drop the index and column."""
    op.drop_index("audit_log_actor_sub_idx", table_name="audit_log")
    op.drop_column("audit_log", "actor_sub")
