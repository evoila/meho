# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The login ids that MEHO knows as agent, service or runner logins.

A machine login never counts as a person behind another login
(:mod:`meho_backplane.operations.people_behind`). MEHO knows a login id is
a machine login when:

* the login signed in as one: :func:`note_machine_login` adds a
  :class:`~meho_backplane.db.models.MachineLogin` row on each REST and MCP
  call of an agent, service or runner login, and when a hosted agent run that
  a schedule or an event starts signs in; or
* the id is the login of an agent definition, an agent, an add-on or a
  runner of the tenant.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable

from sqlalchemy import ColumnElement, CompoundSelect, select, union
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.dml import Insert

from meho_backplane.auth.operator import Operator, is_human_principal
from meho_backplane.db.models import (
    AddonPairing,
    AgentDefinition,
    AgentPrincipal,
    MachineLogin,
    RunnerPrincipal,
)

__all__ = ["is_machine_login", "machine_subs", "note_machine_login"]


async def note_machine_login(session: AsyncSession, operator: Operator) -> None:
    """Keep the login id of *operator* when it is an agent, service or runner login.

    The row also keeps the client id of the token (``agent:<name>`` for an
    agent), the name MEHO shows for the login. A known login id gets the
    client id when its row has another one or none. Safe to run twice.
    The caller owns the transaction.
    """
    if is_human_principal(operator):
        return
    values = {
        "tenant_id": operator.tenant_id,
        "sub": operator.sub,
        "kind": operator.principal_kind.value,
        "client_id": operator.client_id,
    }
    conn = await session.connection()
    stmt: Insert
    if conn.dialect.name == "postgresql":
        pg_stmt = pg_insert(MachineLogin).values(**values)
        stmt = pg_stmt.on_conflict_do_update(
            index_elements=[MachineLogin.tenant_id, MachineLogin.sub],
            set_={"client_id": pg_stmt.excluded.client_id},
            where=_client_id_changed(pg_stmt.excluded.client_id),
        )
    else:
        lite_stmt = sqlite_insert(MachineLogin).values(**values)
        stmt = lite_stmt.on_conflict_do_update(
            index_elements=[MachineLogin.tenant_id, MachineLogin.sub],
            set_={"client_id": lite_stmt.excluded.client_id},
            where=_client_id_changed(lite_stmt.excluded.client_id),
        )
    await session.execute(stmt)


def _client_id_changed(new: ColumnElement[str | None]) -> ColumnElement[bool]:
    return new.is_not(None) & MachineLogin.client_id.is_distinct_from(new)


async def machine_subs(
    session: AsyncSession, tenant_id: uuid.UUID, subs: Iterable[str]
) -> set[str]:
    """The ids of *subs* that MEHO knows as machine logins in *tenant_id*."""
    wanted = set(subs)
    if not wanted:
        return set()
    known: CompoundSelect[tuple[str]] = union(
        select(MachineLogin.sub).where(
            MachineLogin.tenant_id == tenant_id, MachineLogin.sub.in_(wanted)
        ),
        select(AgentDefinition.identity_ref).where(
            AgentDefinition.tenant_id == tenant_id, AgentDefinition.identity_ref.in_(wanted)
        ),
        select(AgentPrincipal.keycloak_client_id).where(
            AgentPrincipal.tenant_id == tenant_id, AgentPrincipal.keycloak_client_id.in_(wanted)
        ),
        select(AddonPairing.keycloak_client_id).where(
            AddonPairing.tenant_id == tenant_id, AddonPairing.keycloak_client_id.in_(wanted)
        ),
        select(AddonPairing.service_account_sub).where(
            AddonPairing.tenant_id == tenant_id, AddonPairing.service_account_sub.in_(wanted)
        ),
        select(RunnerPrincipal.keycloak_client_id).where(
            RunnerPrincipal.tenant_id == tenant_id, RunnerPrincipal.keycloak_client_id.in_(wanted)
        ),
    )
    return {sub for (sub,) in (await session.execute(known)).all() if sub}


async def is_machine_login(session: AsyncSession, tenant_id: uuid.UUID, sub: str) -> bool:
    """True when MEHO knows *sub* as a machine login in *tenant_id*."""
    return bool(await machine_subs(session, tenant_id, [sub]))
