# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Store the people behind a request of an agent, service or runner login.

Revision ID: 0176
Revises: 0175
Create Date: 2026-11-18

Why
---

A request made by an agent, a service login or a runner counts as a
request of the people who set that login up, changed it or started its
run (``docs/codebase/approvals.md``, "The people behind a request").
MEHO needs four facts it does not keep yet:

* ``approval_request.people_behind`` -- JSON, nullable. The list
  ``[{"sub", "name", "why"}]`` MEHO found when the request was made.
  NULL for a person's request.
* ``agent_definition.updated_by_sub`` -- Text, nullable. The person who
  last changed the agent definition. The upgrade fills it from the
  newest successful ``agent.edit`` audit row of the definition, and from
  ``created_by_sub`` when there is none. It reads only audit rows from the
  time the definition was created, so an edit of a deleted definition with
  the same name does not count.
* ``runbook_runs.reassigned_by`` -- Text, nullable. The person who last
  handed the run to a new assignee.
* ``run_approval.principal_client_id`` -- Text, nullable. The client id
  of the add-on login that asked for the run approval list. MEHO uses it
  to find the add-on pairing when a person approves the list. NULL on a
  list from before the upgrade; then MEHO finds the pairing by the login
  id alone.

Reversibility
-------------

``downgrade()`` drops the four columns. The filled values are lost; the
audit rows they came from stay.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0176"
down_revision: str | None = "0175"
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
        ORDER BY audit_log.occurred_at DESC
        LIMIT 1
    ),
    agent_definition.created_by_sub
)
"""

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


def upgrade() -> None:
    """Add the four columns and fill ``updated_by_sub``."""
    op.add_column("approval_request", sa.Column("people_behind", sa.JSON(), nullable=True))
    op.add_column("agent_definition", sa.Column("updated_by_sub", sa.Text(), nullable=True))
    op.add_column("runbook_runs", sa.Column("reassigned_by", sa.Text(), nullable=True))
    op.add_column("run_approval", sa.Column("principal_client_id", sa.Text(), nullable=True))
    op.execute(sa.text(fill_sql(op.get_bind().dialect.name)))


def downgrade() -> None:
    """Drop the four columns."""
    op.drop_column("run_approval", "principal_client_id")
    op.drop_column("runbook_runs", "reassigned_by")
    op.drop_column("agent_definition", "updated_by_sub")
    op.drop_column("approval_request", "people_behind")
