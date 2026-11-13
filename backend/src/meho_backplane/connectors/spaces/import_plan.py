# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Plan and apply the import of ``spaces.import``.

One function plans a lab (:func:`plan_lab`). The preview and the apply both
call it, so they cannot disagree. Every input row lands in exactly one
bucket: ``created``, ``changed``, ``unchanged`` or ``conflict``. The rows are
the labs (kind ``space``), the people, the target references and the users.
The owner of a lab is part of its ``space`` row.

A lab with a lab-level conflict (no owner, an owner without an account, two
owners, the same name twice, a disabled space or owner in MEHO, or another
owner in MEHO) is not imported: its space row and all its other rows are
conflicts. A conflict on one target or one user leaves the rest of the lab
to be imported.

The import never brings back what an administrator ended: a person who is
disabled in MEHO, a target that now belongs to another space, a disabled
space, or another owner stay as they are and are listed.
"""

from __future__ import annotations

import uuid
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Final

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.connectors.spaces.import_manifest import Lab, Manifest, PersonKey
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import Target
from meho_backplane.spaces import store
from meho_backplane.spaces.models import Space

__all__ = ["BUCKETS", "KINDS", "ImportResult", "Row", "apply_import", "preview_import"]

BUCKETS: Final[tuple[str, ...]] = ("created", "changed", "unchanged", "conflict")
KINDS: Final[tuple[str, ...]] = ("space", "person", "assignment", "member")


@dataclass(frozen=True)
class Row:
    """The fate of one input row."""

    kind: str
    ref: str
    lab: str | None
    bucket: str
    code: str | None = None
    detail: str | None = None


@dataclass
class _Action:
    """One write of a lab. ``op`` is create_space, set_member or assign."""

    op: str
    args: dict[str, Any]


@dataclass
class _LabPlan:
    rows: list[Row] = field(default_factory=list)
    actions: list[_Action] = field(default_factory=list)


@dataclass
class _Context:
    """What a lab plan needs from the whole manifest."""

    tenant_id: uuid.UUID
    people: frozenset[PersonKey]
    targets: dict[str, uuid.UUID]
    lab_name_count: Counter[str]
    target_lab_count: Counter[str]


@dataclass
class ImportResult:
    """Every row with its bucket, and how many labs were written."""

    rows: list[Row]
    labs_written: int = 0

    def counts(self) -> dict[str, dict[str, int]]:
        out = {kind: dict.fromkeys(BUCKETS, 0) for kind in KINDS}
        for row in self.rows:
            out[row.kind][row.bucket] += 1
        return out

    def conflicts(self) -> list[dict[str, Any]]:
        return [
            {"kind": r.kind, "ref": r.ref, "lab": r.lab, "code": r.code, "detail": r.detail}
            for r in self.rows
            if r.bucket == "conflict"
        ]


def _who(key: PersonKey) -> str:
    return f"{key[1]} ({key[0]})"


def _blocked(lab: Lab, code: str, detail: str) -> _LabPlan:
    """A lab that is not imported: every row is a conflict."""
    plan = _LabPlan([Row("space", lab.name, lab.name, "conflict", code, detail)])
    for ref in lab.targets:
        plan.rows.append(
            Row(
                "assignment",
                ref,
                lab.name,
                "conflict",
                "lab_not_imported",
                "The lab has a conflict.",
            )
        )
    for user in lab.users:
        plan.rows.append(
            Row(
                "member",
                _who(user),
                lab.name,
                "conflict",
                "lab_not_imported",
                "The lab has a conflict.",
            )
        )
    return plan


def _lab_conflict(lab: Lab, ctx: _Context) -> tuple[str, str] | None:
    """The lab-level conflict that stops the whole lab, if any."""
    if ctx.lab_name_count[lab.name] > 1:
        return "duplicate_lab", "The manifest has this lab name more than once."
    owners = set(lab.owners)
    if not owners:
        return "no_owner", "The lab has no owner."
    if len(owners) > 1:
        return "two_owners", "The lab has more than one owner."
    (owner,) = owners
    if owner not in ctx.people:
        return "owner_has_no_account", f"{_who(owner)} is not in the people list."
    return None


async def _plan_users(
    session: AsyncSession, lab: Lab, owner: PersonKey, space: Space | None, ctx: _Context
) -> tuple[list[Row], list[_Action]]:
    rows: list[Row] = []
    actions: list[_Action] = []
    current: dict[PersonKey, Any] = {}
    if space is not None:
        members = await store.list_members(session, tenant_id=ctx.tenant_id, space_id=space.id)
        current = {(m.member_issuer, m.member_sub): m for m in members}
    seen: set[PersonKey] = {owner}
    for user in lab.users:
        ref = _who(user)
        if user in seen:
            rows.append(Row("member", ref, lab.name, "unchanged", "duplicate", "Listed twice."))
            continue
        seen.add(user)
        if user not in ctx.people:
            rows.append(
                Row("member", ref, lab.name, "conflict", "no_account", "Not in the people list.")
            )
            continue
        member = current.get(user)
        if member is None:
            rows.append(Row("member", ref, lab.name, "created"))
            actions.append(_Action("set_member", {"key": user, "role": "member"}))
        elif member.state == "disabled":
            rows.append(
                Row(
                    "member",
                    ref,
                    lab.name,
                    "conflict",
                    "disabled_in_meho",
                    "Left this lab in MEHO.",
                )
            )
        else:
            rows.append(Row("member", ref, lab.name, "unchanged"))
    return rows, actions


async def _plan_targets(
    session: AsyncSession, lab: Lab, space: Space | None, ctx: _Context
) -> tuple[list[Row], list[_Action]]:
    rows: list[Row] = []
    actions: list[_Action] = []
    owned: dict[uuid.UUID, uuid.UUID] = {}
    for a in await store.list_assignments(session, tenant_id=ctx.tenant_id):
        if a.kind == "target" and a.target_id is not None:
            owned[a.target_id] = a.space_id
    seen: set[str] = set()
    for ref in lab.targets:
        if ref in seen:
            rows.append(Row("assignment", ref, lab.name, "unchanged", "duplicate", "Listed twice."))
            continue
        seen.add(ref)
        target_id = ctx.targets.get(ref)
        if target_id is None:
            rows.append(
                Row(
                    "assignment",
                    ref,
                    lab.name,
                    "conflict",
                    "unknown_target",
                    "MEHO has no such target.",
                )
            )
        elif ctx.target_lab_count[ref] > 1:
            rows.append(
                Row(
                    "assignment",
                    ref,
                    lab.name,
                    "conflict",
                    "target_in_two_labs",
                    "Two labs list it.",
                )
            )
        elif target_id not in owned:
            rows.append(Row("assignment", ref, lab.name, "created"))
            actions.append(_Action("assign", {"target_id": target_id, "name": ref}))
        elif space is not None and owned[target_id] == space.id:
            rows.append(Row("assignment", ref, lab.name, "unchanged"))
        else:
            rows.append(
                Row(
                    "assignment",
                    ref,
                    lab.name,
                    "conflict",
                    "assigned_elsewhere_in_meho",
                    "MEHO assigns it to another space now.",
                )
            )
    return rows, actions


async def _plan_space(
    session: AsyncSession, lab: Lab, owner: PersonKey, space: Space | None, ctx: _Context
) -> tuple[Row, list[_Action]] | _LabPlan:
    """The space row and its writes, or a blocked lab."""
    if space is None:
        return Row("space", lab.name, lab.name, "created"), [
            _Action("create_space", {"name": lab.name, "owner": owner}),
            _Action("set_member", {"key": owner, "role": "owner"}),
        ]
    if space.state != "active":
        return _blocked(lab, "space_disabled", "The space is disabled in MEHO.")
    if (space.owner_issuer, space.owner_sub) != owner:
        return _blocked(lab, "owner_differs_in_meho", "MEHO has another owner for this space.")
    members = await store.list_members(session, tenant_id=ctx.tenant_id, space_id=space.id)
    mine = next((m for m in members if (m.member_issuer, m.member_sub) == owner), None)
    if mine is None or (mine.state == "active" and mine.role != "owner"):
        return Row("space", lab.name, lab.name, "changed"), [
            _Action("set_member", {"key": owner, "role": "owner"})
        ]
    if mine.state == "disabled":
        return _blocked(lab, "owner_disabled_in_meho", "The owner left this lab in MEHO.")
    return Row("space", lab.name, lab.name, "unchanged"), []


async def _plan_lab(session: AsyncSession, lab: Lab, ctx: _Context) -> _LabPlan:
    conflict = _lab_conflict(lab, ctx)
    if conflict is not None:
        return _blocked(lab, *conflict)
    (owner,) = set(lab.owners)
    space = await session.scalar(
        select(Space).where(Space.tenant_id == ctx.tenant_id, Space.name == lab.name)
    )
    planned = await _plan_space(session, lab, owner, space, ctx)
    if isinstance(planned, _LabPlan):
        return planned
    space_row, actions = planned
    user_rows, user_actions = await _plan_users(session, lab, owner, space, ctx)
    target_rows, target_actions = await _plan_targets(session, lab, space, ctx)
    return _LabPlan(
        [space_row, *user_rows, *target_rows], [*actions, *user_actions, *target_actions]
    )


async def _context(session: AsyncSession, tenant_id: uuid.UUID, manifest: Manifest) -> _Context:
    found = await session.execute(
        select(Target.name, Target.id).where(
            Target.tenant_id == tenant_id, Target.deleted_at.is_(None)
        )
    )
    refs: Counter[str] = Counter()
    for lab in manifest.labs:
        refs.update(set(lab.targets))
    return _Context(
        tenant_id=tenant_id,
        people=frozenset(manifest.people),
        targets=dict(found.tuples().all()),
        lab_name_count=Counter(lab.name for lab in manifest.labs),
        target_lab_count=refs,
    )


def _people_rows(manifest: Manifest) -> list[Row]:
    """MEHO keeps no record of a person, so these rows change nothing."""
    return [
        Row("person", _who(p), None, "unchanged", "no_record_kept", "Only vouches for the account.")
        for p in manifest.people
    ]


async def preview_import(
    session: AsyncSession, tenant_id: uuid.UUID, manifest: Manifest
) -> ImportResult:
    """Plan every lab and write nothing."""
    ctx = await _context(session, tenant_id, manifest)
    rows = _people_rows(manifest)
    for lab in manifest.labs:
        rows.extend((await _plan_lab(session, lab, ctx)).rows)
    return ImportResult(rows)


async def _run_actions(
    session: AsyncSession, plan: _LabPlan, ctx: _Context, lab: Lab, who: tuple[str, str]
) -> None:
    changed_by, reason = who
    space_id = await session.scalar(
        select(Space.id).where(Space.tenant_id == ctx.tenant_id, Space.name == lab.name)
    )
    for action in plan.actions:
        args = action.args
        if action.op == "create_space":
            issuer, sub = args["owner"]
            space = await store.create_space(
                session,
                tenant_id=ctx.tenant_id,
                name=args["name"],
                kind="team",
                owner_issuer=issuer,
                owner_sub=sub,
                created_by=changed_by,
            )
            space_id = space.id
        elif space_id is None:
            raise RuntimeError("the plan writes into a space that does not exist")
        elif action.op == "set_member":
            issuer, sub = args["key"]
            await store.set_member(
                session,
                tenant_id=ctx.tenant_id,
                space_id=space_id,
                member_issuer=issuer,
                member_sub=sub,
                role=args["role"],
                state="active",
                changed_by=changed_by,
                reason=reason,
            )
        else:
            await store.assign(
                session,
                tenant_id=ctx.tenant_id,
                space_id=space_id,
                kind="target",
                target_id=args["target_id"],
                object_id=None,
                name_at_assignment=args["name"],
                access="own",
                changed_by=changed_by,
                reason=reason,
            )


async def apply_import(
    tenant_id: uuid.UUID, manifest: Manifest, *, changed_by: str, reason: str
) -> ImportResult:
    """Write the import, one transaction per lab. Safe to run twice."""
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        ctx = await _context(session, tenant_id, manifest)
    rows = _people_rows(manifest)
    written = 0
    for lab in manifest.labs:
        try:
            async with sessionmaker() as session, session.begin():
                plan = await _plan_lab(session, lab, ctx)
                await _run_actions(session, plan, ctx, lab, (changed_by, reason))
        except SQLAlchemyError:
            rows.extend(_blocked(lab, "write_failed", "The lab could not be written.").rows)
            continue
        rows.extend(plan.rows)
        written += 1 if plan.actions else 0
    return ImportResult(rows, labs_written=written)
