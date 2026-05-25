# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Add ``audit_log.actor_sub`` for RFC 8693 delegation attribution.

Revision ID: 0019
Revises: 0018
Create Date: 2026-05-25

This migration is the schema foundation of G11.2-T2 (#816): RFC 8693
token-exchange delegation for agent runs. It adds the nullable
``audit_log.actor_sub`` column that records **who acted** when a token
was produced by a delegation exchange (``sub``=user, ``act``=agent), so
every agent-initiated audit row is attributable to the human who
triggered the run.

Background
----------

RFC 8693 token exchange (GA in Keycloak 26.2) produces a delegated
token whose ``sub`` claim is the *initiating user's* Keycloak sub and
whose ``act.sub`` claim is the *acting agent's* Keycloak sub. MEHO's
existing ``audit_log.operator_sub`` column captures the subject (the
user); ``actor_sub`` captures the actor (the agent). Autonomous
(cron / no-human) runs use ``client_credentials`` directly — the agent
is both subject and actor — and leave ``actor_sub`` NULL.

What this migration adds
------------------------

* ``audit_log.actor_sub text`` — nullable. The RFC 8693 ``act.sub``
  claim: the acting agent's Keycloak ``sub``. NULL for every direct-
  user request and every ``client_credentials`` run. Populated by
  :func:`~meho_backplane.audit.AuditMiddleware` (chassis HTTP),
  :func:`~meho_backplane.mcp.audit.write_mcp_audit_row` (MCP), and
  :func:`~meho_backplane.operations._audit.write_audit_row`
  (dispatcher) when ``Operator.actor_sub`` is set.
* ``audit_log_actor_sub_idx`` — b-tree index on ``actor_sub``.
  Drives ``WHERE actor_sub = ?`` queries (e.g. "all actions by this
  agent across all tenants in the window").

Why nullable, no FK clause
---------------------------

Same soft-FK discipline as every other nullable column on
``audit_log`` (``tenant_id`` added in 0002, ``target_id`` in 0004,
``parent_audit_id`` in 0006, ``agent_session_id`` in 0014): keeping
the column nullable makes the migration trivially reversible on a
populated table and defers any tightening to a dedicated future
migration. There is no ``agent`` table to point a FK at yet
(G11.2-T1 #815 ships that substrate); the Keycloak ``sub`` is the
stable issuer-managed identifier, not a row in this schema.

Reversibility contract
----------------------

``downgrade()`` undoes everything ``upgrade()`` created in reverse
order: drop the actor_sub index → drop the actor_sub column. Mirrors
the 0014 discipline exactly.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0019"
down_revision: str | None = "0018"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add ``audit_log.actor_sub`` column + named b-tree index."""
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
    """Reverse the upgrade — drop the index and column."""
    op.drop_index("audit_log_actor_sub_idx", table_name="audit_log")
    op.drop_column("audit_log", "actor_sub")
