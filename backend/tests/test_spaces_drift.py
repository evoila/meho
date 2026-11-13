# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for ``spaces.drift`` and its nightly health-check template."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest
from sqlalchemy import func, select

from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.checks import evaluate_assertion
from meho_backplane.checks.assertions import AssertionSpec
from meho_backplane.checks.schemas import SensorCreate
from meho_backplane.connectors.spaces.common import SPACES_CONNECTOR_ID
from meho_backplane.connectors.spaces.drift import DRIFT_KINDS, spaces_drift
from meho_backplane.connectors.spaces.drift_template import drift_sensor_template
from meho_backplane.connectors.spaces.registry import register_spaces_operations
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import (
    GraphNode,
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
from meho_backplane.spaces.models import ResourceAssignment, Space

_ISSUER = "https://idp.example.test/realms/demo"
_NOW = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)


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
        self.tenant_a = uuid.uuid4()
        self.tenant_b = uuid.uuid4()
        self.space_a = uuid.uuid4()
        self.space_b = uuid.uuid4()


async def _add_target(
    tenant_id: uuid.UUID,
    name: str,
    *,
    fingerprint: dict[str, Any] | None = None,
    deleted: bool = False,
) -> uuid.UUID:
    target_id = uuid.uuid4()
    async with get_sessionmaker()() as session, session.begin():
        session.add(
            Target(
                id=target_id,
                tenant_id=tenant_id,
                name=name,
                product="demo-product",
                host="lab.example.test",
                fingerprint=fingerprint,
                deleted_at=_NOW if deleted else None,
            )
        )
    return target_id


async def _assign(
    tenant_id: uuid.UUID,
    space_id: uuid.UUID,
    *,
    kind: str = "target",
    target_id: uuid.UUID | None = None,
    object_id: str | None = None,
    name: str,
) -> uuid.UUID:
    async with get_sessionmaker()() as session, session.begin():
        row = await store.assign(
            session,
            tenant_id=tenant_id,
            space_id=space_id,
            kind=kind,
            target_id=target_id,
            object_id=object_id,
            name_at_assignment=name,
            access="own",
            changed_by="admin-1",
            reason="test",
        )
        return row.id


@pytest.fixture
async def world() -> AsyncIterator[_World]:
    w = _World()
    async with get_sessionmaker()() as session, session.begin():
        session.add(Tenant(id=w.tenant_a, slug="tenant-a", name="Tenant A"))
        session.add(Tenant(id=w.tenant_b, slug="tenant-b", name="Tenant B"))
    async with get_sessionmaker()() as session, session.begin():
        for tenant, space_id, name in (
            (w.tenant_a, w.space_a, "lab-a"),
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
    yield w


async def _drift(w: _World) -> dict[str, Any]:
    return await spaces_drift(_operator(w.tenant_a), None, {})


@pytest.mark.asyncio
async def test_clean_data_has_no_drift(world: _World) -> None:
    target_id = await _add_target(world.tenant_a, "demo-one", fingerprint={"reachable": True})
    await _assign(world.tenant_a, world.space_a, target_id=target_id, name="demo-one")

    result = await _drift(world)

    assert result["total_count"] == 0
    assert result["counts"] == dict.fromkeys(DRIFT_KINDS, 0)
    assert all(result[kind] == [] for kind in DRIFT_KINDS)


@pytest.mark.asyncio
async def test_empty_tenant_has_no_drift(world: _World) -> None:
    assert (await _drift(world))["total_count"] == 0


@pytest.mark.asyncio
async def test_a_target_in_no_space_is_unassigned(world: _World) -> None:
    owned = await _add_target(world.tenant_a, "demo-owned")
    lone = await _add_target(world.tenant_a, "demo-lone")
    await _add_target(world.tenant_a, "demo-gone", deleted=True)
    await _assign(world.tenant_a, world.space_a, target_id=owned, name="demo-owned")

    result = await _drift(world)

    assert [(e["target_id"], e["name"]) for e in result["unassigned_targets"]] == [
        (str(lone), "demo-lone")
    ]
    assert result["counts"]["unassigned_targets"] == 1


@pytest.mark.asyncio
async def test_an_ended_assignment_leaves_the_target_unassigned(world: _World) -> None:
    target_id = await _add_target(world.tenant_a, "demo-one")
    assignment_id = await _assign(
        world.tenant_a, world.space_a, target_id=target_id, name="demo-one"
    )
    async with get_sessionmaker()() as session, session.begin():
        await store.end_assignment(
            session,
            tenant_id=world.tenant_a,
            assignment_id=assignment_id,
        )

    result = await _drift(world)

    assert [e["name"] for e in result["unassigned_targets"]] == ["demo-one"]
    assert result["missing_targets"] == []


@pytest.mark.asyncio
async def test_an_assignment_of_a_deleted_or_unknown_target_is_missing(world: _World) -> None:
    deleted = await _add_target(world.tenant_a, "demo-deleted", deleted=True)
    unknown = uuid.uuid4()
    await _assign(world.tenant_a, world.space_a, target_id=deleted, name="demo-deleted")
    await _assign(world.tenant_a, world.space_a, target_id=unknown, name="demo-unknown")

    result = await _drift(world)

    assert sorted(e["name"] for e in result["missing_targets"]) == ["demo-deleted", "demo-unknown"]
    assert all(e["space_name"] == "lab-a" for e in result["missing_targets"])
    assert result["counts"]["missing_targets"] == 2


@pytest.mark.asyncio
async def test_only_a_probe_that_said_not_reachable_is_unreachable(world: _World) -> None:
    down = await _add_target(
        world.tenant_a,
        "demo-down",
        fingerprint={"reachable": False, "probed_at": "2026-11-01T01:00:00+00:00"},
    )
    never = await _add_target(world.tenant_a, "demo-never", fingerprint=None)
    up = await _add_target(world.tenant_a, "demo-up", fingerprint={"reachable": True})
    for target_id, name in ((down, "demo-down"), (never, "demo-never"), (up, "demo-up")):
        await _assign(world.tenant_a, world.space_a, target_id=target_id, name=name)

    result = await _drift(world)

    (entry,) = result["unreachable_targets"]
    assert (entry["name"], entry["source"], entry["seen_at"]) == (
        "demo-down",
        "probe",
        "2026-11-01T01:00:00+00:00",
    )


async def _check(tenant_id: uuid.UUID, target_name: str, *, age: timedelta, **over: Any) -> None:
    fields: dict[str, Any] = {
        "tenant_id": tenant_id,
        "name": f"check-{uuid.uuid4().hex[:6]}",
        "connector_id": "demo-1.x",
        "op_id": "demo.list",
        "target": {"name": target_name},
        "params": {},
        "assertion": {
            "select": {"path": "$.count"},
            "compare": {"type": "threshold", "op": "lt", "critical": 10},
        },
        "status": SensorStatus.ACTIVE.value,
        "cadence_kind": SensorCadenceKind.INTERVAL.value,
        "interval_seconds": 3600,
        "severity": SensorSeverity.CRITICAL.value,
        "for_seconds": 0,
        "last_state": "ok",
        "last_evaluated_at": datetime.now(UTC) - age,
        "created_by_sub": "admin-1",
    }
    fields.update(over)
    async with get_sessionmaker()() as session, session.begin():
        session.add(Sensor(**fields))


def _probe(reachable: bool, age: timedelta) -> dict[str, Any]:
    return {"reachable": reachable, "probed_at": (datetime.now(UTC) - age).isoformat()}


def _not_ok(status: str, error: str) -> dict[str, Any]:
    return {
        "last_state": "unknown",
        "last_evidence": {
            "reason": "dispatch_not_ok",
            "dispatch_status": status,
            "dispatch_error": error,
        },
    }


_CALL_FAILED = _not_ok("error", "connector_timeout: ConnectTimeout")


@pytest.mark.asyncio
async def test_a_newer_failed_check_makes_a_probed_target_unreachable(world: _World) -> None:
    target_id = await _add_target(
        world.tenant_a, "demo-one", fingerprint=_probe(True, timedelta(hours=5))
    )
    await _assign(world.tenant_a, world.space_a, target_id=target_id, name="demo-one")
    await _check(world.tenant_a, "demo-one", age=timedelta(minutes=10), **_CALL_FAILED)

    result = await _drift(world)

    (entry,) = result["unreachable_targets"]
    assert (entry["name"], entry["source"]) == ("demo-one", "health_check")
    assert entry["check_id"] is not None


@pytest.mark.asyncio
async def test_a_newer_passing_check_clears_an_old_failed_probe(world: _World) -> None:
    target_id = await _add_target(
        world.tenant_a, "demo-one", fingerprint=_probe(False, timedelta(hours=5))
    )
    await _assign(world.tenant_a, world.space_a, target_id=target_id, name="demo-one")
    await _check(world.tenant_a, "demo-one", age=timedelta(minutes=10), last_state="critical")

    assert (await _drift(world))["unreachable_targets"] == []


@pytest.mark.asyncio
async def test_a_stale_or_unrelated_check_result_is_not_reach_evidence(world: _World) -> None:
    stale = await _add_target(
        world.tenant_a, "demo-stale", fingerprint=_probe(True, timedelta(days=2))
    )
    bad_check = await _add_target(world.tenant_a, "demo-bad-check", fingerprint=None)
    for target_id, name in ((stale, "demo-stale"), (bad_check, "demo-bad-check")):
        await _assign(world.tenant_a, world.space_a, target_id=target_id, name=name)
    await _check(world.tenant_a, "demo-stale", age=timedelta(hours=3), **_CALL_FAILED)
    await _check(
        world.tenant_a,
        "demo-bad-check",
        age=timedelta(minutes=10),
        last_state="unknown",
        last_evidence={"reason": "op_descriptor_missing"},
    )

    assert (await _drift(world))["unreachable_targets"] == []


@pytest.mark.parametrize(
    "check",
    [
        _not_ok("error", "connector_error: ConnectError"),
        _not_ok("error", "connector_timeout: ReadTimeout"),
    ],
)
@pytest.mark.asyncio
async def test_a_connection_failure_makes_a_target_unreachable(
    world: _World, check: dict[str, Any]
) -> None:
    target_id = await _add_target(world.tenant_a, "demo-one", fingerprint=None)
    await _assign(world.tenant_a, world.space_a, target_id=target_id, name="demo-one")
    await _check(world.tenant_a, "demo-one", age=timedelta(minutes=10), **check)

    assert [e["name"] for e in (await _drift(world))["unreachable_targets"]] == ["demo-one"]


@pytest.mark.parametrize(
    "check",
    [
        _not_ok("denied", "denied: no grant covers this call"),
        _not_ok("error", "vault_credentials_read_error: the secret could not be read"),
        _not_ok("error", "no_connector: no connector for this product"),
        _not_ok("error", "connector_error: KeyError"),
        _not_ok("error", "connector_error: HTTPStatusError"),
        {"last_state": "unknown", "last_evidence": {"reason": "dispatch_error"}},
        {"last_state": "unknown", "last_evidence": {"reason": "evaluation_timeout"}},
        {
            "last_state": "unknown",
            "last_evidence": {"reason": "no_target", "dispatch_error": "no target"},
        },
    ],
)
@pytest.mark.asyncio
async def test_a_local_failure_does_not_make_a_target_unreachable(
    world: _World, check: dict[str, Any]
) -> None:
    target_id = await _add_target(
        world.tenant_a, "demo-one", fingerprint=_probe(True, timedelta(hours=5))
    )
    await _assign(world.tenant_a, world.space_a, target_id=target_id, name="demo-one")
    await _check(world.tenant_a, "demo-one", age=timedelta(minutes=10), **check)

    assert (await _drift(world))["unreachable_targets"] == []


@pytest.mark.asyncio
async def test_a_failure_waiting_for_confirmation_counts(world: _World) -> None:
    target_id = await _add_target(
        world.tenant_a, "demo-one", fingerprint=_probe(True, timedelta(hours=5))
    )
    await _assign(world.tenant_a, world.space_a, target_id=target_id, name="demo-one")
    await _check(
        world.tenant_a,
        "demo-one",
        age=timedelta(minutes=10),
        retry_times=2,
        pending_count=1,
        last_evidence=_CALL_FAILED["last_evidence"],
        last_state="ok",
        pending_state="unknown",
    )

    (entry,) = (await _drift(world))["unreachable_targets"]
    assert (entry["name"], entry["source"]) == ("demo-one", "health_check")


@pytest.mark.asyncio
async def test_a_recovery_waiting_for_confirmation_clears_a_failed_probe(world: _World) -> None:
    target_id = await _add_target(
        world.tenant_a, "demo-one", fingerprint=_probe(False, timedelta(hours=5))
    )
    await _assign(world.tenant_a, world.space_a, target_id=target_id, name="demo-one")
    await _check(
        world.tenant_a,
        "demo-one",
        age=timedelta(minutes=10),
        retry_times=2,
        pending_count=1,
        last_evidence={"value": 3},
        last_state="unknown",
        pending_state="ok",
    )

    assert (await _drift(world))["unreachable_targets"] == []


@pytest.mark.asyncio
async def test_an_unreachable_target_in_no_space_is_only_unassigned(world: _World) -> None:
    await _add_target(world.tenant_a, "demo-down", fingerprint={"reachable": False})

    result = await _drift(world)

    assert result["counts"]["unreachable_targets"] == 0
    assert result["counts"]["unassigned_targets"] == 1


async def _node(
    tenant_id: uuid.UUID,
    name: str,
    *,
    kind: str = "vm",
    live: bool = True,
    target_id: uuid.UUID | None = None,
    source_scope: str | None = None,
    properties: dict[str, Any] | None = None,
) -> None:
    async with get_sessionmaker()() as session, session.begin():
        session.add(
            GraphNode(
                tenant_id=tenant_id,
                kind=kind,
                name=name,
                target_id=target_id,
                source="auto",
                source_scope=source_scope,
                properties=properties or {},
                discovered_by="demo",
                first_seen=_NOW,
                last_seen=_NOW if live else None,
            )
        )


@pytest.mark.asyncio
async def test_a_machine_in_no_live_topology_node_is_missing(world: _World) -> None:
    await _node(world.tenant_a, "seen", properties={"uid": "vm-seen"})
    await _node(world.tenant_a, "dropped", live=False, properties={"uid": "vm-dropped"})
    await _node(world.tenant_b, "other", properties={"uid": "vm-other-tenant"})
    for object_id in ("vm-seen", "vm-dropped", "vm-other-tenant", "vm-never"):
        await _assign(
            world.tenant_a,
            world.space_a,
            kind="object",
            object_id=object_id,
            name=f"name of {object_id}",
        )

    result = await _drift(world)

    assert sorted(e["object_id"] for e in result["missing_objects"]) == [
        "vm-dropped",
        "vm-never",
        "vm-other-tenant",
    ]


@pytest.mark.asyncio
async def test_a_machine_matches_by_vendor_id_not_by_name(world: _World) -> None:
    host = await _add_target(world.tenant_a, "demo-host")
    await _node(world.tenant_a, "srv-alpha", target_id=host, properties={"server_number": 4711})
    await _assign(world.tenant_a, world.space_a, target_id=host, name="demo-host")
    for object_id in ("4711", "srv-beta"):
        await _assign(
            world.tenant_a,
            world.space_a,
            kind="object",
            target_id=host,
            object_id=object_id,
            name=f"name of {object_id}",
        )

    result = await _drift(world)

    assert [e["object_id"] for e in result["missing_objects"]] == ["srv-beta"]


@pytest.mark.asyncio
async def test_a_display_name_or_a_reference_is_not_a_machine_id(world: _World) -> None:
    host = await _add_target(world.tenant_a, "demo-host")
    await _node(
        world.tenant_a, "4711", kind="host", target_id=host, properties={"server_number": 9999}
    )
    await _node(
        world.tenant_a,
        "10.0.0.0/29",
        kind="network",
        target_id=host,
        properties={"server_number": 4711},
    )
    await _assign(world.tenant_a, world.space_a, target_id=host, name="demo-host")
    for object_id in ("4711", "9999"):
        await _assign(
            world.tenant_a,
            world.space_a,
            kind="object",
            target_id=host,
            object_id=object_id,
            name=f"name of {object_id}",
        )

    result = await _drift(world)

    assert [e["object_id"] for e in result["missing_objects"]] == ["4711"]


@pytest.mark.asyncio
async def test_a_machine_is_only_found_on_its_assigned_target(world: _World) -> None:
    first = await _add_target(world.tenant_a, "demo-first")
    second = await _add_target(world.tenant_a, "demo-second")
    third = await _add_target(world.tenant_a, "demo-third")
    await _node(world.tenant_a, "vm-on-first", target_id=first, properties={"uid": "obj-1"})
    await _node(
        world.tenant_a,
        "vm-on-second",
        source_scope=f"target:{second}",
        properties={"uid": "obj-1"},
    )
    for target_id in (first, second, third):
        await _assign(
            world.tenant_a,
            world.space_a,
            kind="object",
            target_id=target_id,
            object_id="obj-1",
            name="shared id",
        )

    result = await _drift(world)

    assert [e["target_id"] for e in result["missing_objects"]] == [str(third)]


@pytest.mark.asyncio
async def test_a_disabled_space_and_a_renamed_target_are_state_mismatches(world: _World) -> None:
    renamed = await _add_target(world.tenant_a, "demo-new-name")
    await _assign(world.tenant_a, world.space_a, target_id=renamed, name="demo-old-name")
    async with get_sessionmaker()() as session, session.begin():
        space = await session.get(Space, world.space_a)
        assert space is not None
        space.state = "disabled"

    result = await _drift(world)

    reasons = sorted((e["reason"], e["name"]) for e in result["state_mismatch"])
    assert reasons == [("space_disabled", "demo-old-name"), ("target_renamed", "demo-old-name")]
    renamed_entry = next(e for e in result["state_mismatch"] if e["reason"] == "target_renamed")
    assert renamed_entry["current_name"] == "demo-new-name"
    assert result["total_count"] == 2


@pytest.mark.asyncio
async def test_another_tenant_is_never_read(world: _World) -> None:
    other = await _add_target(world.tenant_b, "demo-b", fingerprint={"reachable": False})
    await _assign(world.tenant_b, world.space_b, target_id=other, name="demo-other-name")
    await _assign(world.tenant_b, world.space_b, target_id=uuid.uuid4(), name="demo-b-missing")

    assert (await _drift(world))["total_count"] == 0
    assert (await spaces_drift(_operator(world.tenant_b), None, {}))["total_count"] == 3


@pytest.mark.asyncio
async def test_drift_writes_nothing_and_makes_no_vendor_call(
    world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _assign(world.tenant_a, world.space_a, target_id=uuid.uuid4(), name="demo-missing")
    http = AsyncMock(side_effect=AssertionError("no network call is allowed"))
    monkeypatch.setattr(httpx.AsyncClient, "send", http)

    async def count() -> int:
        async with get_sessionmaker()() as session:
            return int(
                await session.scalar(select(func.count()).select_from(ResourceAssignment)) or 0
            )

    before = await count()
    await _drift(world)

    assert await count() == before
    http.assert_not_called()


@pytest.mark.asyncio
async def test_drift_is_refused_while_spaces_are_off(
    world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SPACES_ENABLED", "false")
    get_settings.cache_clear()
    with pytest.raises(ValueError, match="Spaces are off"):
        await _drift(world)


@pytest.mark.asyncio
async def test_drift_dispatches_as_a_safe_read(world: _World) -> None:
    service = AsyncMock()
    service.encode_one.return_value = [0.1] * 384
    service.encode.return_value = [[0.1] * 384]
    service.dimension = 384
    await register_spaces_operations(embedding_service=service)
    await _add_target(world.tenant_a, "demo-lone")

    result = await dispatch(
        operator=_operator(world.tenant_a, TenantRole.READ_ONLY),
        connector_id=SPACES_CONNECTOR_ID,
        op_id="spaces.drift",
        target=None,
        params={},
    )

    assert result.status == "ok", result.error
    assert isinstance(result.result, dict)
    assert result.result["total_count"] == 1


def test_template_is_a_working_nightly_check() -> None:
    body = SensorCreate.model_validate(drift_sensor_template())
    assert (body.connector_id, body.op_id, body.target) == (
        SPACES_CONNECTOR_ID,
        "spaces.drift",
        None,
    )
    assert (body.cadence_kind, body.cron_expr, body.severity) == ("cron", "0 2 * * *", "degraded")
    renamed = SensorCreate.model_validate(drift_sensor_template(name="site-drift"))
    assert renamed.name == "site-drift"


@pytest.mark.parametrize(
    ("total", "expected"),
    [(0, "ok"), (1, "degraded"), (40, "degraded")],
)
def test_template_goes_degraded_when_any_count_is_above_zero(total: int, expected: str) -> None:
    spec = AssertionSpec.model_validate(drift_sensor_template()["assertion"])
    outcome = evaluate_assertion(spec, {"total_count": total}, now=_NOW)
    assert outcome.state == expected


def test_template_bounds_are_settable() -> None:
    body = drift_sensor_template(degraded_above=2, critical_above=10)
    assert SensorCreate.model_validate(body).severity == "critical"
    spec = AssertionSpec.model_validate(body["assertion"])
    states = [evaluate_assertion(spec, {"total_count": n}, now=_NOW).state for n in (2, 3, 10, 11)]
    assert states == ["ok", "degraded", "degraded", "critical"]
