# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Store the people behind a request of an agent, service or runner login.

Revision ID: 0178
Revises: 0177
Create Date: 2026-11-18

Why
---

A request made by an agent, a service login or a runner counts as a
request of the people who set that login up, changed it or started its
run (``docs/codebase/approvals.md``, "The people behind a request").
A machine login never counts as such a person. MEHO needs five facts it
does not keep yet:

* ``approval_request.people_behind`` -- JSON, nullable. The list
  ``[{"sub", "name", "why"}]`` MEHO found when the request was made.
  NULL for a person's request.
* ``agent_definition.updated_by_sub`` -- Text, nullable. The person who
  last changed the agent definition. The upgrade fills it from the
  newest successful ``agent.edit`` audit row of the definition by a login
  that is not a machine login, and from ``created_by_sub`` when there is
  none. It reads only audit rows from the time the definition was created,
  so an edit of a deleted definition with the same name does not count.
  A machine login here is a row of ``machine_login`` or the login of an
  agent definition of the tenant.
* ``runbook_runs.reassigned_by`` -- Text, nullable. The person who last
  handed the run to a new assignee.
* ``run_approval.principal_client_id`` -- Text, nullable. The client id
  of the add-on login that asked for the run approval list. MEHO uses it
  to find the add-on pairing when a person approves the list. NULL on a
  list from before the upgrade; then MEHO finds the pairing by the login
  id alone.
* the table ``machine_login`` -- the login ids MEHO has seen as an agent,
  service or runner login, one row per tenant and id. The upgrade fills it
  from the waiting requests of such logins (``approval_request.principal_kind``)
  and from the ``client`` block of the audit rows, where MEHO kept one.

Reversibility
-------------

``downgrade()`` drops the four columns and the table. The filled values
are lost; the rows they came from stay.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0178"
down_revision: str | None = "0177"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Alembic reads these names itself. List them in __all__, so the code-quality
# check does not report them as unused globals.
__all__ = [
    "branch_labels",
    "depends_on",
    "down_revision",
    "downgrade",
    "fill_sql",
    "revision",
    "seed_sql",
    "upgrade",
]

#: The newest person who changed the definition, from the audit log. The
#: audit row of an edit keeps ``op_id`` and ``agent_name`` in its payload,
#: not the definition id. So MEHO reads only rows from the time the
#: definition was created: an edit of a deleted definition with the same
#: name is older and does not count.
_FILL_SQL = """
UPDATE agent_definition SET updated_by_sub = COALESCE(
    (
        SELECT audit_log.operator_sub FROM audit_log
        WHERE audit_log.tenant_id = agent_definition.tenant_id
          AND audit_log.status_code >= 200 AND audit_log.status_code < 300
          AND {op_id} = 'agent.edit'
          AND {agent_name} = agent_definition.name
          AND audit_log.occurred_at >= agent_definition.created_at
          AND audit_log.operator_sub NOT IN (
              SELECT machine_login.sub FROM machine_login
              WHERE machine_login.tenant_id = agent_definition.tenant_id
              UNION
              SELECT agent.identity_ref FROM agent_definition AS agent
              WHERE agent.tenant_id = agent_definition.tenant_id
          )
        ORDER BY audit_log.occurred_at DESC
        LIMIT 1
    ),
    agent_definition.created_by_sub
)
"""

#: The machine logins MEHO can see in its records: the requests of an
#: agent, service or runner login, and the ``client`` block of an audit row.
_SEED_SQL = """
INSERT INTO machine_login (tenant_id, sub, kind, first_seen_at)
SELECT seen.tenant_id, seen.sub, MIN(seen.kind), MIN(seen.seen_at) FROM (
    SELECT approval_request.tenant_id AS tenant_id, approval_request.principal_sub AS sub,
           approval_request.principal_kind AS kind, approval_request.created_at AS seen_at
    FROM approval_request
    WHERE approval_request.principal_kind IN ('agent', 'service', 'runner')
    UNION ALL
    SELECT audit_log.tenant_id, audit_log.operator_sub, {client_kind}, audit_log.occurred_at
    FROM audit_log
    WHERE audit_log.tenant_id IS NOT NULL
      AND {client_kind} IN ('agent', 'service', 'runner')
) AS seen
GROUP BY seen.tenant_id, seen.sub
"""

_CLIENT_KIND = {
    "postgresql": "audit_log.payload -> 'client' ->> 'principal_kind'",
    "sqlite": "json_extract(audit_log.payload, '$.client.principal_kind')",
}

_JSON_TEXT = {
    "postgresql": "audit_log.payload ->> '{key}'",
    "sqlite": "json_extract(audit_log.payload, '$.{key}')",
}


def fill_sql(dialect: str) -> str:
    """The fill statement of ``agent_definition.updated_by_sub`` for *dialect*."""
    pattern = _JSON_TEXT[dialect]
    return _FILL_SQL.format(
        op_id=pattern.format(key="op_id"), agent_name=pattern.format(key="agent_name")
    )


def seed_sql(dialect: str) -> str:
    """The fill statement of ``machine_login`` for *dialect*."""
    return _SEED_SQL.format(client_kind=_CLIENT_KIND[dialect])


def upgrade() -> None:
    """Add the four columns and the table, then fill them."""
    op.create_table(
        "machine_login",
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("sub", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("tenant_id", "sub"),
    )
    op.add_column("approval_request", sa.Column("people_behind", sa.JSON(), nullable=True))
    op.add_column("agent_definition", sa.Column("updated_by_sub", sa.Text(), nullable=True))
    op.add_column("runbook_runs", sa.Column("reassigned_by", sa.Text(), nullable=True))
    op.add_column("run_approval", sa.Column("principal_client_id", sa.Text(), nullable=True))
    dialect = op.get_bind().dialect.name
    op.execute(sa.text(seed_sql(dialect)))
    op.execute(sa.text(fill_sql(dialect)))


def downgrade() -> None:
    """Drop the four columns and the table."""
    op.drop_column("run_approval", "principal_client_id")
    op.drop_column("runbook_runs", "reassigned_by")
    op.drop_column("agent_definition", "updated_by_sub")
    op.drop_column("approval_request", "people_behind")
    op.drop_table("machine_login")
