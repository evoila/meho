# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The people behind a request of an agent, service or runner login.

A request made by an agent, a service login or a runner counts as a
request of the people the login comes from. They may approve it only where
they may approve their own change, and a change of rights always needs
another person (:func:`~meho_backplane.operations.approval_queue._check_self_approval`,
:meth:`~meho_backplane.auth.operator.Operator.may_approve_rights_change`).

:func:`find_people_behind` finds them, in the login's tenant:

* **Agent:** who registered or owns the agent
  (:class:`~meho_backplane.db.models.AgentPrincipal`); who gave one of its
  grants in force (:class:`~meho_backplane.db.models.AgentPermission`);
  who wrote or last changed one of its agent definitions; who made one of
  the schedules of those definitions, active or not; and, when one of
  them waits for events, who made an event source of the tenant.
* **Service login:** who paired or owns its add-on
  (:class:`~meho_backplane.db.models.AddonPairing`) and who gave one of
  its standing grants in force.
* **Runner:** who registered or owns the runner.
* **Every machine login:** who started or handed over a runbook run that
  is assigned to it and has not ended.

The answer is ``None`` for a person: the person is the requester already.
For a machine login it is a list of ``{"sub", "name", "why"}`` entries,
one per person and reason. An empty list means MEHO knows nobody behind
the login. Then only an admin may approve the request
(:func:`people_behind_unknown`).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, datetime
from typing import Any, Final, NamedTuple

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.auth.operator import MACHINE_PRINCIPAL_KINDS, PrincipalKind
from meho_backplane.db.models import (
    AddonPairing,
    AgentDefinition,
    AgentPermission,
    AgentPrincipal,
    AuditLog,
    EventSource,
    RunbookRun,
    RunnerPrincipal,
    SandboxDeleteGrant,
    ScheduledTrigger,
    ServicePrincipalGrant,
)

__all__ = [
    "WHY_TEXT",
    "PersonBehind",
    "behind_subs",
    "entries_of",
    "find_people_behind",
    "is_person_behind",
    "people_behind_line",
    "people_behind_unknown",
    "whys_of",
]

#: What each reason code means, in words for the approval card. The order
#: is the order of the card line.
WHY_TEXT: Final[Mapping[str, str]] = {
    "registered": "Set up by",
    "owns": "Owned by",
    "agent_definition": "Agent definition by",
    "changed_agent_definition": "Agent definition last changed by",
    "gave_grant": "Grant by",
    "made_schedule": "Schedule by",
    "made_event_source": "Event source by",
    "started_run": "Run started by",
    "handed_over_run": "Run handed over by",
}

#: How many of a person's newest audit rows MEHO reads to find their name.
_NAME_SCAN_ROWS: Final[int] = 200


def _machine_kind(kind: str | None) -> PrincipalKind | None:
    if kind is None:
        return None
    try:
        parsed = PrincipalKind(kind)
    except ValueError:
        return None
    return parsed if parsed in MACHINE_PRINCIPAL_KINDS else None


async def _scalars(session: AsyncSession, stmt: Any) -> list[Any]:
    return list((await session.execute(stmt)).all())


async def _agent_people(
    session: AsyncSession, tenant_id: uuid.UUID, ids: set[str], now: datetime
) -> list[tuple[str | None, str]]:
    found: list[tuple[str | None, str]] = []
    for owner, creator in await _scalars(
        session,
        select(AgentPrincipal.owner_sub, AgentPrincipal.created_by_sub).where(
            AgentPrincipal.tenant_id == tenant_id,
            AgentPrincipal.keycloak_client_id.in_(ids),
        ),
    ):
        found += [(creator, "registered"), (owner, "owns")]
    for (giver,) in await _scalars(
        session,
        select(AgentPermission.created_by_sub).where(
            AgentPermission.tenant_id == tenant_id,
            AgentPermission.principal_sub.in_(ids),
            or_(AgentPermission.expires_at.is_(None), AgentPermission.expires_at > now),
        ),
    ):
        found.append((giver, "gave_grant"))
    definitions = await _scalars(
        session,
        select(
            AgentDefinition.id, AgentDefinition.created_by_sub, AgentDefinition.updated_by_sub
        ).where(AgentDefinition.tenant_id == tenant_id, AgentDefinition.identity_ref.in_(ids)),
    )
    for _, creator, editor in definitions:
        found += [(creator, "agent_definition"), (editor, "changed_agent_definition")]
    found += await _schedule_people(session, tenant_id, [row[0] for row in definitions])
    return found


async def _schedule_people(
    session: AsyncSession, tenant_id: uuid.UUID, definition_ids: Sequence[uuid.UUID]
) -> list[tuple[str | None, str]]:
    if not definition_ids:
        return []
    found: list[tuple[str | None, str]] = []
    waits_for_events = False
    for creator, kind in await _scalars(
        session,
        select(ScheduledTrigger.created_by_sub, ScheduledTrigger.kind).where(
            ScheduledTrigger.tenant_id == tenant_id,
            ScheduledTrigger.agent_definition_id.in_(definition_ids),
        ),
    ):
        found.append((creator, "made_schedule"))
        waits_for_events = waits_for_events or kind == "event"
    if waits_for_events:
        for (creator,) in await _scalars(
            session,
            select(EventSource.created_by_sub).where(EventSource.tenant_id == tenant_id),
        ):
            found.append((creator, "made_event_source"))
    return found


async def _service_people(
    session: AsyncSession, tenant_id: uuid.UUID, sub: str, client_id: str | None, now: datetime
) -> list[tuple[str | None, str]]:
    found: list[tuple[str | None, str]] = []
    pairing_match = AddonPairing.service_account_sub == sub
    if client_id:
        pairing_match = or_(pairing_match, AddonPairing.keycloak_client_id == client_id)
    for owner, creator in await _scalars(
        session,
        select(AddonPairing.owner_sub, AddonPairing.created_by_sub).where(
            AddonPairing.tenant_id == tenant_id, pairing_match
        ),
    ):
        found += [(creator, "registered"), (owner, "owns")]
    for model in (ServicePrincipalGrant, SandboxDeleteGrant):
        for (giver,) in await _scalars(
            session,
            select(model.created_by_sub).where(
                model.tenant_id == tenant_id,
                model.principal_sub == sub,
                model.revoked_at.is_(None),
                model.replaced_at.is_(None),
                or_(model.expires_at.is_(None), model.expires_at > now),
            ),
        ):
            found.append((giver, "gave_grant"))
    return found


async def _runner_people(
    session: AsyncSession, tenant_id: uuid.UUID, ids: set[str]
) -> list[tuple[str | None, str]]:
    found: list[tuple[str | None, str]] = []
    for owner, creator in await _scalars(
        session,
        select(RunnerPrincipal.owner_sub, RunnerPrincipal.created_by_sub).where(
            RunnerPrincipal.tenant_id == tenant_id,
            RunnerPrincipal.keycloak_client_id.in_(ids),
        ),
    ):
        found += [(creator, "registered"), (owner, "owns")]
    return found


async def _runbook_people(
    session: AsyncSession, tenant_id: uuid.UUID, ids: set[str]
) -> list[tuple[str | None, str]]:
    found: list[tuple[str | None, str]] = []
    for starter, handed_over_by in await _scalars(
        session,
        select(RunbookRun.started_by, RunbookRun.reassigned_by).where(
            RunbookRun.tenant_id == tenant_id,
            RunbookRun.assigned_to.in_(ids),
            RunbookRun.completed_at.is_(None),
            RunbookRun.abandoned_at.is_(None),
        ),
    ):
        found += [(starter, "started_run"), (handed_over_by, "handed_over_run")]
    return found


async def _name_of(session: AsyncSession, tenant_id: uuid.UUID, sub: str) -> str | None:
    """The newest name MEHO recorded for *sub* in the tenant's audit log, or ``None``."""
    newest = (
        select(AuditLog.payload)
        .where(AuditLog.operator_sub == sub, AuditLog.tenant_id == tenant_id)
        .order_by(AuditLog.occurred_at.desc())
        .limit(_NAME_SCAN_ROWS)
    )
    for (payload,) in await _scalars(session, newest):
        name = payload.get("principal_name") if isinstance(payload, dict) else None
        if isinstance(name, str) and name:
            return name
    return None


async def find_people_behind(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    sub: str,
    client_id: str | None,
    kind: str | None,
) -> list[dict[str, Any]] | None:
    """Return the people behind the login *sub*, or ``None`` for a person.

    *kind* is the login's ``principal_kind``; *client_id* its OAuth client
    id, when known. See the module docstring for who counts. The requester
    itself is never in the list.
    """
    machine = _machine_kind(kind)
    if machine is None:
        return None
    ids = {sub} | ({client_id} if client_id else set())
    now = datetime.now(UTC)
    found: list[tuple[str | None, str]] = []
    if machine is PrincipalKind.AGENT:
        found += await _agent_people(session, tenant_id, ids, now)
    elif machine is PrincipalKind.SERVICE:
        found += await _service_people(session, tenant_id, sub, client_id, now)
    else:
        found += await _runner_people(session, tenant_id, ids)
    found += await _runbook_people(session, tenant_id, ids)
    pairs = sorted(
        {(person, why) for person, why in found if person and person not in ids},
        key=lambda pair: (list(WHY_TEXT).index(pair[1]), pair[0]),
    )
    names: dict[str, str | None] = {}
    for person, _ in pairs:
        if person not in names:
            names[person] = await _name_of(session, tenant_id, person)
    return [{"sub": person, "name": names[person], "why": why} for person, why in pairs]


class PersonBehind(NamedTuple):
    """One stored ``people_behind`` entry, with its reason in words."""

    sub: str
    name: str | None
    why: str
    why_text: str


def entries_of(people: Iterable[object] | None) -> list[PersonBehind]:
    """The well-formed entries of *people* (a stored ``people_behind`` list)."""
    found: list[PersonBehind] = []
    for entry in people or []:
        if not isinstance(entry, Mapping) or not isinstance(entry.get("sub"), str):
            continue
        name = entry.get("name")
        why = str(entry.get("why"))
        found.append(
            PersonBehind(
                sub=entry["sub"],
                name=name if isinstance(name, str) and name else None,
                why=why,
                why_text=WHY_TEXT.get(why, why),
            )
        )
    return found


def behind_subs(people: Iterable[object] | None) -> frozenset[str]:
    """The subs of *people* (a stored ``people_behind`` list), casefolded."""
    return frozenset(entry.sub.casefold() for entry in entries_of(people))


def is_person_behind(sub: str, people: Iterable[object] | None) -> bool:
    """True when *sub* is one of *people*."""
    return sub.casefold() in behind_subs(people)


def whys_of(sub: str, people: Iterable[object] | None) -> list[str]:
    """The reason codes for which *sub* is one of *people*, in card order."""
    mine = sub.casefold()
    return [entry.why for entry in entries_of(people) if entry.sub.casefold() == mine]


def people_behind_unknown(kind: str | None, people: Iterable[object] | None) -> bool:
    """True when a machine login asked and MEHO knows nobody behind it."""
    return _machine_kind(kind) is not None and not entries_of(people)


def people_behind_line(kind: str | None, people: Iterable[object] | None) -> str | None:
    """The card line, for example ``"Set up by Ann. Grant by Bob, Eve."``.

    ``None`` for a person's request. ``"Set up by: not known. ..."`` when
    MEHO knows nobody behind the machine login.
    """
    if _machine_kind(kind) is None:
        return None
    entries = entries_of(people)
    if not entries:
        return "Set up by: not known. Only an Admin may approve."
    by_why: dict[str, list[str]] = {}
    for entry in entries:
        by_why.setdefault(entry.why_text, []).append(entry.name or entry.sub)
    return " ".join(f"{why_text} {', '.join(labels)}." for why_text, labels in by_why.items())
