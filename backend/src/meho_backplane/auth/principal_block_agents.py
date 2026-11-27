# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The agent names of the admin block list (``agent:<name>``).

A block text that starts with ``agent:`` names an agent: it must be the
``keycloak_client_id`` of an agent registered in the admin's reach (the
own tenant, or every tenant for a platform admin; a revoked agent counts),
else MEHO refuses it (:class:`UnknownLoginError`). Every other text is a
login id, and MEHO never compares it with agent names.

At block time MEHO asks Keycloak for the login id behind the agent (the
service-account user of the agent's client, :func:`agent_login_id`) and
stores it with the block. A token check that is refused by the agent name
also keeps the link of its login id to the name
(:func:`keep_refused_agent_link`). So a check that knows only the login id
finds the block too.
"""

from __future__ import annotations

import uuid
from typing import Final

import structlog
from sqlalchemy import ColumnElement, select, true, union
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped

from meho_backplane.auth.keycloak_admin import KeycloakAdminClient
from meho_backplane.auth.machine_logins import note_machine_login
from meho_backplane.auth.operator import Operator
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AgentPrincipal, MachineLogin

__all__ = [
    "AGENT_NAME_PREFIX",
    "UnknownLoginError",
    "agent_login_id",
    "agent_names",
    "agent_of_block_text",
    "agents_in_reach",
    "is_agent_name",
    "keep_refused_agent_link",
    "stored_sentence",
]

#: The start of the name MEHO gives an agent's login (``agent:<name>``).
AGENT_NAME_PREFIX: Final[str] = "agent:"

_log = structlog.get_logger(__name__)


class UnknownLoginError(Exception):
    """The block names an agent that MEHO does not know. ``str(exc)`` says it in plain words."""


def is_agent_name(text: str | None) -> bool:
    """True when *text* has the form of an agent name (``agent:<name>``)."""
    return text is not None and text.startswith(AGENT_NAME_PREFIX)


def stored_sentence(sub: str) -> str:
    """What the admin reads when the block of *sub* is stored.

    Each MEHO process keeps the block list for ``BLOCK_CACHE_SECONDS``, so
    the block reaches every process within that time.
    """
    login = "agent" if is_agent_name(sub) else "login"
    return (
        f"The block is stored. MEHO refuses this {login} "
        "within a few seconds on every MEHO process."
    )


def _in_reach(operator: Operator, tenant_column: Mapped[uuid.UUID]) -> ColumnElement[bool]:
    """Every tenant for a platform admin, else the admin's own tenant."""
    return true() if operator.platform_admin else tenant_column == operator.tenant_id


async def agent_of_block_text(
    session: AsyncSession, operator: Operator, sub: str
) -> AgentPrincipal | None:
    """The agent *sub* names, or ``None`` when *sub* is a login id.

    Raises :class:`UnknownLoginError` when *sub* starts with ``agent:`` but
    names no agent registered in the admin's reach.
    """
    if not is_agent_name(sub):
        return None
    query = select(AgentPrincipal).where(
        AgentPrincipal.keycloak_client_id == sub,
        _in_reach(operator, AgentPrincipal.tenant_id),
    )
    agent = (await session.execute(query)).scalars().first()
    if agent is None:
        raise UnknownLoginError(
            f"MEHO knows no agent with the login name {sub}. "
            "Copy the login name from the agent list."
        )
    return agent


async def agents_in_reach(operator: Operator) -> list[str]:
    """The login names of the agents in the admin's reach, revoked ones too."""
    query = (
        select(AgentPrincipal.keycloak_client_id)
        .where(_in_reach(operator, AgentPrincipal.tenant_id))
        .order_by(AgentPrincipal.keycloak_client_id)
    )
    async with get_sessionmaker()() as session:
        return [str(name) for name in (await session.execute(query)).scalars()]


async def agent_names(session: AsyncSession, operator: Operator, subs: set[str]) -> dict[str, str]:
    """The agent name of each blocked login in *subs* that is an agent.

    A block of an agent name gives that name. A block of a login id gives
    the agent name that MEHO saw with the login id.
    """
    if not subs:
        return {}
    by_name = select(AgentPrincipal.keycloak_client_id, AgentPrincipal.keycloak_client_id).where(
        AgentPrincipal.keycloak_client_id.in_(subs), _in_reach(operator, AgentPrincipal.tenant_id)
    )
    by_sub = select(MachineLogin.sub, MachineLogin.client_id).where(
        MachineLogin.sub.in_(subs),
        MachineLogin.client_id.startswith(AGENT_NAME_PREFIX),
        _in_reach(operator, MachineLogin.tenant_id),
    )
    names: dict[str, str] = {}
    for sub, name in (await session.execute(union(by_name, by_sub))).all():
        if is_agent_name(str(name)):
            names.setdefault(str(sub), str(name))
    return names


async def agent_login_id(agent: AgentPrincipal) -> str | None:
    """The login id behind *agent* (its service-account user in Keycloak), or ``None``.

    MEHO takes the login id only from a clear answer: Keycloak answers 200
    and the answer's ``id`` is a text that is not empty. Every other answer,
    and every error while the answer is read, counts as "Keycloak cannot be
    read" and gives ``None``. The block is stored all the same; the checks
    that know only a login id then refuse a machine login whose agent name
    MEHO does not know (:func:`~meho_backplane.auth.principal_block.is_principal_blocked`).
    """
    try:
        async with KeycloakAdminClient.from_settings() as keycloak:
            return await keycloak.get_service_account_user_id(agent.keycloak_internal_id)
    except Exception:
        # Any failure, also a body that is not JSON, means "cannot be read".
        _log.warning(
            "principal_block_agent_login_id_unread",
            agent=agent.keycloak_client_id,
            exc_info=True,
        )
        return None


async def keep_refused_agent_link(operator: Operator) -> None:
    """Keep the link of a refused agent token's login id to its agent name.

    The token check calls it when it refuses the token by the agent name.
    It writes the same row as a call that passes the token check
    (:func:`~meho_backplane.auth.machine_logins.note_machine_login`). A
    failed write is logged; the token is refused all the same.
    """
    try:
        async with get_sessionmaker()() as session, session.begin():
            await note_machine_login(session, operator)
    except Exception:
        _log.warning("principal_block_agent_link_write_failed", exc_info=True)
