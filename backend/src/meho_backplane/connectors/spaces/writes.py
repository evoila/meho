# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The four ``spaces.*`` writes: assign, unassign, reassign and member.disable.

Each write needs the ``tenant_admin`` role and a reason, and works only in
the caller's tenant. A write never deletes a row. It ends the current row
with a time and adds a new one when needed (``spaces/store.py``). The audit
row of the call holds who made it and the reason.

``spaces.member.disable`` ends one person's membership in one space. It
never calls the Keycloak connector: the person's company account is a
separate step.
"""

from __future__ import annotations

import uuid
from typing import Any

from meho_backplane.auth.operator import Operator
from meho_backplane.connectors.spaces.common import (
    assignment_dict,
    parse_uuid,
    require_admin,
    require_enabled,
)
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.spaces import store
from meho_backplane.spaces.models import ResourceAssignment, SpaceMember
from meho_backplane.targets.resolver import resolve_target_by_id

__all__ = [
    "owner_left",
    "spaces_assign",
    "spaces_member_disable",
    "spaces_reassign",
    "spaces_unassign",
]


def owner_left(members: list[SpaceMember]) -> bool:
    """True when the current members have a disabled owner and no active owner."""
    owners = [m for m in members if m.role == "owner"]
    return any(m.state == "disabled" for m in owners) and not any(
        m.state == "active" for m in owners
    )


def _not_found(exc: LookupError) -> ValueError:
    if isinstance(exc, store.SpaceNotFoundError):
        return ValueError(f"No space {exc} in your tenant.")
    return ValueError(f"No current assignment {exc} in your tenant.")


async def spaces_assign(operator: Operator, target: Any, params: dict[str, Any]) -> dict[str, Any]:
    """Assign a target, a machine, a service or a fact to a space.

    Op-id: ``spaces.assign``. Asking twice for the same current assignment
    returns ``unchanged`` and adds no row.
    """
    require_enabled()
    require_admin(operator, "spaces.assign")
    space_id = parse_uuid(params, "space_id")
    kind = str(params["kind"])
    reason = str(params["reason"]).strip()
    if not reason:
        raise ValueError("reason must not be empty")
    name = params.get("name")
    object_id = params.get("object_id")
    target_id = parse_uuid(params, "target_id") if params.get("target_id") else None
    if kind == "target" and target_id is None:
        raise ValueError("kind target needs target_id")
    if kind == "object" and not object_id:
        raise ValueError("kind object needs object_id")
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session, session.begin():
        if target_id is not None:
            row = await resolve_target_by_id(session, operator.tenant_id, target_id)
            if row is None:
                raise ValueError(f"No target {target_id} in your tenant.")
            name = name or row.name
        if not name:
            raise ValueError(f"kind {kind} needs name")
        try:
            existing = await _find_current(
                session, operator.tenant_id, space_id, kind, target_id, object_id, str(name)
            )
            if existing is not None:
                return {"status": "unchanged", "assignment": assignment_dict(existing)}
            created = await store.assign(
                session,
                tenant_id=operator.tenant_id,
                space_id=space_id,
                kind=kind,
                target_id=target_id,
                object_id=str(object_id) if object_id else None,
                name_at_assignment=str(name),
                access=str(params.get("access", "own")),
                changed_by=operator.sub,
                reason=reason,
            )
        except store.SpaceNotFoundError as exc:
            raise _not_found(exc) from exc
    return {"status": "assigned", "assignment": assignment_dict(created)}


async def _find_current(
    session: Any,
    tenant_id: uuid.UUID,
    space_id: uuid.UUID,
    kind: str,
    target_id: uuid.UUID | None,
    object_id: str | None,
    name: str,
) -> ResourceAssignment | None:
    """The current assignment of the same thing in the same space, if any."""
    rows = await store.list_assignments(session, tenant_id=tenant_id, space_id=space_id)
    for row in rows:
        if row.kind != kind:
            continue
        if kind == "target" and row.target_id == target_id:
            return row
        if kind == "object" and row.object_id == object_id:
            return row
        if kind in ("service", "fact") and row.name_at_assignment == name:
            return row
    return None


async def spaces_unassign(
    operator: Operator, target: Any, params: dict[str, Any]
) -> dict[str, Any]:
    """End one current assignment. The row stays, with an end time.

    Op-id: ``spaces.unassign``.
    """
    require_enabled()
    require_admin(operator, "spaces.unassign")
    assignment_id = parse_uuid(params, "assignment_id")
    if not str(params["reason"]).strip():
        raise ValueError("reason must not be empty")
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session, session.begin():
        try:
            row = await store.end_assignment(
                session, tenant_id=operator.tenant_id, assignment_id=assignment_id
            )
        except store.AssignmentNotFoundError as exc:
            raise _not_found(exc) from exc
        answer = assignment_dict(row)
    return {"status": "ended", "assignment": answer}


async def spaces_reassign(
    operator: Operator, target: Any, params: dict[str, Any]
) -> dict[str, Any]:
    """Move a current assignment to another space in one step.

    Op-id: ``spaces.reassign``. The old row ends and a new one starts with
    the same time. A move to the space it is in changes nothing.
    """
    require_enabled()
    require_admin(operator, "spaces.reassign")
    assignment_id = parse_uuid(params, "assignment_id")
    to_space_id = parse_uuid(params, "to_space_id")
    reason = str(params["reason"]).strip()
    if not reason:
        raise ValueError("reason must not be empty")
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session, session.begin():
        try:
            current = await store.list_assignments(session, tenant_id=operator.tenant_id)
            same = next((r for r in current if r.id == assignment_id), None)
            if same is not None and same.space_id == to_space_id:
                return {
                    "status": "unchanged",
                    "ended_assignment_id": str(assignment_id),
                    "assignment": assignment_dict(same),
                }
            created = await store.reassign(
                session,
                tenant_id=operator.tenant_id,
                assignment_id=assignment_id,
                to_space_id=to_space_id,
                changed_by=operator.sub,
                reason=reason,
            )
        except (store.SpaceNotFoundError, store.AssignmentNotFoundError) as exc:
            raise _not_found(exc) from exc
        answer = assignment_dict(created)
    return {"status": "moved", "ended_assignment_id": str(assignment_id), "assignment": answer}


async def spaces_member_disable(
    operator: Operator, target: Any, params: dict[str, Any]
) -> dict[str, Any]:
    """End one person's membership in one space ("leave a lab").

    Op-id: ``spaces.member.disable``. The member's current row ends and a
    new row with state ``disabled`` starts. When that leaves the space with
    no active owner, the answer lists the space's current assignments: they
    wait for reassignment. Nothing is deleted. The person's company account
    is not touched, and no Keycloak call is made.
    """
    require_enabled()
    require_admin(operator, "spaces.member.disable")
    space_id = parse_uuid(params, "space_id")
    issuer = str(params["member_issuer"])
    sub = str(params["member_sub"])
    reason = str(params["reason"]).strip()
    if not reason:
        raise ValueError("reason must not be empty")
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session, session.begin():
        try:
            members = await store.list_members(
                session, tenant_id=operator.tenant_id, space_id=space_id
            )
            mine = next(
                (m for m in members if m.member_issuer == issuer and m.member_sub == sub), None
            )
            if mine is None:
                raise ValueError(f"{sub} is not a current member of space {space_id}.")
            status = "unchanged"
            if mine.state != "disabled":
                await store.set_member(
                    session,
                    tenant_id=operator.tenant_id,
                    space_id=space_id,
                    member_issuer=issuer,
                    member_sub=sub,
                    role=mine.role,
                    state="disabled",
                    changed_by=operator.sub,
                    reason=reason,
                )
                status = "disabled"
            members = await store.list_members(
                session, tenant_id=operator.tenant_id, space_id=space_id
            )
            left = owner_left(members)
            marked: list[str] = []
            if left:
                rows = await store.list_assignments(
                    session, tenant_id=operator.tenant_id, space_id=space_id
                )
                marked = [str(r.id) for r in rows]
        except store.SpaceNotFoundError as exc:
            raise _not_found(exc) from exc
    return {
        "status": status,
        "space_id": str(space_id),
        "owner_left": left,
        "assignments_marked_owner_left": marked,
    }
