# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Server-checked agent identity for intentional announcements (#3736).

Only a trusted agent may publish an announcement on the broadcast feed.
People keep read access under their normal tenant role. This module
decides, from server-side facts only, whether the caller is a trusted
agent, and returns the identity the server stamps on the announcement.

Two ways in, checked in this order:

1. **Hosted agent run.** The call runs inside a live MEHO-hosted agent
   run. The run id comes from
   :data:`~meho_backplane.agent.invoke.current_agent_run_id_var`, which
   only the agent runtime sets; no request field, header, or token claim
   can set it. The run row must exist in the caller's tenant and must
   not be finished. The acting agent comes from the delegation binding
   (:func:`~meho_backplane.auth.delegation.resolve_actor_sub`) and the
   work reference comes from the run row.
2. **Registered agent principal.** The verified token says
   ``principal_kind=agent`` and its client id names a registered,
   not-revoked :class:`~meho_backplane.db.models.AgentPrincipal` row in
   the caller's tenant (registered with ``meho agent-principal
   register``).

Anything else raises :class:`AgentIdentityRequiredError`, whose ``code``
is the stable machine error ``agent_identity_required``. MEHO never
guesses an agent from the transport, the OAuth client of a person's
login, a CLI login, a boolean flag, or a ``run_id`` the caller sent.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final
from uuid import UUID

import structlog
from sqlalchemy import select

from meho_backplane.auth.delegation import resolve_actor_sub
from meho_backplane.auth.operator import Operator, PrincipalKind
from meho_backplane.broadcast.agent_events import AdmissionSource
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AgentPrincipal, AgentRun, AgentRunStatus

__all__ = [
    "AGENT_IDENTITY_REMEDIATION",
    "AGENT_IDENTITY_REQUIRED",
    "AgentAdmission",
    "AgentIdentityRequiredError",
    "resolve_agent_admission",
]

_log = structlog.get_logger(__name__)

#: Stable machine error code for a publish without a trusted agent identity.
AGENT_IDENTITY_REQUIRED: Final[str] = "agent_identity_required"

#: What the caller must do instead. Returned on every refusal.
AGENT_IDENTITY_REMEDIATION: Final[str] = (
    "Only a trusted agent can publish an announcement. Use one of two ways: "
    "(1) run the work as a MEHO-hosted agent run, or "
    "(2) register an agent principal with `meho agent-principal register <name>` "
    "and call MEHO with that agent's client-credentials token. "
    "People can still read the feed with meho_broadcast_recent. "
    "See docs/codebase/broadcast.md, section 'Who can publish'."
)

#: Run states in which a hosted run may still publish.
_LIVE_RUN_STATUSES: Final[frozenset[str]] = frozenset(
    {
        AgentRunStatus.PENDING.value,
        AgentRunStatus.RUNNING.value,
        AgentRunStatus.AWAITING_APPROVAL.value,
    }
)


class AgentIdentityRequiredError(Exception):
    """The caller is not a trusted agent, so it may not publish.

    ``code`` is always :data:`AGENT_IDENTITY_REQUIRED`. ``reason`` says
    which check failed. ``remediation`` says how to get a trusted
    identity.
    """

    code: Final[str] = AGENT_IDENTITY_REQUIRED

    def __init__(self, reason: str) -> None:
        self.reason = reason
        self.remediation = AGENT_IDENTITY_REMEDIATION
        super().__init__(f"{AGENT_IDENTITY_REQUIRED}: {reason}")


@dataclass(frozen=True)
class AgentAdmission:
    """The checked identity the server stamps on an announcement.

    * ``source`` -- which check let the caller in.
    * ``owner_sub`` -- the trusted agent identity that owns the claims it
      publishes. For a hosted run: the acting agent, or the run's subject
      when the agent runs as itself. For an agent principal: its
      ``agent:<name>`` client id.
    * ``principal_sub`` -- the verified token ``sub``.
    * ``actor_sub`` -- the RFC 8693 actor of a delegated hosted run.
    * ``run_id`` / ``work_ref`` -- the hosted run's id and work reference,
      read from the run row. Both ``None`` for an agent principal.
    """

    source: AdmissionSource
    owner_sub: str
    principal_sub: str
    actor_sub: str | None
    run_id: UUID | None
    work_ref: str | None


async def _admit_hosted_run(operator: Operator, run_id: UUID) -> AgentAdmission:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        row = (
            await session.execute(
                select(
                    AgentRun.identity_sub, AgentRun.identity_act, AgentRun.status, AgentRun.work_ref
                ).where(AgentRun.id == run_id, AgentRun.tenant_id == operator.tenant_id)
            )
        ).one_or_none()
    if row is None:
        raise AgentIdentityRequiredError("the agent run of this call does not exist in this tenant")
    if row.status not in _LIVE_RUN_STATUSES:
        raise AgentIdentityRequiredError(f"the agent run of this call is {row.status}, not live")
    actor_sub = resolve_actor_sub() or row.identity_act
    return AgentAdmission(
        source="hosted_run",
        owner_sub=actor_sub or row.identity_sub,
        principal_sub=operator.sub,
        actor_sub=actor_sub,
        run_id=run_id,
        work_ref=row.work_ref,
    )


async def _admit_agent_principal(operator: Operator) -> AgentAdmission:
    client_id = operator.client_id
    if not client_id:
        raise AgentIdentityRequiredError("the agent token carries no client id")
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        revoked = (
            await session.execute(
                select(AgentPrincipal.revoked).where(
                    AgentPrincipal.tenant_id == operator.tenant_id,
                    AgentPrincipal.keycloak_client_id == client_id,
                )
            )
        ).scalar_one_or_none()
    if revoked is None:
        raise AgentIdentityRequiredError("the agent is not registered in this tenant")
    if revoked:
        raise AgentIdentityRequiredError("the agent principal is revoked")
    return AgentAdmission(
        source="agent_principal",
        owner_sub=client_id,
        principal_sub=operator.sub,
        actor_sub=None,
        run_id=None,
        work_ref=None,
    )


async def resolve_agent_admission(operator: Operator) -> AgentAdmission:
    """Return the checked agent identity of *operator*, or refuse.

    Raises:
        AgentIdentityRequiredError: the caller is neither inside a live
            hosted agent run nor a registered agent principal.
    """
    # Imported here: ``meho_backplane.agent`` imports the broadcast MCP
    # tools, which import this module.
    from meho_backplane.agent.invoke import current_agent_run_id_var

    run_id = current_agent_run_id_var.get()
    try:
        if run_id is not None:
            return await _admit_hosted_run(operator, run_id)
        if operator.principal_kind is PrincipalKind.AGENT:
            return await _admit_agent_principal(operator)
        raise AgentIdentityRequiredError(
            f"a {operator.principal_kind.value} token outside a hosted agent run "
            "cannot publish announcements"
        )
    except AgentIdentityRequiredError as exc:
        _log.info(
            "broadcast_announce_identity_refused",
            tenant_id=str(operator.tenant_id),
            principal_kind=operator.principal_kind.value,
            reason=exc.reason,
        )
        raise
