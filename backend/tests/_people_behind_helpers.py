# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Give a machine login in a test a known person behind it.

A request of an agent, service or runner login with nobody known behind it
needs an Admin to approve it (:mod:`meho_backplane.operations.people_behind`).
A test that parks such a request and approves it as another person gives the
login a known person first. The rows touch an operation no test runs.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select

from meho_backplane.auth.operator import Operator, PrincipalKind
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AgentPermission, ServicePrincipalGrant

#: The person behind the login in these tests. No test approves as them.
GRANT_GIVER = "grant-giver-sub"

#: An operation no test runs, so the rows change no call.
_UNUSED_OP = "people-behind.unused"


async def give_agent_a_known_person(
    tenant_id: uuid.UUID, principal_sub: str, giver: str = GRANT_GIVER
) -> None:
    """Store an agent grant for *principal_sub* that *giver* gave."""
    async with get_sessionmaker()() as session:
        found = await session.scalar(
            select(AgentPermission.id).where(
                AgentPermission.tenant_id == tenant_id,
                AgentPermission.principal_sub == principal_sub,
                AgentPermission.op_pattern == _UNUSED_OP,
            )
        )
        if found is not None:
            return
        session.add(
            AgentPermission(
                tenant_id=tenant_id,
                principal_sub=principal_sub,
                op_pattern=_UNUSED_OP,
                verdict="deny",
                created_by_sub=giver,
            )
        )
        await session.commit()


async def give_service_a_known_person(
    tenant_id: uuid.UUID, principal_sub: str, giver: str = GRANT_GIVER
) -> None:
    """Store a standing grant for the service login *principal_sub* that *giver* gave."""
    async with get_sessionmaker()() as session:
        found = await session.scalar(
            select(ServicePrincipalGrant.id).where(
                ServicePrincipalGrant.tenant_id == tenant_id,
                ServicePrincipalGrant.principal_sub == principal_sub,
                ServicePrincipalGrant.op_id == _UNUSED_OP,
            )
        )
        if found is not None:
            return
        session.add(
            ServicePrincipalGrant(
                tenant_id=tenant_id,
                principal_sub=principal_sub,
                op_id=_UNUSED_OP,
                connector_id="people-behind-1.0",
                reason="a known person behind the login",
                created_by_sub=giver,
            )
        )
        await session.commit()


async def give_known_person(operator: Operator, giver: str = GRANT_GIVER) -> None:
    """Give the agent or service login *operator* a known person; a person needs none."""
    if operator.principal_kind is PrincipalKind.AGENT:
        await give_agent_a_known_person(operator.tenant_id, operator.sub, giver)
    elif operator.principal_kind is PrincipalKind.SERVICE:
        await give_service_a_known_person(operator.tenant_id, operator.sub, giver)
