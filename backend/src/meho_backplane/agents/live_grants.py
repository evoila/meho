# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The live grants of an agent, shown when MEHO asks for no starting grant.

MEHO needs no starting grant pick for an agent definition whose agent
already has a live grant (:mod:`meho_backplane.agents.grant_sets`). So a
person still sees what the agent may do, the create and edit answers of
REST, MCP and the CLI, and the console form, list the agent's live
grants. A grant is live when it has no end date or its end date is in the
future. A grant that has run out is not live and is not listed.

The list holds at most :data:`MAX_LIVE_GRANTS` grants. When the agent has
more, ``truncated`` is true, so a cut list never looks like the whole list.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Final

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AgentPermission, Target

__all__ = [
    "MAX_LIVE_GRANTS",
    "LiveGrant",
    "LiveGrants",
    "load_live_grants",
    "read_live_grants",
]

#: The most grants one answer lists.
MAX_LIVE_GRANTS: Final[int] = 100


class LiveGrant(BaseModel):
    """One live grant of an agent."""

    model_config = ConfigDict(frozen=True)

    id: uuid.UUID
    op_pattern: str = Field(description="The operations the grant covers ('*' = every operation).")
    target_scope: str = Field(description="'*' for every target, else the id of one target.")
    target_name: str | None = Field(
        description="The name of the target; null for every target or a target MEHO cannot find."
    )
    verdict: str = Field(description="auto-execute, needs-approval or deny.")
    expires_at: datetime | None = Field(description="The end date; null when the grant has none.")


class LiveGrants(BaseModel):
    """The live grants of an agent, and whether the list was cut."""

    model_config = ConfigDict(frozen=True)

    grants: list[LiveGrant]
    truncated: bool = Field(
        description=f"True when the agent has more than {MAX_LIVE_GRANTS} live grants "
        "and the list shows only the first ones."
    )


async def read_live_grants(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    principal_sub: str,
) -> LiveGrants:
    """Return the live grants of *principal_sub* in *tenant_id*, in the caller's session."""
    result = await session.execute(
        select(AgentPermission)
        .where(
            AgentPermission.tenant_id == tenant_id,
            AgentPermission.principal_sub == principal_sub,
            (AgentPermission.expires_at.is_(None))
            | (AgentPermission.expires_at > datetime.now(UTC)),
        )
        .order_by(AgentPermission.op_pattern, AgentPermission.target_scope)
        .limit(MAX_LIVE_GRANTS + 1)
    )
    rows = list(result.scalars().all())
    truncated = len(rows) > MAX_LIVE_GRANTS
    rows = rows[:MAX_LIVE_GRANTS]
    names = await _target_names(session, tenant_id, [row.target_scope for row in rows])
    grants = [
        LiveGrant(
            id=row.id,
            op_pattern=row.op_pattern,
            target_scope=row.target_scope,
            target_name=names.get(row.target_scope.lower()),
            verdict=row.verdict,
            expires_at=_as_utc(row.expires_at),
        )
        for row in rows
    ]
    return LiveGrants(grants=grants, truncated=truncated)


async def load_live_grants(tenant_id: uuid.UUID, principal_sub: str) -> LiveGrants:
    """Return the live grants of *principal_sub*, in a session of its own."""
    async with get_sessionmaker()() as session:
        return await read_live_grants(session, tenant_id, principal_sub)


async def _target_names(
    session: AsyncSession, tenant_id: uuid.UUID, scopes: list[str]
) -> dict[str, str]:
    """Map each target id in *scopes* to its name, within *tenant_id*."""
    ids: list[uuid.UUID] = []
    for scope in scopes:
        try:
            ids.append(uuid.UUID(scope))
        except ValueError:
            continue
    if not ids:
        return {}
    result = await session.execute(
        select(Target.id, Target.name).where(Target.tenant_id == tenant_id, Target.id.in_(ids))
    )
    return {str(target_id): name for target_id, name in result.all()}


def _as_utc(value: datetime | None) -> datetime | None:
    # SQLite gives back a naive datetime; the column stores UTC.
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value
