# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Read and write spaces, members and assignments.

The history rule: a change never edits a member or assignment row and never
deletes one. It sets ``valid_to`` on the current row and adds a new row with
the same time as ``valid_from``. The end is one conditional ``UPDATE ...
WHERE valid_to IS NULL``, so when two callers end the same row at once, only
one of them ends it; the other finds no current row. Every function takes the tenant id and
filters on it, so one tenant never sees or changes the rows of another. The
callers commit.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.spaces.models import ResourceAssignment, Space, SpaceMember

__all__ = [
    "AssignmentNotFoundError",
    "SpaceNotFoundError",
    "assign",
    "create_space",
    "end_assignment",
    "end_member",
    "list_assignments",
    "list_members",
    "reassign",
    "set_member",
]


class SpaceNotFoundError(LookupError):
    """The space does not exist in this tenant."""


class AssignmentNotFoundError(LookupError):
    """There is no current assignment with this id in this tenant."""


def _now() -> datetime:
    return datetime.now(UTC)


async def _require_space(session: AsyncSession, tenant_id: uuid.UUID, space_id: uuid.UUID) -> None:
    found = await session.scalar(
        select(Space.id).where(Space.id == space_id, Space.tenant_id == tenant_id)
    )
    if found is None:
        raise SpaceNotFoundError(str(space_id))


async def create_space(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    name: str,
    kind: str,
    owner_issuer: str,
    owner_sub: str,
    created_by: str,
    now: datetime | None = None,
) -> Space:
    """Add a space. ``kind`` is ``person`` or ``team``."""
    space = Space(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        name=name,
        kind=kind,
        owner_issuer=owner_issuer,
        owner_sub=owner_sub,
        state="active",
        created_at=now or _now(),
        created_by=created_by,
    )
    session.add(space)
    await session.flush()
    return space


async def _end_open_member(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    space_id: uuid.UUID,
    issuer: str,
    sub: str,
    when: datetime,
) -> bool:
    ended = await session.scalars(
        update(SpaceMember)
        .where(
            SpaceMember.tenant_id == tenant_id,
            SpaceMember.space_id == space_id,
            SpaceMember.member_issuer == issuer,
            SpaceMember.member_sub == sub,
            SpaceMember.valid_to.is_(None),
        )
        .values(valid_to=when)
        .returning(SpaceMember.id)
    )
    return ended.first() is not None


async def set_member(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    space_id: uuid.UUID,
    member_issuer: str,
    member_sub: str,
    role: str,
    state: str,
    changed_by: str,
    reason: str,
    now: datetime | None = None,
) -> SpaceMember:
    """Make a person a member, or change their role or state.

    The current row, if there is one, is ended. A new row is added.
    """
    await _require_space(session, tenant_id, space_id)
    when = now or _now()
    await _end_open_member(session, tenant_id, space_id, member_issuer, member_sub, when)
    row = SpaceMember(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        space_id=space_id,
        member_issuer=member_issuer,
        member_sub=member_sub,
        role=role,
        state=state,
        valid_from=when,
        valid_to=None,
        changed_by=changed_by,
        reason=reason,
    )
    session.add(row)
    await session.flush()
    return row


async def end_member(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    space_id: uuid.UUID,
    member_issuer: str,
    member_sub: str,
    now: datetime | None = None,
) -> bool:
    """End the current membership. Returns False when the person had none."""
    await _require_space(session, tenant_id, space_id)
    return await _end_open_member(
        session, tenant_id, space_id, member_issuer, member_sub, now or _now()
    )


async def list_members(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    space_id: uuid.UUID,
    include_history: bool = False,
) -> list[SpaceMember]:
    """The current members, or every row ever made when ``include_history``."""
    await _require_space(session, tenant_id, space_id)
    stmt = select(SpaceMember).where(
        SpaceMember.tenant_id == tenant_id, SpaceMember.space_id == space_id
    )
    if not include_history:
        stmt = stmt.where(SpaceMember.valid_to.is_(None))
    result = await session.scalars(stmt.order_by(SpaceMember.valid_from, SpaceMember.id))
    return list(result)


async def assign(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    space_id: uuid.UUID,
    kind: str,
    target_id: uuid.UUID | None,
    object_id: str | None,
    name_at_assignment: str,
    access: str,
    changed_by: str,
    reason: str,
    now: datetime | None = None,
) -> ResourceAssignment:
    """Assign a system to a space. The new row is current."""
    await _require_space(session, tenant_id, space_id)
    row = ResourceAssignment(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        space_id=space_id,
        kind=kind,
        target_id=target_id,
        object_id=object_id,
        name_at_assignment=name_at_assignment,
        access=access,
        valid_from=now or _now(),
        valid_to=None,
        changed_by=changed_by,
        reason=reason,
    )
    session.add(row)
    await session.flush()
    return row


async def end_assignment(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    assignment_id: uuid.UUID,
    now: datetime | None = None,
) -> ResourceAssignment:
    """End a current assignment. The row stays, with ``valid_to`` set.

    Raises :class:`AssignmentNotFoundError` when the row is not current, also
    when another caller ended it a moment ago.
    """
    result = await session.scalars(
        update(ResourceAssignment)
        .where(
            ResourceAssignment.id == assignment_id,
            ResourceAssignment.tenant_id == tenant_id,
            ResourceAssignment.valid_to.is_(None),
        )
        .values(valid_to=now or _now())
        .returning(ResourceAssignment)
        .execution_options(populate_existing=True)
    )
    row = result.first()
    if row is None:
        raise AssignmentNotFoundError(str(assignment_id))
    return row


async def reassign(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    assignment_id: uuid.UUID,
    to_space_id: uuid.UUID,
    changed_by: str,
    reason: str,
    now: datetime | None = None,
) -> ResourceAssignment:
    """Move a current assignment to another space of the same tenant."""
    await _require_space(session, tenant_id, to_space_id)
    when = now or _now()
    old = await end_assignment(session, tenant_id=tenant_id, assignment_id=assignment_id, now=when)
    return await assign(
        session,
        tenant_id=tenant_id,
        space_id=to_space_id,
        kind=old.kind,
        target_id=old.target_id,
        object_id=old.object_id,
        name_at_assignment=old.name_at_assignment,
        access=old.access,
        changed_by=changed_by,
        reason=reason,
        now=when,
    )


async def list_assignments(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    space_id: uuid.UUID | None = None,
    include_history: bool = False,
) -> list[ResourceAssignment]:
    """Current assignments of the tenant (or of one space), or all rows."""
    stmt = select(ResourceAssignment).where(ResourceAssignment.tenant_id == tenant_id)
    if space_id is not None:
        stmt = stmt.where(ResourceAssignment.space_id == space_id)
    if not include_history:
        stmt = stmt.where(ResourceAssignment.valid_to.is_(None))
    result = await session.scalars(
        stmt.order_by(ResourceAssignment.valid_from, ResourceAssignment.id)
    )
    return list(result)
