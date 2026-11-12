# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Give every existing agent one explicit "read everything" grant.

Revision ID: 0155
Revises: 0154
Create Date: 2026-11-10

Data only, no schema change. Today an agent login with no matching grant
runs ``safe`` operations at once and every other risk level waits for a
person's approval. A later switch (``AGENT_GRANT_REQUIRED``) can make
"no grant" mean "no access". So that existing agents keep working when
the switch turns on, each one gets this row in ``agent_permission``::

    op_pattern="*", target_scope="*", verdict="auto-execute"

The risk-level ceiling keeps every level above ``safe`` at "waits for
approval", so the row gives the same answers as having no row. A more
specific row still wins, and an existing ``*`` row on one target ties
with this row, so the stricter verdict wins: an existing ``deny`` stays
a ``deny``.

Who gets the row, per tenant:

* the ``keycloak_client_id`` of every agent principal that is not
  revoked (the key that grants use);
* every other ``principal_sub`` that already has a row in
  ``agent_permission`` (for example a person's agent login), unless it
  is the client id of a revoked agent principal.

``created_by_sub`` is the marker ``meho:upgrade-read-everything``, so
``meho agent grant list`` shows where the row came from, and an admin
can remove it like any other grant. A principal that already has the
key ``(tenant, sub, "*", "*")`` is skipped, so the upgrade is safe to
run twice.

``downgrade()`` deletes only the rows with the marker.
"""

from collections.abc import Sequence
from datetime import UTC, datetime
from uuid import UUID, uuid4

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0155"
down_revision: str | None = "0154"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Alembic reads these names itself. List them in __all__, so the code-quality
# check does not report them as unused globals.
__all__ = [
    "UPGRADE_MARKER",
    "add_upgrade_grants",
    "branch_labels",
    "depends_on",
    "down_revision",
    "downgrade",
    "revision",
    "upgrade",
]

UPGRADE_MARKER = "meho:upgrade-read-everything"

_permission = sa.table(
    "agent_permission",
    sa.column("id", sa.Uuid()),
    sa.column("tenant_id", sa.Uuid()),
    sa.column("principal_sub", sa.Text()),
    sa.column("op_pattern", sa.Text()),
    sa.column("target_scope", sa.Text()),
    sa.column("verdict", sa.Text()),
    sa.column("created_by_sub", sa.Text()),
    sa.column("created_at", sa.DateTime(timezone=True)),
    sa.column("updated_at", sa.DateTime(timezone=True)),
)

_principal = sa.table(
    "agent_principal",
    sa.column("tenant_id", sa.Uuid()),
    sa.column("keycloak_client_id", sa.Text()),
    sa.column("revoked", sa.Boolean()),
)


def _principals_to_grant(conn: sa.Connection) -> set[tuple[UUID, str]]:
    """Return the ``(tenant_id, principal_sub)`` pairs that need the row."""
    live: set[tuple[UUID, str]] = set()
    revoked: set[tuple[UUID, str]] = set()
    principal_rows = conn.execute(
        sa.select(_principal.c.tenant_id, _principal.c.keycloak_client_id, _principal.c.revoked)
    )
    for tenant_id, client_id, is_revoked in principal_rows:
        (revoked if is_revoked else live).add((tenant_id, client_id))

    permission_rows = conn.execute(
        sa.select(_permission.c.tenant_id, _permission.c.principal_sub).distinct()
    )
    with_rows = {(tenant_id, sub) for tenant_id, sub in permission_rows}

    covered = conn.execute(
        sa.select(_permission.c.tenant_id, _permission.c.principal_sub).where(
            _permission.c.op_pattern == "*",
            _permission.c.target_scope == "*",
        )
    )
    already = {(tenant_id, sub) for tenant_id, sub in covered}

    return (live | (with_rows - revoked)) - already


def add_upgrade_grants(conn: sa.Connection) -> int:
    """Insert the "read everything" row where it is missing; return the count."""
    now = datetime.now(UTC)
    rows = [
        {
            "id": uuid4(),
            "tenant_id": tenant_id,
            "principal_sub": sub,
            "op_pattern": "*",
            "target_scope": "*",
            "verdict": "auto-execute",
            "created_by_sub": UPGRADE_MARKER,
            "created_at": now,
            "updated_at": now,
        }
        for tenant_id, sub in _principals_to_grant(conn)
    ]
    if rows:
        conn.execute(_permission.insert(), rows)
    return len(rows)


def upgrade() -> None:
    """Give each existing agent the explicit "read everything" row."""
    add_upgrade_grants(op.get_bind())


def downgrade() -> None:
    """Delete only the rows this migration added."""
    op.get_bind().execute(
        sa.delete(_permission).where(_permission.c.created_by_sub == UPGRADE_MARKER)
    )
