# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for ``spaces.import``: buckets, conflicts, preview, apply, ``only``."""

from __future__ import annotations

import copy
import uuid
from collections.abc import AsyncIterator, Iterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import func, select

from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.connectors.spaces.common import SPACES_CONNECTOR_ID
from meho_backplane.connectors.spaces.import_manifest import parse_manifest
from meho_backplane.connectors.spaces.import_op import build_import_preview, spaces_import
from meho_backplane.connectors.spaces.registry import register_spaces_operations
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import Target, Tenant
from meho_backplane.operations import dispatch, reset_dispatcher_caches
from meho_backplane.operations._preview import PreviewContext
from meho_backplane.operations.meta_tools import preview_operation
from meho_backplane.settings import get_settings
from meho_backplane.spaces import store
from meho_backplane.spaces.models import ResourceAssignment, Space, SpaceMember

_ISSUER = "https://idp.example.test/realms/demo"


def _p(sub: str) -> dict[str, str]:
    return {"issuer": _ISSUER, "subject": sub, "display_name": f"Name of {sub}"}


def _key(sub: str) -> tuple[str, str]:
    return (_ISSUER, sub)


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.setenv("SPACES_ENABLED", "true")
    get_settings.cache_clear()
    reset_dispatcher_caches()
    yield
    get_settings.cache_clear()
    reset_dispatcher_caches()


def _operator(tenant_id: uuid.UUID, role: TenantRole = TenantRole.TENANT_ADMIN) -> Operator:
    return Operator(
        sub="admin-1",
        name=None,
        email=None,
        raw_jwt="fake.jwt.value",
        tenant_id=tenant_id,
        tenant_role=role,
        principal_kind=PrincipalKind.USER,
        web_session=True,
    )


class _World:
    def __init__(self) -> None:
        self.tenant = uuid.uuid4()
        self.other = uuid.uuid4()


@pytest.fixture
async def world() -> AsyncIterator[_World]:
    w = _World()
    async with get_sessionmaker()() as session, session.begin():
        session.add(Tenant(id=w.tenant, slug="tenant-a", name="Tenant A"))
        session.add(Tenant(id=w.other, slug="tenant-b", name="Tenant B"))
        for tenant, name in ((w.tenant, "demo-t1"), (w.tenant, "demo-t2"), (w.tenant, "demo-t3")):
            session.add(
                Target(
                    id=uuid.uuid4(),
                    tenant_id=tenant,
                    name=name,
                    product="demo-product",
                    version="1.0",
                    host="lab.example.test",
                )
            )
        session.add(
            Target(
                id=uuid.uuid4(),
                tenant_id=w.other,
                name="demo-other",
                product="demo-product",
                version="1.0",
                host="lab.example.test",
            )
        )
    yield w


def _manifest() -> dict[str, Any]:
    """Six labs. Known result: see ``test_known_conflicts_land_in_the_expected_buckets``."""
    return {
        "environments": [
            {"name": "lab-ok", "targets": ["demo-t1"], "owner": _p("ann"), "users": [_p("bob")]},
            {"name": "lab-noowner", "targets": ["demo-t2"], "users": [_p("bob")]},
            {"name": "lab-ghost", "targets": [], "owner": _p("ghost"), "users": []},
            {
                "name": "lab-two",
                "targets": [],
                "owner": [_p("ann"), _p("bob")],
                "users": [],
            },
            {
                "name": "lab-partial",
                "targets": ["demo-unknown", "demo-t3", "demo-t3"],
                "owner": _p("cy"),
                "users": [_p("nobody"), _p("bob")],
            },
        ],
        "people": [_p("ann"), _p("bob"), _p("cy")],
    }


def _params(manifest: dict[str, Any], **extra: Any) -> dict[str, Any]:
    return {"manifest": manifest, "reason": "first load", **extra}


async def _count(model: Any) -> int:
    async with get_sessionmaker()() as session:
        return int(await session.scalar(select(func.count()).select_from(model)) or 0)


def _total(counts: dict[str, dict[str, int]]) -> int:
    return sum(sum(per_kind.values()) for per_kind in counts.values())


def _codes(answer: dict[str, Any]) -> dict[tuple[str, str], str]:
    return {(c["kind"], c["ref"]): c["code"] for c in answer["conflicts"]}


@pytest.mark.asyncio
async def test_known_conflicts_land_in_the_expected_buckets(world: _World) -> None:
    answer = await spaces_import(_operator(world.tenant), None, _params(_manifest()))
    counts = answer["counts"]
    # 5 labs + 3 people + 5 target rows (one is listed twice) + 4 user rows
    assert answer["total_rows"] == 5 + 3 + 5 + 4
    assert _total(counts) == answer["total_rows"]
    assert counts["space"] == {"created": 2, "changed": 0, "unchanged": 0, "conflict": 3}
    assert counts["person"] == {"created": 0, "changed": 0, "unchanged": 3, "conflict": 0}
    assert counts["assignment"] == {"created": 2, "changed": 0, "unchanged": 1, "conflict": 2}
    assert counts["member"] == {"created": 2, "changed": 0, "unchanged": 0, "conflict": 2}
    codes = _codes(answer)
    assert codes[("space", "lab-noowner")] == "no_owner"
    assert codes[("space", "lab-ghost")] == "owner_has_no_account"
    assert codes[("space", "lab-two")] == "two_owners"
    assert codes[("assignment", "demo-unknown")] == "unknown_target"
    assert codes[("assignment", "demo-t2")] == "lab_not_imported"
    assert codes[("member", f"nobody ({_ISSUER})")] == "no_account"
    assert answer["conflict_count"] == len(answer["conflicts"]) == 7
    assert await _count(Space) == 2
    assert await _count(ResourceAssignment) == 2


@pytest.mark.asyncio
async def test_preview_and_apply_agree(world: _World) -> None:
    ctx = PreviewContext(
        descriptor=MagicMock(op_id="spaces.import"),
        connector_instance=None,
        operator=_operator(world.tenant),
        target=None,
        params=_params(_manifest()),
    )
    preview = await build_import_preview(ctx)
    assert preview is not None
    assert await _count(Space) == 0
    assert await _count(SpaceMember) == 0
    answer = await spaces_import(_operator(world.tenant), None, _params(_manifest()))
    for key in ("total_rows", "counts", "conflict_count", "conflicts"):
        assert preview[key] == answer[key]


@pytest.mark.asyncio
async def test_second_run_changes_nothing(world: _World) -> None:
    await spaces_import(_operator(world.tenant), None, _params(_manifest()))
    before = (await _count(Space), await _count(SpaceMember), await _count(ResourceAssignment))
    again = await spaces_import(_operator(world.tenant), None, _params(_manifest()))
    after = (await _count(Space), await _count(SpaceMember), await _count(ResourceAssignment))
    assert before == after
    assert again["labs_written"] == 0
    for kind in ("space", "assignment", "member"):
        assert again["counts"][kind]["created"] == 0
        assert again["counts"][kind]["changed"] == 0
    assert again["counts"]["space"]["unchanged"] == 2
    assert _total(again["counts"]) == again["total_rows"]


@pytest.mark.asyncio
async def test_only_imports_one_lab_and_leaves_the_others(world: _World) -> None:
    answer = await spaces_import(
        _operator(world.tenant), None, _params(_manifest(), only=["lab-ok"])
    )
    assert answer["counts"]["space"] == {"created": 1, "changed": 0, "unchanged": 0, "conflict": 0}
    assert answer["counts"]["assignment"]["created"] == 1
    assert answer["conflict_count"] == 0
    async with get_sessionmaker()() as session:
        names = list(await session.scalars(select(Space.name)))
    assert names == ["lab-ok"]
    with pytest.raises(ValueError, match="not in the manifest"):
        await spaces_import(_operator(world.tenant), None, _params(_manifest(), only=["nope"]))


@pytest.mark.asyncio
async def test_import_writes_records_with_who_and_why(world: _World) -> None:
    await spaces_import(_operator(world.tenant), None, _params(_manifest(), only=["lab-ok"]))
    async with get_sessionmaker()() as session:
        space = (await session.scalars(select(Space))).one()
        members = {(m.member_sub, m.role): m for m in await session.scalars(select(SpaceMember))}
        assignment = (await session.scalars(select(ResourceAssignment))).one()
    assert (space.owner_issuer, space.owner_sub, space.created_by) == (_ISSUER, "ann", "admin-1")
    assert set(members) == {("ann", "owner"), ("bob", "member")}
    assert all(m.changed_by == "admin-1" and m.reason == "first load" for m in members.values())
    assert assignment.name_at_assignment == "demo-t1"
    assert assignment.reason == "first load"


@pytest.mark.asyncio
async def test_import_never_brings_back_what_meho_ended(world: _World) -> None:
    await spaces_import(_operator(world.tenant), None, _params(_manifest(), only=["lab-ok"]))
    async with get_sessionmaker()() as session, session.begin():
        space = (await session.scalars(select(Space))).one()
        await store.set_member(
            session,
            tenant_id=world.tenant,
            space_id=space.id,
            member_issuer=_ISSUER,
            member_sub="bob",
            role="member",
            state="disabled",
            changed_by="admin-1",
            reason="left",
        )
        old = (await session.scalars(select(ResourceAssignment))).one()
        other = await store.create_space(
            session,
            tenant_id=world.tenant,
            name="lab-new-home",
            kind="team",
            owner_issuer=_ISSUER,
            owner_sub="cy",
            created_by="admin-1",
        )
        await store.reassign(
            session,
            tenant_id=world.tenant,
            assignment_id=old.id,
            to_space_id=other.id,
            changed_by="admin-1",
            reason="moved",
        )
    before = (await _count(SpaceMember), await _count(ResourceAssignment))
    answer = await spaces_import(
        _operator(world.tenant), None, _params(_manifest(), only=["lab-ok"])
    )
    codes = _codes(answer)
    assert codes[("member", f"bob ({_ISSUER})")] == "disabled_in_meho"
    assert codes[("assignment", "demo-t1")] == "assigned_elsewhere_in_meho"
    assert (await _count(SpaceMember), await _count(ResourceAssignment)) == before


@pytest.mark.asyncio
async def test_other_owner_or_disabled_space_in_meho_blocks_the_lab(world: _World) -> None:
    async with get_sessionmaker()() as session, session.begin():
        await store.create_space(
            session,
            tenant_id=world.tenant,
            name="lab-ok",
            kind="team",
            owner_issuer=_ISSUER,
            owner_sub="bob",
            created_by="admin-1",
        )
    answer = await spaces_import(
        _operator(world.tenant), None, _params(_manifest(), only=["lab-ok"])
    )
    assert _codes(answer)[("space", "lab-ok")] == "owner_differs_in_meho"
    assert answer["counts"]["space"]["conflict"] == 1
    assert _total(answer["counts"]) == answer["total_rows"]
    assert await _count(ResourceAssignment) == 0


@pytest.mark.asyncio
async def test_existing_space_without_owner_row_is_changed(world: _World) -> None:
    async with get_sessionmaker()() as session, session.begin():
        await store.create_space(
            session,
            tenant_id=world.tenant,
            name="lab-ok",
            kind="team",
            owner_issuer=_ISSUER,
            owner_sub="ann",
            created_by="admin-1",
        )
    answer = await spaces_import(
        _operator(world.tenant), None, _params(_manifest(), only=["lab-ok"])
    )
    assert answer["counts"]["space"] == {"created": 0, "changed": 1, "unchanged": 0, "conflict": 0}
    again = await spaces_import(
        _operator(world.tenant), None, _params(_manifest(), only=["lab-ok"])
    )
    assert again["counts"]["space"]["unchanged"] == 1


@pytest.mark.asyncio
async def test_same_target_in_two_labs_and_other_tenants_target(world: _World) -> None:
    manifest = {
        "environments": [
            {"name": "lab-x", "targets": ["demo-t1", "demo-other"], "owner": _p("ann")},
            {"name": "lab-y", "targets": ["demo-t1"], "owner": _p("bob")},
        ],
        "people": [_p("ann"), _p("bob")],
    }
    answer = await spaces_import(_operator(world.tenant), None, _params(manifest))
    assert answer["counts"]["assignment"] == {
        "created": 0,
        "changed": 0,
        "unchanged": 0,
        "conflict": 3,
    }
    codes = [c["code"] for c in answer["conflicts"] if c["kind"] == "assignment"]
    assert sorted(codes) == ["target_in_two_labs", "target_in_two_labs", "unknown_target"]
    assert await _count(ResourceAssignment) == 0


@pytest.mark.asyncio
async def test_same_lab_name_twice_is_a_conflict_not_a_write(world: _World) -> None:
    manifest = {
        "environments": [
            {"name": "lab-x", "owner": _p("ann")},
            {"name": "lab-x", "owner": _p("bob")},
        ],
        "people": [_p("ann"), _p("bob")],
    }
    answer = await spaces_import(_operator(world.tenant), None, _params(manifest))
    assert answer["counts"]["space"]["conflict"] == 2
    assert await _count(Space) == 0


@pytest.mark.asyncio
async def test_another_tenants_space_with_the_same_name_is_not_touched(world: _World) -> None:
    async with get_sessionmaker()() as session, session.begin():
        await store.create_space(
            session,
            tenant_id=world.other,
            name="lab-ok",
            kind="team",
            owner_issuer=_ISSUER,
            owner_sub="zed",
            created_by="admin-9",
        )
    answer = await spaces_import(
        _operator(world.tenant), None, _params(_manifest(), only=["lab-ok"])
    )
    assert answer["counts"]["space"]["created"] == 1
    assert await _count(Space) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bad",
    [
        None,
        [],
        {"people": []},
        {"environments": "x"},
        {"environments": [{"targets": []}]},
        {"environments": [{"name": "a", "owner": {"issuer": "i"}}]},
        {"environments": [], "people": [{"issuer": "", "subject": "s"}]},
        {"environments": [{"name": "a", "targets": "t"}]},
    ],
)
async def test_bad_manifest_shape_is_refused_and_writes_nothing(world: _World, bad: Any) -> None:
    with pytest.raises(ValueError, match=r"manifest|environments|issuer|name|targets|people"):
        await spaces_import(_operator(world.tenant), None, _params(bad))
    assert await _count(Space) == 0


@pytest.mark.asyncio
async def test_empty_manifest_and_missing_reason(world: _World) -> None:
    answer = await spaces_import(_operator(world.tenant), None, _params({"environments": []}))
    assert answer["total_rows"] == 0
    with pytest.raises(ValueError, match="reason"):
        await spaces_import(_operator(world.tenant), None, {"manifest": _manifest(), "reason": " "})


@pytest.mark.asyncio
async def test_import_refuses_while_spaces_are_off(
    world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SPACES_ENABLED", "false")
    get_settings.cache_clear()
    with pytest.raises(ValueError, match="Spaces are off"):
        await spaces_import(_operator(world.tenant), None, _params(_manifest()))
    ctx = PreviewContext(
        descriptor=MagicMock(op_id="spaces.import"),
        connector_instance=None,
        operator=_operator(world.tenant),
        target=None,
        params=_params(_manifest()),
    )
    with pytest.raises(ValueError, match="Spaces are off"):
        await build_import_preview(ctx)


def test_the_manifest_is_not_changed_by_parsing() -> None:
    raw = _manifest()
    snapshot = copy.deepcopy(raw)
    parsed = parse_manifest(raw)
    assert raw == snapshot
    assert _key("ann") in parsed.people


async def _register() -> None:
    service = AsyncMock()
    service.encode_one.return_value = [0.1] * 384
    service.encode.return_value = [[0.1] * 384]
    service.dimension = 384
    await register_spaces_operations(embedding_service=service)


@pytest.mark.asyncio
async def test_dispatch_waits_for_approval_and_a_lower_role_is_denied(world: _World) -> None:
    await _register()
    denied = await dispatch(
        operator=_operator(world.tenant, TenantRole.OPERATOR),
        connector_id=SPACES_CONNECTOR_ID,
        op_id="spaces.import",
        target=None,
        params=_params(_manifest()),
    )
    assert denied.status != "ok"
    parked = await dispatch(
        operator=_operator(world.tenant),
        connector_id=SPACES_CONNECTOR_ID,
        op_id="spaces.import",
        target=None,
        params=_params(_manifest(), only=["lab-ok"]),
    )
    assert parked.status == "awaiting_approval", parked.error
    assert await _count(Space) == 0


@pytest.mark.asyncio
async def test_owner_list_passes_the_schema_on_the_public_paths(world: _World) -> None:
    """The manifest has a lab with two owners. The schema takes it; the plan lists it."""
    await _register()
    parked = await dispatch(
        operator=_operator(world.tenant),
        connector_id=SPACES_CONNECTOR_ID,
        op_id="spaces.import",
        target=None,
        params=_params(_manifest(), only=["lab-ok"]),
    )
    assert parked.status == "awaiting_approval", parked.error
    envelope = await preview_operation(
        _operator(world.tenant),
        {
            "connector_id": SPACES_CONNECTOR_ID,
            "op_id": "spaces.import",
            "params": _params(_manifest()),
        },
    )
    assert envelope["status"] == "ok", envelope
    assert envelope["plan_shown"] is True
    plan = envelope["proposed_effect"]["preview"]
    assert _codes(plan)[("space", "lab-two")] == "two_owners"
    assert await _count(Space) == 0


async def _end_the_assignment(tenant_id: uuid.UUID) -> None:
    async with get_sessionmaker()() as session, session.begin():
        row = (await session.scalars(select(ResourceAssignment))).one()
        await store.end_assignment(session, tenant_id=tenant_id, assignment_id=row.id)


@pytest.mark.asyncio
async def test_import_does_not_bring_back_an_ended_assignment(world: _World) -> None:
    params = _params(_manifest(), only=["lab-ok"])
    await spaces_import(_operator(world.tenant), None, params)
    await _end_the_assignment(world.tenant)
    ctx = PreviewContext(
        descriptor=MagicMock(op_id="spaces.import"),
        connector_instance=None,
        operator=_operator(world.tenant),
        target=None,
        params=params,
    )
    preview = await build_import_preview(ctx)
    assert preview is not None
    assert _codes(preview)[("assignment", "demo-t1")] == "ended_in_meho"
    answer = await spaces_import(_operator(world.tenant), None, params)
    assert _codes(answer)[("assignment", "demo-t1")] == "ended_in_meho"
    assert answer["counts"]["assignment"]["created"] == 0
    assert _total(answer["counts"]) == answer["total_rows"]
    async with get_sessionmaker()() as session:
        current = await store.list_assignments(session, tenant_id=world.tenant)
    assert current == []
    assert await _count(ResourceAssignment) == 1


@pytest.mark.asyncio
async def test_import_does_not_bring_back_an_ended_membership(world: _World) -> None:
    params = _params(_manifest(), only=["lab-ok"])
    await spaces_import(_operator(world.tenant), None, params)
    async with get_sessionmaker()() as session, session.begin():
        space = (await session.scalars(select(Space))).one()
        await store.end_member(
            session,
            tenant_id=world.tenant,
            space_id=space.id,
            member_issuer=_ISSUER,
            member_sub="bob",
        )
    answer = await spaces_import(_operator(world.tenant), None, params)
    assert _codes(answer)[("member", f"bob ({_ISSUER})")] == "ended_in_meho"
    assert answer["counts"]["member"]["created"] == 0
    async with get_sessionmaker()() as session, session.begin():
        await store.end_member(
            session,
            tenant_id=world.tenant,
            space_id=space.id,
            member_issuer=_ISSUER,
            member_sub="ann",
        )
    before = await _count(SpaceMember)
    again = await spaces_import(_operator(world.tenant), None, params)
    assert _codes(again)[("space", "lab-ok")] == "owner_ended_in_meho"
    assert again["labs_written"] == 0
    assert await _count(SpaceMember) == before
