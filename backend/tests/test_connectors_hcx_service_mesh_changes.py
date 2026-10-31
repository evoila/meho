# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for the HCX service mesh changes and ``hcx.task.get``, with a respx-mocked Manager.

* each change is typed, waits for a person's approval and has the safety
  level of the Task table; ``hcx.task.get`` is ``safe``;
* each change sends the right call and answers ``accepted`` with the
  interconnect task id and the status read; a vendor error reaches the
  answer;
* a task that already runs on the mesh is returned and nothing starts;
* ``hcx.task.get`` gives the state, progress and the vendor's error; after
  an expired login it logs in again; a Manager that does not answer gives
  ``not_reachable`` with the task id and the time;
* the delete preview reads the mesh and its appliances, refuses a mesh
  that does not exist, and a delete without a matching preview hash is
  refused;
* no password or token comes back in clear text.

Fixture values are made up; no real host, user or id appears here.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from typing import Any
from unittest.mock import patch

import httpx
import pytest
import respx
from sqlalchemy import func, select
from tenacity import wait_none

from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.connectors.hcx import (
    HCX_CONNECTOR_ID,
    HCX_IMPL_ID,
    HCX_PRODUCT,
    HCX_VERSION,
    HcxConnector,
    register_hcx_typed_operations,
)
from meho_backplane.connectors.hcx.service_mesh_ops import HCX_SERVICE_MESH_OPS
from meho_backplane.connectors.registry import clear_registry, register_connector_v2
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import ApprovalRequest, EndpointDescriptor
from meho_backplane.db.models import Target as TargetORM
from meho_backplane.operations import dispatch, reset_dispatcher_caches, set_default_reducer
from meho_backplane.operations._handler_resolve import (
    get_or_create_connector_instance,
    reset_handler_cache,
)
from meho_backplane.operations._preview import PreviewContext, build_proposed_effect
from meho_backplane.operations.approval_queue import (
    approve_request,
    resume_dispatch_after_approval,
)
from meho_backplane.operations.meta_tools import call_operation, preview_operation
from meho_backplane.operations.reducer import PassThroughReducer
from meho_backplane.settings import get_settings
from meho_backplane.targets.resolver import resolve_target

_HOST = "hcx-mesh-changes.test.invalid"
_BASE_URL = f"https://{_HOST}"
_TARGET_NAME = "hcx-mesh-changes"
_SESSIONS = "/hybridity/api/sessions"
_PASSWORD = "mesh-password-value"
_TOKEN = "mesh-hm-token-1"
_MESH_ID = "mesh-0001"
_TASK_ID = "task-0001"
_MESH_PATH = "/hybridity/api/interconnect/serviceMesh"
_MESH_ITEM = f"{_MESH_PATH}/{_MESH_ID}"
_TASKS_QUERY = "/hybridity/api/interconnect/tasks/query"
_TASK_PATH = f"/hybridity/api/interconnect/tasks/{_TASK_ID}"
_APPLIANCES = "/hybridity/api/interconnect/appliances"
_TENANT_ID = uuid.UUID("00000000-0000-0000-0000-000000004548")

_TABLE = {
    "hcx.service_mesh.create": "dangerous",
    "hcx.service_mesh.update": "dangerous",
    "hcx.service_mesh.resync": "dangerous",
    "hcx.service_mesh.delete": "destructive",
}

_COMPUTE_PROFILES = [
    {"endpoint_id": "site-local", "compute_profile_id": "cp-local"},
    {"endpoint_id": "site-remote", "compute_profile_id": "cp-remote"},
]


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _reset_module_state() -> Iterator[None]:
    reset_dispatcher_caches()
    reset_handler_cache()
    clear_registry()
    register_connector_v2(
        product=HCX_PRODUCT, version=HCX_VERSION, impl_id=HCX_IMPL_ID, cls=HcxConnector
    )
    yield
    reset_dispatcher_caches()
    reset_handler_cache()
    clear_registry()


def _operator(sub: str = "hcx-mesh-requester") -> Operator:
    return Operator(
        sub=sub,
        name="HCX Mesh Test Operator",
        email=None,
        raw_jwt="<hcx-mesh-raw-jwt>",
        tenant_id=_TENANT_ID,
        tenant_role=TenantRole.OPERATOR,
        principal_kind=PrincipalKind.USER,
    )


async def _session_loader(_target: object, _operator: Operator) -> dict[str, str]:
    return {"username": "hcx-mesh-svc", "password": _PASSWORD}


@pytest.fixture
async def hcx() -> AsyncIterator[HcxConnector]:
    set_default_reducer(PassThroughReducer())
    await register_hcx_typed_operations()
    async with get_sessionmaker()() as session:
        session.add(
            TargetORM(
                tenant_id=_TENANT_ID,
                name=_TARGET_NAME,
                aliases=[],
                product=HCX_PRODUCT,
                host=_HOST,
                port=443,
                fqdn=None,
                secret_ref="hcx/mesh-changes",
                auth_model="shared_service_account",
                vpn_required=False,
                extras={},
                fingerprint={"version": "4.11.0"},
                notes="seeded by test_connectors_hcx_service_mesh_changes",
            )
        )
        await session.commit()
    instance = get_or_create_connector_instance(HcxConnector)
    assert isinstance(instance, HcxConnector)
    instance._session_loader = _session_loader
    yield instance


async def _target() -> Any:
    async with get_sessionmaker()() as session:
        return await resolve_target(session, _TENANT_ID, _TARGET_NAME)


async def _run(op_id: str, params: dict[str, Any]) -> Any:
    """Run the op as an already-approved call (the handler path)."""
    return await dispatch(
        operator=_operator(),
        connector_id=HCX_CONNECTOR_ID,
        op_id=op_id,
        target=await _target(),
        params=params,
        _approved=True,
    )


def _args(op_id: str, params: dict[str, Any]) -> dict[str, Any]:
    return {
        "connector_id": HCX_CONNECTOR_ID,
        "op_id": op_id,
        "target": _TARGET_NAME,
        "params": params,
    }


def _login(mock: respx.MockRouter) -> respx.Route:
    return mock.post(_SESSIONS).respond(200, headers={"x-hm-authorization": _TOKEN})


def _no_running_task(mock: respx.MockRouter) -> respx.Route:
    return mock.post(_TASKS_QUERY).respond(200, json={"items": [], "paging": {"totalSize": 0}})


def _accepted_body(mesh_id: str = _MESH_ID) -> dict[str, Any]:
    return {
        "data": {"interconnectTaskId": _TASK_ID, "serviceMeshId": mesh_id},
        "warnings": [],
        "errors": [],
    }


def _body(route: respx.Route, index: int = 0) -> Any:
    return json.loads(route.calls[index].request.content or b"null")


def _mesh() -> dict[str, Any]:
    return {
        "serviceMeshId": _MESH_ID,
        "name": "<mesh-name>",
        "state": "ACTIVE",
        "services": [{"name": "INTERCONNECT"}, {"name": "VMOTION"}],
        "computeProfiles": [
            {"endpointId": "site-local", "computeProfileId": "cp-local"},
            {"endpointId": "site-remote", "computeProfileId": "cp-remote"},
        ],
        "trafficEnggCfg": {"isAppPathResiliencyEnabled": False},
        "lastTaskDetails": {"interconnectTaskId": "task-old", "status": "SUCCESS"},
    }


async def _approval_rows() -> int:
    async with get_sessionmaker()() as session:
        count = await session.execute(select(func.count()).select_from(ApprovalRequest))
        return int(count.scalar_one())


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


async def test_each_change_waits_for_approval_with_the_table_risk(hcx: HcxConnector) -> None:
    async with get_sessionmaker()() as session:
        rows = {
            row.op_id: row
            for row in (
                await session.execute(
                    select(EndpointDescriptor).where(EndpointDescriptor.impl_id == HCX_IMPL_ID)
                )
            )
            .scalars()
            .all()
        }
    ops = {op.op_id: op for op in HCX_SERVICE_MESH_OPS}
    for op_id, safety in _TABLE.items():
        row = rows[op_id]
        assert row.source_kind == "typed"
        assert row.safety_level == safety, op_id
        assert row.requires_approval is True, op_id
        assert ops[op_id].group_key == "hcx-service-mesh"
        assert "preconditions" in row.llm_instructions
    task = rows["hcx.task.get"]
    assert task.safety_level == "safe"
    assert task.requires_approval is False
    assert ops["hcx.task.get"].group_key == "hcx-jobs-alerts"


# ---------------------------------------------------------------------------
# Approval: a change only runs after a second person approves
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("op_id", "params"),
    [
        (
            "hcx.service_mesh.create",
            {"name": "<mesh-name>", "compute_profiles": _COMPUTE_PROFILES, "services": ["VMOTION"]},
        ),
        ("hcx.service_mesh.update", {"service_mesh_id": _MESH_ID, "services": ["INTERCONNECT"]}),
        ("hcx.service_mesh.resync", {"service_mesh_id": _MESH_ID}),
    ],
)
async def test_a_change_waits_for_approval_and_sends_nothing(
    hcx: HcxConnector, op_id: str, params: dict[str, Any]
) -> None:
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        _login(mock)
        vendor = mock.route(path__startswith="/hybridity/api/interconnect").respond(202, json={})
        call = await call_operation(_operator(), _args(op_id, params))
    assert call["status"] == "awaiting_approval", call
    assert not vendor.called
    assert await _approval_rows() == 1


# ---------------------------------------------------------------------------
# create
# ---------------------------------------------------------------------------


async def test_create_posts_the_mesh_and_answers_accepted(hcx: HcxConnector) -> None:
    params = {
        "name": "<mesh-name>",
        "compute_profiles": [
            {**_COMPUTE_PROFILES[0], "uplink_networks": [{"id": "np-1", "name": "<np>"}]},
            _COMPUTE_PROFILES[1],
        ],
        "services": ["INTERCONNECT", "NETWORK_EXTENSION"],
        "switch_pairs": [
            {"switches": [{"id": "sw-l", "cmp_id": "vc-l"}, {"id": "sw-r"}]},
        ],
        "uplink_max_bandwidth": 5000,
        "tcp_flow_conditioning": True,
    }
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        _login(mock)
        query = _no_running_task(mock)
        create = mock.post(_MESH_PATH).respond(202, json=_accepted_body())
        result = await _run("hcx.service_mesh.create", params)

    assert result.status == "ok", result.error
    assert _body(query) == {"filter": {"status": ["QUEUED", "RUNNING"]}}
    assert create.calls[0].request.headers["x-hm-authorization"] == _TOKEN
    assert _body(create) == {
        "name": "<mesh-name>",
        "services": [{"name": "INTERCONNECT"}, {"name": "NETWORK_EXTENSION"}],
        "computeProfiles": [
            {
                "endpointId": "site-local",
                "computeProfileId": "cp-local",
                "networks": [{"id": "np-1", "name": "<np>"}],
                "overriddenUplink": True,
            },
            {"endpointId": "site-remote", "computeProfileId": "cp-remote"},
        ],
        "switchPairCount": [
            {"switches": [{"id": "sw-l", "cmpId": "vc-l"}, {"id": "sw-r"}], "l2cApplianceCount": 1}
        ],
        "wanoptConfig": {"uplinkMaxBandwidth": 5000},
        "trafficEnggCfg": {"isTcpFlowConditioningEnabled": True},
    }
    assert result.result == {
        "status": "accepted",
        "service_mesh_id": _MESH_ID,
        "interconnect_task_id": _TASK_ID,
        "task_type": None,
        "task_status": None,
        "status_read": "hcx.task.get",
        "warnings": [],
        "errors": [],
    }


async def test_create_returns_a_running_task_of_the_same_mesh_and_starts_nothing(
    hcx: HcxConnector,
) -> None:
    running = {
        "interconnectTaskId": "task-running",
        "type": "<task-type>",
        "status": "RUNNING",
        "entityDetails": {"id": "mesh-new", "name": "<mesh-name>", "type": "<entity-type>"},
    }
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        _login(mock)
        mock.post(_TASKS_QUERY).respond(200, json={"items": [running]})
        create = mock.post(_MESH_PATH).respond(202, json=_accepted_body())
        result = await _run(
            "hcx.service_mesh.create",
            {"name": "<mesh-name>", "compute_profiles": _COMPUTE_PROFILES, "services": ["RAV"]},
        )

    assert result.status == "ok", result.error
    assert not create.called
    assert result.result["status"] == "already_running"
    assert result.result["interconnect_task_id"] == "task-running"
    assert result.result["service_mesh_id"] == "mesh-new"


async def test_create_vendor_error_reaches_the_answer(hcx: HcxConnector) -> None:
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        _login(mock)
        _no_running_task(mock)
        mock.post(_MESH_PATH).respond(
            422,
            json={"errors": [{"code": "<code>", "message": "Compute profile not found"}]},
        )
        result = await _run(
            "hcx.service_mesh.create",
            {"name": "<mesh-name>", "compute_profiles": _COMPUTE_PROFILES, "services": ["RAV"]},
        )

    assert result.status == "error"
    assert result.extras["http_status"] == 422
    assert "Compute profile not found" in result.extras["upstream_message"]


async def test_create_refuses_a_service_hcx_9_removed(hcx: HcxConnector) -> None:
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        vendor = mock.route().respond(202, json=_accepted_body())
        result = await _run(
            "hcx.service_mesh.create",
            {
                "name": "<mesh-name>",
                "compute_profiles": _COMPUTE_PROFILES,
                "services": ["WANOPT"],
            },
        )
    assert result.status == "error"
    assert not vendor.called


# ---------------------------------------------------------------------------
# update
# ---------------------------------------------------------------------------


async def test_update_reads_the_mesh_merges_the_change_and_puts_it_back(
    hcx: HcxConnector,
) -> None:
    params = {
        "service_mesh_id": _MESH_ID,
        "services": ["INTERCONNECT", "VMOTION", "NETWORK_EXTENSION"],
        "uplinks": [{"compute_profile_id": "cp-remote", "networks": [{"id": "np-9"}]}],
        "app_path_resiliency": True,
    }
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        _login(mock)
        query = _no_running_task(mock)
        mock.get(_MESH_ITEM).respond(200, json=_mesh())
        put = mock.put(_MESH_ITEM).respond(202, json=_accepted_body())
        result = await _run("hcx.service_mesh.update", params)

    assert result.status == "ok", result.error
    assert _body(query)["filter"]["entityIds"] == [_MESH_ID]
    sent = _body(put)
    assert "state" not in sent and "lastTaskDetails" not in sent
    assert sent["serviceMeshId"] == _MESH_ID
    assert sent["name"] == "<mesh-name>"
    assert [s["name"] for s in sent["services"]] == params["services"]
    assert sent["computeProfiles"][0] == {
        "endpointId": "site-local",
        "computeProfileId": "cp-local",
    }
    assert sent["computeProfiles"][1]["networks"] == [{"id": "np-9", "name": None}]
    assert sent["computeProfiles"][1]["overriddenUplink"] is True
    assert sent["trafficEnggCfg"] == {"isAppPathResiliencyEnabled": True}
    assert result.result["status"] == "accepted"
    assert result.result["interconnect_task_id"] == _TASK_ID


async def test_update_refuses_an_uplink_of_a_profile_the_mesh_does_not_use(
    hcx: HcxConnector,
) -> None:
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        _login(mock)
        _no_running_task(mock)
        mock.get(_MESH_ITEM).respond(200, json=_mesh())
        put = mock.put(_MESH_ITEM).respond(202, json=_accepted_body())
        result = await _run(
            "hcx.service_mesh.update",
            {
                "service_mesh_id": _MESH_ID,
                "uplinks": [{"compute_profile_id": "cp-other", "networks": [{"id": "np-1"}]}],
            },
        )
    assert result.status == "error"
    assert not put.called


async def test_update_needs_at_least_one_change(hcx: HcxConnector) -> None:
    result = await _run("hcx.service_mesh.update", {"service_mesh_id": _MESH_ID})
    assert result.status == "error"


async def test_update_vendor_error_reaches_the_answer(hcx: HcxConnector) -> None:
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        _login(mock)
        _no_running_task(mock)
        mock.get(_MESH_ITEM).respond(200, json=_mesh())
        mock.put(_MESH_ITEM).respond(
            409, json={"errors": [{"code": "<code>", "message": "Service mesh is busy"}]}
        )
        result = await _run(
            "hcx.service_mesh.update", {"service_mesh_id": _MESH_ID, "uplink_max_bandwidth": 100}
        )
    assert result.status == "error"
    assert "Service mesh is busy" in result.extras["upstream_message"]


# ---------------------------------------------------------------------------
# resync
# ---------------------------------------------------------------------------


async def test_resync_posts_to_the_mesh_and_answers_accepted(hcx: HcxConnector) -> None:
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        _login(mock)
        _no_running_task(mock)
        resync = mock.post(f"{_MESH_ITEM}/resync").respond(202, json=_accepted_body())
        result = await _run("hcx.service_mesh.resync", {"service_mesh_id": _MESH_ID})

    assert result.status == "ok", result.error
    assert resync.call_count == 1
    assert result.result["status"] == "accepted"
    assert result.result["interconnect_task_id"] == _TASK_ID
    assert result.result["status_read"] == "hcx.task.get"


async def test_resync_returns_the_running_task_of_the_mesh_and_starts_nothing(
    hcx: HcxConnector,
) -> None:
    running = {
        "interconnectTaskId": "task-running",
        "status": "QUEUED",
        "entityDetails": {"id": _MESH_ID},
    }
    other = {"interconnectTaskId": "task-other", "status": "RUNNING", "entityDetails": {"id": "x"}}
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        _login(mock)
        mock.post(_TASKS_QUERY).respond(200, json={"items": [other, running]})
        resync = mock.post(f"{_MESH_ITEM}/resync").respond(202, json=_accepted_body())
        result = await _run("hcx.service_mesh.resync", {"service_mesh_id": _MESH_ID})

    assert result.status == "ok", result.error
    assert not resync.called
    assert result.result["status"] == "already_running"
    assert result.result["interconnect_task_id"] == "task-running"
    assert result.result["task_status"] == "QUEUED"


async def test_resync_vendor_error_reaches_the_answer(hcx: HcxConnector) -> None:
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        _login(mock)
        _no_running_task(mock)
        mock.post(f"{_MESH_ITEM}/resync").respond(
            422, json={"errors": [{"code": "<code>", "message": "Mesh is in error state"}]}
        )
        result = await _run("hcx.service_mesh.resync", {"service_mesh_id": _MESH_ID})
    assert result.status == "error"
    assert "Mesh is in error state" in result.extras["upstream_message"]


async def test_an_accepted_answer_without_a_task_id_is_an_error(hcx: HcxConnector) -> None:
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        _login(mock)
        _no_running_task(mock)
        mock.post(f"{_MESH_ITEM}/resync").respond(
            202, json={"data": {}, "errors": [{"message": "No change to apply"}]}
        )
        result = await _run("hcx.service_mesh.resync", {"service_mesh_id": _MESH_ID})
    assert result.status == "error"
    assert "No change to apply" in json.dumps(result.model_dump(mode="json"))


# ---------------------------------------------------------------------------
# delete
# ---------------------------------------------------------------------------


async def test_delete_sends_force_and_answers_accepted(hcx: HcxConnector) -> None:
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        _login(mock)
        _no_running_task(mock)
        delete = mock.delete(_MESH_ITEM).respond(202, json=_accepted_body())
        result = await _run("hcx.service_mesh.delete", {"service_mesh_id": _MESH_ID, "force": True})

    assert result.status == "ok", result.error
    assert delete.calls[0].request.url.params["force"] == "true"
    assert result.result["status"] == "accepted"
    assert result.result["interconnect_task_id"] == _TASK_ID


async def test_delete_vendor_error_reaches_the_answer(hcx: HcxConnector) -> None:
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        _login(mock)
        _no_running_task(mock)
        delete = mock.delete(_MESH_ITEM).respond(
            422, json={"errors": [{"code": "<code>", "message": "Networks are still extended"}]}
        )
        result = await _run("hcx.service_mesh.delete", {"service_mesh_id": _MESH_ID})
    assert delete.calls[0].request.url.params["force"] == "false"
    assert result.status == "error"
    assert "Networks are still extended" in result.extras["upstream_message"]


@dataclass
class _FakeDescriptor:
    op_id: str


async def _card(params: dict[str, Any], connector: HcxConnector | None) -> dict[str, Any] | None:
    return await build_proposed_effect(
        PreviewContext(
            descriptor=_FakeDescriptor(op_id="hcx.service_mesh.delete"),  # type: ignore[arg-type]
            connector_instance=connector,
            operator=_operator(),
            target=await _target(),
            params=params,
            connector_id=HCX_CONNECTOR_ID,
        )
    )


def _mount_mesh_and_appliances(mock: respx.MockRouter) -> None:
    mock.get(_MESH_ITEM).respond(200, json=_mesh())
    mock.get(_APPLIANCES).respond(
        200,
        json={
            "items": [
                {
                    "applianceId": "ix-1",
                    "applianceName": "<ix>",
                    "applianceType": "HCX-WAN-IX",
                    "serviceMeshId": _MESH_ID,
                },
                {
                    "applianceId": "ne-1",
                    "applianceName": "<ne>",
                    "applianceType": "HCX-NET-EXT",
                    "serviceMeshId": _MESH_ID,
                },
                {"applianceId": "other-1", "serviceMeshId": "mesh-other"},
            ]
        },
    )


async def test_delete_preview_names_the_mesh_its_appliances_and_what_cannot_be_undone(
    hcx: HcxConnector,
) -> None:
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        _login(mock)
        _mount_mesh_and_appliances(mock)
        effect = await _card({"service_mesh_id": _MESH_ID}, hcx)

    assert effect is not None
    radius = effect["preview"]["blast_radius"] if "preview" in effect else effect["blast_radius"]
    assert radius["object"] == {
        "kind": "service_mesh",
        "id": _MESH_ID,
        "name": "<mesh-name>",
        "services": ["INTERCONNECT", "VMOTION"],
        "force": False,
    }
    assert [child["id"] for child in radius["children"]] == ["ix-1", "ne-1"]
    assert "removed" in radius["irreversibility"]


async def test_delete_preview_refuses_a_mesh_that_does_not_exist(hcx: HcxConnector) -> None:
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        _login(mock)
        mock.get(_MESH_ITEM).respond(404, json={"errors": [{"message": "not found"}]})
        effect = await _card({"service_mesh_id": _MESH_ID}, hcx)

    assert effect is not None
    assert effect["preview_unavailable"] is True
    assert "does not exist" in effect["preview_error"]


async def test_delete_without_preview_hash_is_refused(hcx: HcxConnector) -> None:
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        _login(mock)
        delete = mock.delete(url__regex=r".*").respond(202, json=_accepted_body())
        call = await call_operation(
            _operator(), _args("hcx.service_mesh.delete", {"service_mesh_id": _MESH_ID})
        )
    assert call["status"] == "denied", call
    assert call["extras"]["error_code"] == "preview_binding_required"
    assert not delete.called
    assert await _approval_rows() == 0


async def test_delete_of_a_missing_mesh_is_refused_before_it_waits_for_approval(
    hcx: HcxConnector,
) -> None:
    args = _args("hcx.service_mesh.delete", {"service_mesh_id": _MESH_ID})
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        _login(mock)
        mock.get(_MESH_ITEM).respond(404)
        delete = mock.delete(url__regex=r".*").respond(202, json=_accepted_body())
        preview = await preview_operation(_operator(), args)
        assert preview["status"] == "ok", preview
        call = await call_operation(_operator(), {**args, "preview_hash": preview["preview_hash"]})
    assert call["status"] == "denied", call
    assert call["extras"]["error_code"] == "blast_radius_required"
    assert "does not exist" in call["extras"]["reason"]
    assert not delete.called
    assert await _approval_rows() == 0


async def test_delete_waits_for_a_second_person_then_runs(hcx: HcxConnector) -> None:
    requester = _operator("hcx-mesh-requester")
    approver = _operator("hcx-mesh-approver")
    args = _args("hcx.service_mesh.delete", {"service_mesh_id": _MESH_ID})
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        _login(mock)
        _mount_mesh_and_appliances(mock)
        _no_running_task(mock)
        delete = mock.delete(_MESH_ITEM).respond(202, json=_accepted_body())

        preview = await preview_operation(requester, args)
        assert preview["status"] == "ok", preview
        call = await call_operation(requester, {**args, "preview_hash": preview["preview_hash"]})
        assert call["status"] == "awaiting_approval", call
        assert not delete.called
        request_id = uuid.UUID(call["extras"]["approval_request_id"])

        async with get_sessionmaker()() as session:
            row = await session.get(ApprovalRequest, request_id)
            assert row is not None
            effect = dict(row.proposed_effect)
        assert effect["safety_level"] == "destructive"
        assert effect["blast_radius"]["object"]["id"] == _MESH_ID

        async with get_sessionmaker()() as session:
            await approve_request(session, request_id, operator=approver, params=None)
            await session.commit()
        async with get_sessionmaker()() as session:
            row = await session.get(ApprovalRequest, request_id)
            assert row is not None
            resumed = await resume_dispatch_after_approval(
                operator=approver, request=row, params=None
            )
    assert resumed.status == "ok", resumed
    assert resumed.result["status"] == "accepted"
    assert delete.call_count == 1


# ---------------------------------------------------------------------------
# hcx.task.get -- the status read
# ---------------------------------------------------------------------------


def _task(status: str, **extra: Any) -> dict[str, Any]:
    return {
        "interconnectTaskId": _TASK_ID,
        "type": "<task-type>",
        "status": status,
        "progress": 40,
        "message": "<status-text>",
        "startTime": 1,
        "endTime": None,
        "entityDetails": {"id": _MESH_ID, "type": "<entity-type>", "name": "<mesh-name>"},
        "request": {"computeProfile": {"name": "<large-object>"}},
        "steps": [{"title": "<step>", "status": status, "jobId": "job-1"}],
        **extra,
    }


async def test_task_get_returns_state_progress_and_steps(hcx: HcxConnector) -> None:
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        _login(mock)
        route = mock.get(_TASK_PATH).respond(200, json=_task("RUNNING"))
        result = await _run("hcx.task.get", {"task_id": _TASK_ID})

    assert result.status == "ok", result.error
    assert route.call_count == 1
    body = result.result
    assert body["status"] == "RUNNING"
    assert body["done"] is False
    assert body["progress"] == 40
    assert body["error"] is None
    assert body["entity_id"] == _MESH_ID
    assert body["steps"][0]["job_id"] == "job-1"
    assert "request" not in body
    assert body["checked_at"]


async def test_task_get_gives_the_vendor_error_of_a_failed_task(hcx: HcxConnector) -> None:
    failed = _task(
        "FAILED",
        message="Deployment failed",
        steps=[{"title": "<step>", "status": "FAILED", "errors": [{"message": "No IP left"}]}],
    )
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        _login(mock)
        mock.get(_TASK_PATH).respond(200, json=failed)
        result = await _run("hcx.task.get", {"task_id": _TASK_ID})

    assert result.status == "ok", result.error
    assert result.result["done"] is True
    assert result.result["error"] == "Deployment failed; No IP left"


async def test_task_get_logs_in_again_when_the_login_expired(hcx: HcxConnector) -> None:
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        login = mock.post(_SESSIONS)
        login.side_effect = [
            httpx.Response(200, headers={"x-hm-authorization": "mesh-hm-token-1"}),
            httpx.Response(200, headers={"x-hm-authorization": "mesh-hm-token-2"}),
        ]
        _no_running_task(mock)
        mock.post(f"{_MESH_ITEM}/resync").respond(202, json=_accepted_body())
        start = await _run("hcx.service_mesh.resync", {"service_mesh_id": _MESH_ID})
        task = mock.get(_TASK_PATH)
        task.side_effect = [httpx.Response(401), httpx.Response(200, json=_task("SUCCESS"))]
        result = await _run("hcx.task.get", {"task_id": start.result["interconnect_task_id"]})

    assert result.status == "ok", result.error
    assert login.call_count == 2
    assert task.calls[1].request.headers["x-hm-authorization"] == "mesh-hm-token-2"
    assert result.result["status"] == "SUCCESS"
    assert result.result["done"] is True


@pytest.mark.parametrize(
    "transport_error",
    [httpx.ConnectError("[Errno 111] Connection refused"), httpx.ReadTimeout("timed out")],
    ids=["refused", "timeout"],
)
async def test_task_get_says_not_reachable_when_the_manager_does_not_answer(
    hcx: HcxConnector, transport_error: httpx.TransportError
) -> None:
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        _login(mock)
        route = mock.get(_TASK_PATH).mock(side_effect=transport_error)
        with patch.object(hcx._request_json.retry, "wait", wait_none()):  # type: ignore[attr-defined]
            result = await _run("hcx.task.get", {"task_id": _TASK_ID})

    assert result.status == "ok", result.error
    assert route.called
    assert result.result["status"] == "not_reachable"
    assert result.result["interconnect_task_id"] == _TASK_ID
    assert result.result["checked_at"]
    assert result.result["done"] is False
    assert result.result["error"] is None


async def test_results_hold_no_password_or_token(hcx: HcxConnector) -> None:
    secret_task = _task("RUNNING", steps=[{"title": "<step>", "token": "made-up-step-token"}])
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        _login(mock)
        mock.get(_TASK_PATH).respond(200, json=secret_task)
        result = await _run("hcx.task.get", {"task_id": _TASK_ID})

    assert result.status == "ok", result.error
    rendered = json.dumps(result.result) + json.dumps(result.extras, default=str)
    for secret in (_PASSWORD, _TOKEN, "made-up-step-token"):
        assert secret not in rendered
