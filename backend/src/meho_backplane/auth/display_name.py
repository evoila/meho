# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The name MEHO stores for the caller on audit rows and approval requests.

A person's token carries a ``name`` claim. A machine login (an agent, a
service account, a check runner) gets its token with the
``client_credentials`` grant, and that token has no ``name`` claim. So
before #3885 every audit row and every approval request of an agent had
no name, and the approver saw only the token's ``sub`` (a GUID).

:func:`principal_display_name` gives the name to store, in this order:

1. The token's ``name`` claim (:attr:`Operator.name`). A person's name
   always wins.
2. For an agent token: the name of the registered agent
   (:attr:`AgentPrincipal.name`), found by the token's client id in the
   caller's tenant. A revoked agent still counts: the row shows who
   asked.
3. For any machine token: the client id itself, for example
   ``agent:<name>``.

``None`` only when the token has no name and is not a machine login with
a client id. Callers then store no name, as before.

Every place that stores the caller's name calls this one function: the
operation run and refusal rows, the MCP tool rows, the tool rows of
MEHO's own agent runs, and the approval request and its audit rows.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.auth.operator import MACHINE_PRINCIPAL_KINDS, Operator, PrincipalKind
from meho_backplane.db.models import AgentPrincipal

__all__ = ["principal_display_name"]


async def principal_display_name(session: AsyncSession, operator: Operator) -> str | None:
    """Return the name to store for *operator*, or ``None`` (see the module docstring)."""
    if operator.name:
        return operator.name
    client_id = operator.client_id
    if operator.principal_kind not in MACHINE_PRINCIPAL_KINDS or not client_id:
        return None
    if operator.principal_kind is PrincipalKind.AGENT:
        registered = (
            await session.execute(
                select(AgentPrincipal.name).where(
                    AgentPrincipal.tenant_id == operator.tenant_id,
                    AgentPrincipal.keycloak_client_id == client_id,
                )
            )
        ).scalar_one_or_none()
        if registered:
            return registered
    return client_id
