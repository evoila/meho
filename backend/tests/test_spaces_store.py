# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for the spaces tables and the end-dated history rule."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import Tenant
from meho_backplane.settings import get_settings
from meho_backplane.spaces import store
from meho_backplane.spaces.models import ResourceAssignment, SpaceMember

_ISSUER = "https://idp.example.test/realms/demo"
_T0 = datetime(2026, 1, 1, tzinfo=UTC)
_T1 = _T0 + timedelta(days=1)
_T2 = _T0 + timedelta(days=2)


@pytest.fixture(autouse=True)
def _required_settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    async with get_sessionmaker()() as s:
        yield s


async def _tenant(session: AsyncSession, slug: str) -> uuid.UUID:
    tenant_id = uuid.uuid4()
    session.add(Tenant(id=tenant_id, slug=slug, name=slug))
    await session.commit()
    return tenant_id


async def _space(session: AsyncSession, tenant_id: uuid.UUID, name: str = "lab-a") -> uuid.UUID:
    space = await store.create_space(
        session,
        tenant_id=tenant_id,
        name=name,
        kind="team",
        owner_issuer=_ISSUER,
        owner_sub="sub-1",
        created_by="admin-1",
        now=_T0,
    )
    return space.id


async def _assign(
    session: AsyncSession, tenant_id: uuid.UUID, space_id: uuid.UUID, now: datetime = _T0
) -> ResourceAssignment:
    return await store.assign(
        session,
        tenant_id=tenant_id,
        space_id=space_id,
        kind="target",
        target_id=uuid.uuid4(),
        object_id=None,
        name_at_assignment="demo-target",
        access="own",
        changed_by="admin-1",
        reason="first owner",
        now=now,
    )


async def test_member_change_ends_the_old_row_and_keeps_it(session: AsyncSession) -> None:
    tenant = await _tenant(session, "t-member")
    space = await _space(session, tenant)
    kwargs = {"tenant_id": tenant, "space_id": space, "member_issuer": _ISSUER, "member_sub": "p1"}
    await store.set_member(
        session, **kwargs, role="member", state="active", changed_by="a", reason="join", now=_T0
    )
    await store.set_member(
        session, **kwargs, role="owner", state="active", changed_by="a", reason="promote", now=_T1
    )
    await store.set_member(
        session, **kwargs, role="owner", state="disabled", changed_by="a", reason="left", now=_T2
    )
    await session.commit()

    current = await store.list_members(session, tenant_id=tenant, space_id=space)
    assert [(m.role, m.state) for m in current] == [("owner", "disabled")]
    history = await store.list_members(
        session, tenant_id=tenant, space_id=space, include_history=True
    )
    assert [(m.role, m.valid_to is None) for m in history] == [
        ("member", False),
        ("owner", False),
        ("owner", True),
    ]


async def test_end_member_keeps_the_row_and_a_second_call_is_a_no_op(
    session: AsyncSession,
) -> None:
    tenant = await _tenant(session, "t-end")
    space = await _space(session, tenant)
    kwargs = {"tenant_id": tenant, "space_id": space, "member_issuer": _ISSUER, "member_sub": "p1"}
    await store.set_member(
        session, **kwargs, role="member", state="active", changed_by="a", reason="join", now=_T0
    )
    assert await store.end_member(session, **kwargs, now=_T1) is True
    assert await store.end_member(session, **kwargs, now=_T2) is False
    await session.commit()

    assert await store.list_members(session, tenant_id=tenant, space_id=space) == []
    total = await session.scalar(select(func.count()).select_from(SpaceMember))
    assert total == 1


async def test_two_open_rows_for_one_member_are_refused(session: AsyncSession) -> None:
    tenant = await _tenant(session, "t-open")
    space = await _space(session, tenant)
    for sub in ("p1", "p1"):
        session.add(
            SpaceMember(
                id=uuid.uuid4(),
                tenant_id=tenant,
                space_id=space,
                member_issuer=_ISSUER,
                member_sub=sub,
                role="member",
                state="active",
                valid_from=_T0,
                changed_by="a",
                reason="x",
            )
        )
    with pytest.raises(IntegrityError):
        await session.flush()
    await session.rollback()


async def test_end_assignment_keeps_the_row(session: AsyncSession) -> None:
    tenant = await _tenant(session, "t-assign")
    space = await _space(session, tenant)
    row = await _assign(session, tenant, space)
    await store.end_assignment(session, tenant_id=tenant, assignment_id=row.id, now=_T1)
    await session.commit()

    assert await store.list_assignments(session, tenant_id=tenant) == []
    history = await store.list_assignments(session, tenant_id=tenant, include_history=True)
    assert [a.id for a in history] == [row.id]
    assert history[0].valid_to is not None


async def test_end_assignment_twice_is_an_error(session: AsyncSession) -> None:
    tenant = await _tenant(session, "t-twice")
    space = await _space(session, tenant)
    row = await _assign(session, tenant, space)
    await store.end_assignment(session, tenant_id=tenant, assignment_id=row.id, now=_T1)
    with pytest.raises(store.AssignmentNotFoundError):
        await store.end_assignment(session, tenant_id=tenant, assignment_id=row.id, now=_T2)


async def test_reassign_ends_the_old_row_and_adds_one(session: AsyncSession) -> None:
    tenant = await _tenant(session, "t-move")
    first = await _space(session, tenant, "lab-a")
    second = await _space(session, tenant, "lab-b")
    old = await _assign(session, tenant, first)
    new = await store.reassign(
        session,
        tenant_id=tenant,
        assignment_id=old.id,
        to_space_id=second,
        changed_by="admin-2",
        reason="moved",
        now=_T1,
    )
    await session.commit()

    assert new.id != old.id
    assert (new.space_id, new.target_id, new.name_at_assignment) == (
        second,
        old.target_id,
        "demo-target",
    )
    current = await store.list_assignments(session, tenant_id=tenant)
    assert [a.id for a in current] == [new.id]
    both = await store.list_assignments(session, tenant_id=tenant, include_history=True)
    assert len(both) == 2


async def test_a_deleted_target_keeps_its_assignment_history(session: AsyncSession) -> None:
    tenant = await _tenant(session, "t-gone")
    space = await _space(session, tenant)
    row = await _assign(session, tenant, space)
    await session.commit()
    # The target id points at no row in any table: nothing blocks the history.
    again = await session.get(ResourceAssignment, row.id)
    assert again is not None
    assert again.target_id == row.target_id


async def test_a_tenant_never_sees_or_changes_rows_of_another(session: AsyncSession) -> None:
    mine = await _tenant(session, "t-mine")
    other = await _tenant(session, "t-other")
    space = await _space(session, mine)
    row = await _assign(session, mine, space)
    await store.set_member(
        session,
        tenant_id=mine,
        space_id=space,
        member_issuer=_ISSUER,
        member_sub="p1",
        role="member",
        state="active",
        changed_by="a",
        reason="join",
        now=_T0,
    )
    await session.commit()

    assert await store.list_assignments(session, tenant_id=other) == []
    assert await store.list_assignments(session, tenant_id=other, space_id=space) == []
    with pytest.raises(store.SpaceNotFoundError):
        await store.list_members(session, tenant_id=other, space_id=space)
    with pytest.raises(store.SpaceNotFoundError):
        await store.set_member(
            session,
            tenant_id=other,
            space_id=space,
            member_issuer=_ISSUER,
            member_sub="p2",
            role="member",
            state="active",
            changed_by="a",
            reason="x",
        )
    with pytest.raises(store.AssignmentNotFoundError):
        await store.end_assignment(session, tenant_id=other, assignment_id=row.id)
    other_space = await _space(session, other)
    with pytest.raises(store.SpaceNotFoundError):
        await store.reassign(
            session,
            tenant_id=mine,
            assignment_id=row.id,
            to_space_id=other_space,
            changed_by="a",
            reason="x",
        )
    # The refused calls changed nothing.
    still = await store.list_assignments(session, tenant_id=mine)
    assert [a.id for a in still] == [row.id]


async def test_bad_values_are_refused_by_the_database(session: AsyncSession) -> None:
    tenant = await _tenant(session, "t-bad")
    with pytest.raises(IntegrityError):
        await store.create_space(
            session,
            tenant_id=tenant,
            name="x",
            kind="company",
            owner_issuer=_ISSUER,
            owner_sub="s",
            created_by="a",
        )
    await session.rollback()


async def test_the_same_space_name_twice_in_a_tenant_is_refused_but_not_across(
    session: AsyncSession,
) -> None:
    one = await _tenant(session, "t-one")
    two = await _tenant(session, "t-two")
    await _space(session, one, "same")
    await _space(session, two, "same")
    with pytest.raises(IntegrityError):
        await _space(session, one, "same")
    await session.rollback()


def test_spaces_setting_is_off_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SPACES_ENABLED", raising=False)
    get_settings.cache_clear()
    assert get_settings().spaces_enabled is False
    monkeypatch.setenv("SPACES_ENABLED", "true")
    get_settings.cache_clear()
    assert get_settings().spaces_enabled is True
