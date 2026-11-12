# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for the ``spaces.*`` typed operations on ``spaces-records-1.x``."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest
from sqlalchemy import func, select

from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.connectors.spaces import registry as spaces_registry
from meho_backplane.connectors.spaces.common import SPACES_CONNECTOR_ID
from meho_backplane.connectors.spaces.overview import health_state, spaces_overview
from meho_backplane.connectors.spaces.registry import register_spaces_operations
from meho_backplane.connectors.spaces.writes import (
    spaces_assign,
    spaces_member_disable,
    spaces_reassign,
    spaces_unassign,
)
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import (
    EndpointDescriptor,
    Sensor,
    SensorCadenceKind,
    SensorSeverity,
    SensorStatus,
    Target,
    Tenant,
)
from meho_backplane.operations import dispatch, reset_dispatcher_caches
from meho_backplane.settings import get_settings
from meho_backplane.spaces import store
from meho_backplane.spaces.models import ResourceAssignment, SpaceMember

_ISSUER = "https://idp.example.test/realms/demo"
_ASSERTION = {
    "select": {"path": "$.count"},
    "compare": {"type": "threshold", "op": "lt", "critical": 10},
}


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


def _operator(
    tenant_id: uuid.UUID,
    role: TenantRole = TenantRole.TENANT_ADMIN,
    kind: PrincipalKind = PrincipalKind.USER,
    web: bool = False,
) -> Operator:
    return Operator(
        sub="admin-1",
        name=None,
        email=None,
        raw_jwt="fake.jwt.value",
        tenant_id=tenant_id,
        tenant_role=role,
        principal_kind=kind,
        web_session=web,
    )


class _World:
    """Two tenants, each with one space, and one target in tenant A."""

    def __init__(self) -> None:
        self.tenant_a = uuid.uuid4()
        self.tenant_b = uuid.uuid4()
        self.space_a = uuid.uuid4()
        self.space_a2 = uuid.uuid4()
        self.space_b = uuid.uuid4()
        self.target_a = uuid.uuid4()
        self.target_b = uuid.uuid4()


async def _add_target(
    tenant_id: uuid.UUID,
    target_id: uuid.UUID,
    name: str,
    *,
    version: str | None = "9.0",
    fingerprint: dict[str, Any] | None = None,
) -> None:
    async with get_sessionmaker()() as session, session.begin():
        session.add(
            Target(
                id=target_id,
                tenant_id=tenant_id,
                name=name,
                product="demo-product",
                version=version,
                host="lab.example.test",
                fingerprint=fingerprint,
            )
        )


@pytest.fixture
async def world() -> AsyncIterator[_World]:
    w = _World()
    async with get_sessionmaker()() as session, session.begin():
        session.add(Tenant(id=w.tenant_a, slug="tenant-a", name="Tenant A"))
        session.add(Tenant(id=w.tenant_b, slug="tenant-b", name="Tenant B"))
    async with get_sessionmaker()() as session, session.begin():
        for tenant, space_id, name in (
            (w.tenant_a, w.space_a, "lab-a"),
            (w.tenant_a, w.space_a2, "lab-a2"),
            (w.tenant_b, w.space_b, "lab-b"),
        ):
            space = await store.create_space(
                session,
                tenant_id=tenant,
                name=name,
                kind="team",
                owner_issuer=_ISSUER,
                owner_sub="owner-1",
                created_by="admin-1",
            )
            space.id = space_id
    await _add_target(w.tenant_a, w.target_a, "demo-lab-a")
    await _add_target(w.tenant_b, w.target_b, "demo-lab-b")
    yield w


async def _count(model: Any) -> int:
    async with get_sessionmaker()() as session:
        return int(await session.scalar(select(func.count()).select_from(model)) or 0)


async def _rows(model: Any) -> list[Any]:
    async with get_sessionmaker()() as session:
        return list(await session.scalars(select(model)))


# ---------------------------------------------------------------------------
# spaces.assign
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_assign_target_defaults_name_and_records_who_and_why(world: _World) -> None:
    result = await spaces_assign(
        _operator(world.tenant_a),
        None,
        {
            "space_id": str(world.space_a),
            "kind": "target",
            "target_id": str(world.target_a),
            "reason": "lab belongs here",
        },
    )

    assert result["status"] == "assigned"
    assert result["assignment"]["name"] == "demo-lab-a"
    (row,) = await _rows(ResourceAssignment)
    assert (row.changed_by, row.reason, row.valid_to) == ("admin-1", "lab belongs here", None)


@pytest.mark.asyncio
async def test_assign_twice_changes_nothing_the_second_time(world: _World) -> None:
    params = {
        "space_id": str(world.space_a),
        "kind": "target",
        "target_id": str(world.target_a),
        "reason": "first",
    }
    first = await spaces_assign(_operator(world.tenant_a), None, params)
    second = await spaces_assign(_operator(world.tenant_a), None, params)

    assert second["status"] == "unchanged"
    assert second["assignment"]["assignment_id"] == first["assignment"]["assignment_id"]
    assert await _count(ResourceAssignment) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "extra",
    [
        {"kind": "target"},
        {"kind": "object", "name": "vm-one"},
        {"kind": "service"},
        {"kind": "target", "target_id": "not-a-uuid"},
        {"kind": "fact", "name": "x", "reason": "   "},
    ],
)
async def test_assign_refuses_bad_input(world: _World, extra: dict[str, Any]) -> None:
    params = {"space_id": str(world.space_a), "reason": "why", **extra}
    with pytest.raises(ValueError):
        await spaces_assign(_operator(world.tenant_a), None, params)
    assert await _count(ResourceAssignment) == 0


@pytest.mark.asyncio
async def test_assign_machine_service_and_fact(world: _World) -> None:
    for params in (
        {"kind": "object", "object_id": "vm-1", "name": "web machine"},
        {"kind": "service", "name": "web service"},
        {"kind": "fact", "name": "owner phone"},
    ):
        result = await spaces_assign(
            _operator(world.tenant_a),
            None,
            {"space_id": str(world.space_a), "reason": "why", **params},
        )
        assert result["status"] == "assigned"
    assert await _count(ResourceAssignment) == 3


@pytest.mark.asyncio
async def test_assign_never_crosses_tenants(world: _World) -> None:
    operator = _operator(world.tenant_a)
    with pytest.raises(ValueError, match="No target"):
        await spaces_assign(
            operator,
            None,
            {
                "space_id": str(world.space_a),
                "kind": "target",
                "target_id": str(world.target_b),
                "reason": "why",
            },
        )
    with pytest.raises(ValueError, match="No space"):
        await spaces_assign(
            operator,
            None,
            {
                "space_id": str(world.space_b),
                "kind": "target",
                "target_id": str(world.target_a),
                "reason": "why",
            },
        )
    assert await _count(ResourceAssignment) == 0


# ---------------------------------------------------------------------------
# spaces.unassign and spaces.reassign
# ---------------------------------------------------------------------------


async def _assigned(world: _World) -> str:
    result = await spaces_assign(
        _operator(world.tenant_a),
        None,
        {
            "space_id": str(world.space_a),
            "kind": "target",
            "target_id": str(world.target_a),
            "reason": "first",
        },
    )
    return str(result["assignment"]["assignment_id"])


@pytest.mark.asyncio
async def test_unassign_end_dates_the_row_and_deletes_nothing(world: _World) -> None:
    assignment_id = await _assigned(world)
    operator = _operator(world.tenant_a)

    result = await spaces_unassign(
        operator, None, {"assignment_id": assignment_id, "reason": "gone"}
    )

    assert result["status"] == "ended"
    (row,) = await _rows(ResourceAssignment)
    assert row.valid_to is not None
    with pytest.raises(ValueError, match="No current assignment"):
        await spaces_unassign(operator, None, {"assignment_id": assignment_id, "reason": "gone"})
    assert await _count(ResourceAssignment) == 1


@pytest.mark.asyncio
async def test_unassign_other_tenants_assignment_is_not_found(world: _World) -> None:
    assignment_id = await _assigned(world)
    with pytest.raises(ValueError, match="No current assignment"):
        await spaces_unassign(
            _operator(world.tenant_b), None, {"assignment_id": assignment_id, "reason": "x"}
        )
    (row,) = await _rows(ResourceAssignment)
    assert row.valid_to is None


@pytest.mark.asyncio
async def test_reassign_ends_old_row_and_adds_new_one(world: _World) -> None:
    assignment_id = await _assigned(world)

    result = await spaces_reassign(
        _operator(world.tenant_a),
        None,
        {"assignment_id": assignment_id, "to_space_id": str(world.space_a2), "reason": "moved"},
    )

    assert result["status"] == "moved"
    rows = await _rows(ResourceAssignment)
    assert len(rows) == 2
    old = next(r for r in rows if str(r.id) == assignment_id)
    new = next(r for r in rows if str(r.id) != assignment_id)
    assert old.valid_to is not None
    assert (new.space_id, new.valid_to, new.valid_from) == (world.space_a2, None, old.valid_to)
    assert (new.changed_by, new.reason) == ("admin-1", "moved")


@pytest.mark.asyncio
async def test_reassign_to_same_space_or_other_tenant_changes_nothing(world: _World) -> None:
    assignment_id = await _assigned(world)
    operator = _operator(world.tenant_a)

    same = await spaces_reassign(
        operator,
        None,
        {"assignment_id": assignment_id, "to_space_id": str(world.space_a), "reason": "x"},
    )
    assert same["status"] == "unchanged"
    with pytest.raises(ValueError, match="No space"):
        await spaces_reassign(
            operator,
            None,
            {"assignment_id": assignment_id, "to_space_id": str(world.space_b), "reason": "x"},
        )
    rows = await _rows(ResourceAssignment)
    assert [r.valid_to for r in rows] == [None]


# ---------------------------------------------------------------------------
# spaces.member.disable
# ---------------------------------------------------------------------------


async def _member(world: _World, sub: str, role: str) -> None:
    async with get_sessionmaker()() as session, session.begin():
        await store.set_member(
            session,
            tenant_id=world.tenant_a,
            space_id=world.space_a,
            member_issuer=_ISSUER,
            member_sub=sub,
            role=role,
            state="active",
            changed_by="admin-1",
            reason="joined",
        )


def _disable_params(world: _World, sub: str) -> dict[str, Any]:
    return {
        "space_id": str(world.space_a),
        "member_issuer": _ISSUER,
        "member_sub": sub,
        "reason": "left the company",
    }


@pytest.mark.asyncio
async def test_member_disable_end_dates_and_marks_assignments_when_owner_leaves(
    world: _World,
) -> None:
    await _member(world, "owner-1", "owner")
    await _member(world, "helper-1", "member")
    assignment_id = await _assigned(world)

    result = await spaces_member_disable(
        _operator(world.tenant_a), None, _disable_params(world, "owner-1")
    )

    assert result["status"] == "disabled"
    assert result["owner_left"] is True
    assert result["assignments_marked_owner_left"] == [assignment_id]
    rows = [r for r in await _rows(SpaceMember) if r.member_sub == "owner-1"]
    assert len(rows) == 2  # nothing deleted
    ended = next(r for r in rows if r.valid_to is not None)
    current = next(r for r in rows if r.valid_to is None)
    assert (ended.state, current.state) == ("active", "disabled")
    assert current.valid_from == ended.valid_to
    assert (current.changed_by, current.reason) == ("admin-1", "left the company")
    assert (await _rows(ResourceAssignment))[0].valid_to is None  # assignment kept as is


@pytest.mark.asyncio
async def test_member_disable_of_plain_member_marks_nothing(world: _World) -> None:
    await _member(world, "owner-1", "owner")
    await _member(world, "helper-1", "member")
    await _assigned(world)

    result = await spaces_member_disable(
        _operator(world.tenant_a), None, _disable_params(world, "helper-1")
    )

    assert result["status"] == "disabled"
    assert (result["owner_left"], result["assignments_marked_owner_left"]) == (False, [])


@pytest.mark.asyncio
async def test_member_disable_twice_and_unknown_person(world: _World) -> None:
    await _member(world, "owner-1", "owner")
    operator = _operator(world.tenant_a)
    await spaces_member_disable(operator, None, _disable_params(world, "owner-1"))
    before = await _count(SpaceMember)

    again = await spaces_member_disable(operator, None, _disable_params(world, "owner-1"))
    assert again["status"] == "unchanged"
    with pytest.raises(ValueError, match="not a current member"):
        await spaces_member_disable(operator, None, _disable_params(world, "nobody"))
    assert await _count(SpaceMember) == before


@pytest.mark.asyncio
async def test_member_disable_other_tenants_space_is_not_found(world: _World) -> None:
    with pytest.raises(ValueError, match="No space"):
        await spaces_member_disable(
            _operator(world.tenant_b), None, _disable_params(world, "owner-1")
        )


@pytest.mark.asyncio
async def test_member_disable_makes_no_keycloak_call(
    world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    from meho_backplane.connectors.keycloak.connector import KeycloakConnector

    http = AsyncMock(side_effect=AssertionError("no network call is allowed"))
    monkeypatch.setattr(httpx.AsyncClient, "send", http)
    monkeypatch.setattr(httpx.AsyncClient, "request", http)
    connector_calls = AsyncMock(side_effect=AssertionError("Keycloak connector was called"))
    for name in ("probe", "fingerprint", "_request", "request"):
        if hasattr(KeycloakConnector, name):
            monkeypatch.setattr(KeycloakConnector, name, connector_calls)
    await _member(world, "owner-1", "owner")

    await spaces_member_disable(_operator(world.tenant_a), None, _disable_params(world, "owner-1"))

    http.assert_not_called()
    connector_calls.assert_not_called()


def test_spaces_package_does_not_import_keycloak() -> None:
    package = Path(spaces_registry.__file__).parent
    for source in package.glob("*.py"):
        imports = [
            line
            for line in source.read_text().splitlines()
            if line.startswith(("import ", "from ")) and "keycloak" in line.lower()
        ]
        assert imports == [], f"{source.name} imports Keycloak"


# ---------------------------------------------------------------------------
# Role and switch
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("role", [TenantRole.OPERATOR, TenantRole.READ_ONLY])
async def test_writes_need_tenant_admin(world: _World, role: TenantRole) -> None:
    operator = _operator(world.tenant_a, role)
    for handler, params in (
        (
            spaces_assign,
            {"space_id": str(world.space_a), "kind": "fact", "name": "n", "reason": "r"},
        ),
        (spaces_unassign, {"assignment_id": str(uuid.uuid4()), "reason": "r"}),
        (
            spaces_reassign,
            {
                "assignment_id": str(uuid.uuid4()),
                "to_space_id": str(world.space_a2),
                "reason": "r",
            },
        ),
        (spaces_member_disable, _disable_params(world, "owner-1")),
    ):
        with pytest.raises(ValueError, match="tenant_admin"):
            await handler(operator, None, params)
    assert await _count(ResourceAssignment) == 0


@pytest.mark.asyncio
async def test_every_operation_refuses_while_spaces_are_off(
    world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SPACES_ENABLED", "false")
    get_settings.cache_clear()
    operator = _operator(world.tenant_a)
    with pytest.raises(ValueError, match="Spaces are off"):
        await spaces_overview(operator, None, {})
    with pytest.raises(ValueError, match="Spaces are off"):
        await spaces_assign(
            operator,
            None,
            {"space_id": str(world.space_a), "kind": "fact", "name": "n", "reason": "r"},
        )


# ---------------------------------------------------------------------------
# spaces.overview
# ---------------------------------------------------------------------------

_NOW = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)


def _sensor(**over: Any) -> Sensor:
    fields: dict[str, Any] = {
        "id": uuid.uuid4(),
        "name": f"check-{uuid.uuid4().hex[:6]}",
        "connector_id": "demo-1.x",
        "op_id": "demo.list",
        "params": {},
        "assertion": _ASSERTION,
        "status": SensorStatus.ACTIVE.value,
        "cadence_kind": SensorCadenceKind.INTERVAL.value,
        "interval_seconds": 60,
        "severity": SensorSeverity.CRITICAL.value,
        "for_seconds": 0,
        "last_state": "ok",
        "created_by_sub": "admin-1",
    }
    fields.update(over)
    return Sensor(**fields)


def test_health_is_the_newest_state_while_fresh() -> None:
    old = _sensor(last_state="critical", last_evaluated_at=_NOW - timedelta(seconds=110))
    new = _sensor(last_state="ok", last_evaluated_at=_NOW - timedelta(seconds=30))

    result = health_state([old, new], _NOW)

    assert result["state"] == "ok"
    assert result["last_evaluated_at"] == (_NOW - timedelta(seconds=30)).isoformat()
    assert result["check_interval_seconds"] == 60.0


def test_health_older_than_twice_the_interval_is_unknown() -> None:
    at_limit = _sensor(last_evaluated_at=_NOW - timedelta(seconds=120))
    past_limit = _sensor(last_evaluated_at=_NOW - timedelta(seconds=121))

    assert health_state([at_limit], _NOW)["state"] == "ok"
    stale = health_state([past_limit], _NOW)
    assert stale["state"] == "unknown"
    assert stale["last_evaluated_at"] is not None


def test_health_without_a_check_or_a_time_is_unknown() -> None:
    assert health_state([], _NOW)["state"] == "unknown"
    no_time = _sensor(last_state="critical", last_evaluated_at=None)
    assert health_state([no_time], _NOW) == {
        "state": "unknown",
        "last_evaluated_at": None,
        "check_interval_seconds": None,
    }


def test_health_with_a_cron_cadence_uses_the_gap_between_runs() -> None:
    hourly = _sensor(
        cadence_kind=SensorCadenceKind.CRON.value,
        interval_seconds=None,
        cron_expr="0 * * * *",
        timezone="UTC",
        last_evaluated_at=_NOW - timedelta(minutes=90),
    )
    late = _sensor(
        cadence_kind=SensorCadenceKind.CRON.value,
        interval_seconds=None,
        cron_expr="0 * * * *",
        timezone="UTC",
        last_evaluated_at=_NOW - timedelta(minutes=130),
    )

    assert health_state([hourly], _NOW)["check_interval_seconds"] == 3600.0
    assert health_state([hourly], _NOW)["state"] == "ok"
    assert health_state([late], _NOW)["state"] == "unknown"


@pytest.mark.asyncio
async def test_overview_shows_owner_members_version_and_health(world: _World) -> None:
    probed = "2026-10-30T08:00:00+00:00"
    async with get_sessionmaker()() as session, session.begin():
        row = await session.get(Target, world.target_a)
        assert row is not None
        row.fingerprint = {
            "vendor": "demo",
            "product": "demo-product",
            "version": "9.1",
            "reachable": True,
            "probed_at": probed,
            "probe_method": "test",
        }
        session.add(
            _sensor(
                tenant_id=world.tenant_a,
                target={"name": "demo-lab-a"},
                last_state="degraded",
                last_evaluated_at=datetime.now(UTC) - timedelta(seconds=10),
            )
        )
    await _member(world, "owner-1", "owner")
    await _assigned(world)

    result = await spaces_overview(_operator(world.tenant_a), None, {})

    assert result["count"] == 2
    lab = next(s for s in result["spaces"] if s["name"] == "lab-a")
    assert lab["owner"] == {"issuer": _ISSUER, "sub": "owner-1"}
    assert lab["members"] == [
        {"issuer": _ISSUER, "sub": "owner-1", "role": "owner", "state": "active"}
    ]
    (entry,) = lab["assignments"]
    assert (entry["product"], entry["version"], entry["detected_at"]) == (
        "demo-product",
        "9.1",
        probed,
    )
    assert entry["health"]["state"] == "degraded"
    assert entry["health"]["last_evaluated_at"] is not None


@pytest.mark.asyncio
async def test_overview_health_is_unknown_when_the_newest_run_is_old(world: _World) -> None:
    async with get_sessionmaker()() as session, session.begin():
        session.add(
            _sensor(
                tenant_id=world.tenant_a,
                target={"name": "demo-lab-a"},
                last_state="ok",
                last_evaluated_at=datetime.now(UTC) - timedelta(minutes=10),
            )
        )
    await _assigned(world)

    result = await spaces_overview(
        _operator(world.tenant_a), None, {"space_id": str(world.space_a)}
    )

    assert result["count"] == 1
    assert result["spaces"][0]["assignments"][0]["health"]["state"] == "unknown"


@pytest.mark.asyncio
async def test_overview_flags_assignments_of_a_space_whose_owner_left(world: _World) -> None:
    await _member(world, "owner-1", "owner")
    await _assigned(world)
    operator = _operator(world.tenant_a)
    await spaces_member_disable(operator, None, _disable_params(world, "owner-1"))

    result = await spaces_overview(operator, None, {"space_id": str(world.space_a)})

    (space,) = result["spaces"]
    assert space["owner_left"] is True
    assert space["assignments"][0]["owner_left"] is True
    assert space["members"][0]["state"] == "disabled"


@pytest.mark.asyncio
async def test_overview_without_detected_data_and_for_unknown_kinds(world: _World) -> None:
    await _add_target(world.tenant_a, uuid.uuid4(), "no-version", version=None)
    await spaces_assign(
        _operator(world.tenant_a),
        None,
        {"space_id": str(world.space_a), "kind": "fact", "name": "a fact", "reason": "r"},
    )

    result = await spaces_overview(
        _operator(world.tenant_a), None, {"space_id": str(world.space_a)}
    )

    (entry,) = result["spaces"][0]["assignments"]
    assert (entry["product"], entry["version"], entry["detected_at"], entry["health"]) == (
        None,
        None,
        None,
        None,
    )


@pytest.mark.asyncio
async def test_overview_never_shows_another_tenants_spaces(world: _World) -> None:
    result = await spaces_overview(_operator(world.tenant_b), None, {})
    assert [s["name"] for s in result["spaces"]] == ["lab-b"]
    other = await spaces_overview(_operator(world.tenant_b), None, {"space_id": str(world.space_a)})
    assert other == {"spaces": [], "count": 0}


@pytest.mark.asyncio
async def test_overview_writes_nothing_and_makes_no_vendor_call(
    world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _assigned(world)
    before = (await _count(ResourceAssignment), await _count(SpaceMember))
    http = AsyncMock(side_effect=AssertionError("no network call is allowed"))
    monkeypatch.setattr(httpx.AsyncClient, "send", http)

    await spaces_overview(_operator(world.tenant_a), None, {})

    assert (await _count(ResourceAssignment), await _count(SpaceMember)) == before
    http.assert_not_called()


# ---------------------------------------------------------------------------
# Registration and dispatch
# ---------------------------------------------------------------------------


@pytest.fixture
async def _registered() -> None:
    service = AsyncMock()
    service.encode_one.return_value = [0.1] * 384
    service.encode.return_value = [[0.1] * 384]
    service.dimension = 384
    await register_spaces_operations(embedding_service=service)


@pytest.mark.asyncio
@pytest.mark.usefixtures("_registered")
async def test_registration_sets_the_risk_levels() -> None:
    async with get_sessionmaker()() as session:
        rows = list(
            await session.scalars(
                select(EndpointDescriptor).where(EndpointDescriptor.op_id.like("spaces.%"))
            )
        )
    levels = {r.op_id: r.safety_level for r in rows}
    assert levels == {
        "spaces.assign": "caution",
        "spaces.unassign": "caution",
        "spaces.reassign": "caution",
        "spaces.member.disable": "caution",
        "spaces.overview": "safe",
    }


@pytest.mark.asyncio
@pytest.mark.usefixtures("_registered")
async def test_overview_dispatches_and_a_write_below_the_floor_is_denied(world: _World) -> None:
    result = await dispatch(
        operator=_operator(world.tenant_a, TenantRole.READ_ONLY, web=True),
        connector_id=SPACES_CONNECTOR_ID,
        op_id="spaces.overview",
        target=None,
        params={},
    )
    assert result.status == "ok", result.error
    assert isinstance(result.result, dict)
    assert result.result["count"] == 2

    denied = await dispatch(
        operator=_operator(world.tenant_a, TenantRole.OPERATOR, web=True),
        connector_id=SPACES_CONNECTOR_ID,
        op_id="spaces.assign",
        target=None,
        params={"space_id": str(world.space_a), "kind": "fact", "name": "n", "reason": "r"},
    )
    assert denied.status != "ok"
    assert await _count(ResourceAssignment) == 0
