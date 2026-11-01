# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for the HCX Network Extension HA operations, with a respx-mocked HCX Manager.

* ``hcx.network_extension.ha.list`` returns flat rows; a long list goes
  behind a result handle;
* ``hcx.network_extension.ha.set`` waits for approval and is ``dangerous``
  with ``requires_approval``; the read is ``safe``;
* build and dissolve start a vendor task: the answer says ``accepted`` with
  the task id and the status read; a grouped appliance, a running task or a
  missing group starts nothing;
* HCX refuses an appliance that extends a network, or too few free
  appliances, and the error reaches the answer; an appliance older than
  HCX 4.3 is refused before any change;
* the approval request of a dissolve lists the extensions the group
  serves; ``preview_operation`` shows no plan, because it never contacts
  the Manager; a missing group still waits for approval and then answers
  ``not_found``;
* the status read ``hcx.task.get`` follows a started task: state, progress
  and the vendor error; after an expired login it logs in again; a Manager
  that does not answer gives ``not_reachable``; a failed job shows its
  error in ``hcx.job.get``.

Fixture values are made up; no real host, user or id appears here.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from typing import Any
from unittest.mock import AsyncMock, patch
from uuid import UUID

import httpx
import pytest
import respx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
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
from meho_backplane.connectors.hcx.ha_group_ops import (
    GROUP_NETWORK_EXTENSION_HA,
    HCX_HA_GROUP_OPS,
)
from meho_backplane.connectors.hcx.typed_ops import HCX_TYPED_WHEN_TO_USE_BY_GROUP
from meho_backplane.connectors.registry import clear_registry, register_connector_v2
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import ApprovalRequest, EndpointDescriptor
from meho_backplane.db.models import Target as TargetORM
from meho_backplane.operations import dispatch, reset_dispatcher_caches, set_default_reducer
from meho_backplane.operations._composite_preview import PLAN_NOT_SHOWN_NOTE
from meho_backplane.operations._handler_resolve import (
    get_or_create_connector_instance,
    reset_handler_cache,
)
from meho_backplane.operations._preview import PreviewContext, build_proposed_effect
from meho_backplane.operations._validate import validate_params
from meho_backplane.operations.approval_queue import (
    approve_request,
    resume_dispatch_after_approval,
)
from meho_backplane.operations.jsonflux_reducer import JsonFluxReducer
from meho_backplane.operations.meta_tools import call_operation, preview_operation
from meho_backplane.settings import get_settings

_HOST = "hcx-ha.test.invalid"
_BASE_URL = f"https://{_HOST}"
_SESSIONS = "/hybridity/api/sessions"
_GROUPS = "/hybridity/api/interconnect/appliances/ha/groups"
_APPLIANCES = "/hybridity/api/interconnect/appliances"
_TASKS = "/hybridity/api/interconnect/tasks/query"
_EXTENSIONS = "/hybridity/api/l2Extensions"
_TOKEN = "ha-hm-token-1"
_PASSWORD = "ha-password-value"
_GROUP_ID = "ha-group-0001"
_APPLIANCE_ID = "ne-0001"
_TASK_ID = "task-0001"
_TASK_PATH = f"/hybridity/api/interconnect/tasks/{_TASK_ID}"
_TARGET_NAME = "hcx-ha"
_TENANT_ID = UUID("00000000-0000-0000-0000-0000000000e5")
_SET = "hcx.network_extension.ha.set"
_LIST = "hcx.network_extension.ha.list"
_BUILD = {"action": "build", "appliance_ids": [_APPLIANCE_ID]}
_DISSOLVE = {"action": "dissolve", "group_id": _GROUP_ID}


def _appliance(appliance_id: str = _APPLIANCE_ID, **extra: Any) -> dict[str, Any]:
    return {
        "applianceId": appliance_id,
        "applianceName": f"<{appliance_id}>",
        "applianceType": "HCX-NET-EXT",
        "applianceVersion": {"major": 4, "minor": 11, "patch": 0, "build": 1},
        **extra,
    }


def _group(**extra: Any) -> dict[str, Any]:
    return {
        "groupId": _GROUP_ID,
        "name": "<ha-group-name>",
        "serviceMeshId": "mesh-0001",
        "groupType": "<group-type>",
        "groupMembers": [
            {
                "applianceId": "ne-0101",
                "applianceName": "<ne-0101>",
                "peerApplianceId": "ne-0201",
                "peerApplianceName": "<ne-0201>",
                "role": "ACTIVE",
            },
            {
                "applianceId": "ne-0102",
                "applianceName": "<ne-0102>",
                "peerApplianceId": "ne-0202",
                "peerApplianceName": "<ne-0202>",
                "role": "STANDBY",
            },
        ],
        "policyInfo": {"policy": "<policy>"},
        "status": {"groupState": "HEALTHY", "statusMessage": "<message>"},
        "networkExtensionCount": 1,
        **extra,
    }


def _accepted(group_id: str = _GROUP_ID) -> dict[str, Any]:
    return {
        "data": {"interconnectTaskId": _TASK_ID, "groupId": group_id},
        "warnings": [],
        "errors": [],
    }


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


@pytest.fixture
def _stub_embedding(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    service = AsyncMock()
    service.encode_one.return_value = [0.1] * 384
    service.encode.return_value = [[0.1] * 384]
    service.dimension = 384
    monkeypatch.setattr(
        "meho_backplane.operations.typed_register.encode_endpoint_text",
        AsyncMock(return_value=[0.1] * 384),
    )
    monkeypatch.setattr(
        "meho_backplane.operations._search.get_embedding_service",
        lambda: service,
    )
    return service


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    async with get_sessionmaker()() as s:
        yield s


class _HcxTarget:
    """Target satisfying both ``HcxTargetLike`` and the resolver shape."""

    def __init__(self) -> None:
        self.product = HCX_PRODUCT
        self.fingerprint = type("_FP", (), {"version": HCX_VERSION})()
        self.preferred_impl_id: str | None = None
        self.id: UUID = uuid.uuid4()
        self.tenant_id: UUID = uuid.UUID("00000000-0000-0000-0000-0000000000e0")
        self.name = "hcx-ha"
        self.host = _HOST
        self.port = 443
        self.secret_ref = "targets/op-ha/hcx-ha"
        self.auth_model = "shared_service_account"


def _operator(sub: str = "op-ha-hcx-typed") -> Operator:
    return Operator(
        sub=sub,
        name="HCX HA Operator",
        email=None,
        raw_jwt="op.ha.hcx.typed.jwt",
        tenant_id=_TENANT_ID,
        tenant_role=TenantRole.OPERATOR,
        principal_kind=PrincipalKind.USER,
    )


async def _session_loader(_target: object, _operator: Operator) -> dict[str, str]:
    return {"username": "hcx-ha-svc", "password": _PASSWORD}


@pytest.fixture
async def _registered(_stub_embedding: AsyncMock, session: AsyncSession) -> None:
    await register_hcx_typed_operations()
    instance = get_or_create_connector_instance(HcxConnector)
    instance._session_loader = _session_loader  # type: ignore[attr-defined]


@pytest.fixture
async def _target_row(_registered: None, session: AsyncSession) -> None:
    """A stored target, so ``call_operation`` and ``preview_operation`` resolve it by name."""
    session.add(
        TargetORM(
            tenant_id=_TENANT_ID,
            name=_TARGET_NAME,
            aliases=[],
            product=HCX_PRODUCT,
            host=_HOST,
            port=443,
            fqdn=None,
            secret_ref="targets/op-ha/hcx-ha",
            auth_model="shared_service_account",
            vpn_required=False,
            extras={},
            fingerprint={"version": "4.11.0"},
            notes="seeded by test_connectors_hcx_ha_groups",
        )
    )
    await session.commit()


def _args(params: dict[str, Any]) -> dict[str, Any]:
    return {
        "connector_id": HCX_CONNECTOR_ID,
        "op_id": _SET,
        "target": _TARGET_NAME,
        "params": params,
    }


async def _approval_effect(call: dict[str, Any]) -> tuple[UUID, dict[str, Any]]:
    """The approval request a parked call made, and the preview stored on it."""
    request_id = UUID(call["extras"]["approval_request_id"])
    async with get_sessionmaker()() as s:
        row = await s.get(ApprovalRequest, request_id)
        assert row is not None
        effect: dict[str, Any] = dict(row.proposed_effect)
        return request_id, effect


async def _approve_and_resume(request_id: UUID) -> Any:
    approver = _operator("op-ha-hcx-approver")
    async with get_sessionmaker()() as s:
        await approve_request(s, request_id, operator=approver, params=None)
        await s.commit()
    async with get_sessionmaker()() as s:
        row = await s.get(ApprovalRequest, request_id)
        assert row is not None
        return await resume_dispatch_after_approval(operator=approver, request=row, params=None)


async def _dispatch(op_id: str, params: dict[str, Any], *, approved: bool = False) -> Any:
    return await dispatch(
        operator=_operator(),
        connector_id=HCX_CONNECTOR_ID,
        op_id=op_id,
        target=_HcxTarget(),
        params=params,
        _approved=approved,
    )


def _login(mock: respx.MockRouter) -> respx.Route:
    return mock.post(_SESSIONS).respond(200, headers={"x-hm-authorization": _TOKEN})


def _mount_reads(
    mock: respx.MockRouter,
    *,
    appliances: list[dict[str, Any]] | None = None,
    groups: list[dict[str, Any]] | None = None,
    tasks: list[dict[str, Any]] | None = None,
) -> respx.Route:
    """Mount the reads a change makes first; returns the task query route."""
    mock.get(_APPLIANCES).respond(200, json={"items": appliances or [_appliance()]})
    mock.get(_GROUPS).respond(200, json={"items": groups if groups is not None else []})
    return mock.post(_TASKS).respond(200, json={"items": tasks or []})


def _body(route: respx.Route, index: int = 0) -> Any:
    return json.loads(route.calls[index].request.content or b"null")


def _op(op_id: str) -> Any:
    return next(op for op in HCX_HA_GROUP_OPS if op.op_id == op_id)


# ---------------------------------------------------------------------------
# Registration and contracts
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.usefixtures("_registered")
async def test_ops_are_registered_with_the_risk_of_the_table(session: AsyncSession) -> None:
    rows = {
        row.op_id: row
        for row in (
            await session.execute(
                select(EndpointDescriptor).where(
                    EndpointDescriptor.product == HCX_PRODUCT,
                    EndpointDescriptor.impl_id == HCX_IMPL_ID,
                )
            )
        )
        .scalars()
        .all()
    }
    expected = {_LIST: ("safe", False), _SET: ("dangerous", True)}
    assert {op.op_id for op in HCX_HA_GROUP_OPS} == set(expected)
    for op_id, (safety, approval) in expected.items():
        row = rows[op_id]
        assert row.source_kind == "typed"
        assert (row.safety_level, row.requires_approval) == (safety, approval)
        assert row.parameter_schema["examples"]
        assert row.summary and row.description
        assert "\n" not in row.summary and len(row.description) <= 300
    assert GROUP_NETWORK_EXTENSION_HA in HCX_TYPED_WHEN_TO_USE_BY_GROUP


def test_examples_validate_and_use_placeholders() -> None:
    for op in HCX_HA_GROUP_OPS:
        for example in op.parameter_schema["examples"]:
            assert validate_params(op.parameter_schema, example) == []
            for value in json.dumps(example).split('"'):
                assert "." not in value, value


@pytest.mark.parametrize(
    "params",
    [
        {"action": "build"},
        {"action": "dissolve"},
        {"action": "build", "appliance_ids": [_APPLIANCE_ID], "group_id": _GROUP_ID},
        {"action": "dissolve", "group_id": _GROUP_ID, "appliance_ids": [_APPLIANCE_ID]},
        {"action": "build", "appliance_ids": []},
        {"action": "recover", "group_id": _GROUP_ID},
    ],
)
def test_set_takes_either_build_or_dissolve_inputs(params: dict[str, Any]) -> None:
    assert validate_params(_op(_SET).parameter_schema, params)


@pytest.mark.asyncio
@pytest.mark.usefixtures("_registered")
@pytest.mark.parametrize("params", [_BUILD, _DISSOLVE])
async def test_every_change_waits_for_approval(params: dict[str, Any]) -> None:
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        _login(mock)
        mock.get(path__startswith="/hybridity/api/").respond(200, json={"items": []})
        create = mock.post(_GROUPS).respond(202, json=_accepted())
        delete = mock.route(method="DELETE").respond(202, json=_accepted())
        result = await _dispatch(_SET, params)

    assert result.status == "awaiting_approval", result
    assert create.call_count == 0
    assert delete.call_count == 0


# ---------------------------------------------------------------------------
# hcx.network_extension.ha.list
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.usefixtures("_registered")
async def test_list_returns_flat_rows_with_members_roles_and_state() -> None:
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        _login(mock)
        route = mock.get(_GROUPS).respond(200, json={"items": [_group()]})
        result = await _dispatch(_LIST, {"service_mesh_id": "mesh-0001", "all_group_members": True})

    assert result.status == "ok", result.error
    query = route.calls[0].request.url.params
    assert (query["serviceMeshId"], query["allGroupMembers"]) == ("mesh-0001", "true")
    (row,) = result.result["items"]
    assert row["group_id"] == _GROUP_ID
    assert row["group_state"] == "HEALTHY"
    assert row["policy"] == "<policy>"
    assert row["network_extension_count"] == 1
    assert [(m["appliance_id"], m["peer_appliance_id"], m["role"]) for m in row["members"]] == [
        ("ne-0101", "ne-0201", "ACTIVE"),
        ("ne-0102", "ne-0202", "STANDBY"),
    ]


@pytest.mark.asyncio
@pytest.mark.usefixtures("_registered")
async def test_list_sends_only_this_site_by_default() -> None:
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        _login(mock)
        route = mock.get(_GROUPS).respond(200, json={"items": []})
        result = await _dispatch(_LIST, {})

    assert result.status == "ok", result.error
    assert dict(route.calls[0].request.url.params) == {"allGroupMembers": "false"}


@pytest.mark.asyncio
@pytest.mark.usefixtures("_registered")
async def test_long_list_goes_behind_a_result_handle() -> None:
    set_default_reducer(JsonFluxReducer())
    groups = [_group(groupId=f"ha-group-{i:04d}") for i in range(80)]
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        _login(mock)
        mock.get(_GROUPS).respond(200, json={"items": groups})
        result = await _dispatch(_LIST, {})

    assert result.status == "ok", result.error
    assert result.handle is not None
    assert result.result["row_count"] == 80


# ---------------------------------------------------------------------------
# hcx.network_extension.ha.set -- build
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.usefixtures("_registered")
async def test_build_starts_the_task_and_says_accepted() -> None:
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        _login(mock)
        tasks = _mount_reads(mock)
        create = mock.post(_GROUPS).respond(202, json=_accepted())
        result = await _dispatch(_SET, _BUILD, approved=True)

    assert result.status == "ok", result.error
    assert _body(create) == {"appliances": [_APPLIANCE_ID]}
    assert _body(tasks) == {
        "filter": {"status": ["QUEUED", "RUNNING"], "entityIds": [_APPLIANCE_ID]}
    }
    assert result.result["status"] == "accepted"
    assert result.result["action"] == "build"
    assert result.result["interconnect_task_id"] == _TASK_ID
    assert result.result["group_id"] == _GROUP_ID
    assert result.result["status_read"] == "hcx.task.get"


@pytest.mark.asyncio
@pytest.mark.usefixtures("_registered")
@pytest.mark.parametrize(
    ("status", "message"),
    [
        (409, "Appliance is already extending a network"),
        (400, "Not enough free Network Extension appliances to form an HA group"),
    ],
)
async def test_build_vendor_refusal_reaches_the_answer(status: int, message: str) -> None:
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        _login(mock)
        _mount_reads(mock)
        mock.post(_GROUPS).respond(
            status, json={"errors": [{"code": "<code>", "message": message}]}
        )
        result = await _dispatch(_SET, _BUILD, approved=True)

    assert result.status == "error"
    assert result.extras["http_status"] == status
    assert message in result.extras["upstream_message"]


@pytest.mark.asyncio
@pytest.mark.usefixtures("_registered")
async def test_build_refuses_an_appliance_older_than_4_3() -> None:
    old = _appliance(applianceVersion={"major": 4, "minor": 2, "patch": 1, "build": 1})
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        _login(mock)
        _mount_reads(mock, appliances=[old])
        create = mock.post(_GROUPS).respond(202, json=_accepted())
        result = await _dispatch(_SET, _BUILD, approved=True)

    assert result.status == "error"
    assert create.call_count == 0
    assert "4.3 or newer" in result.extras["exception_message"]


@pytest.mark.asyncio
@pytest.mark.usefixtures("_registered")
@pytest.mark.parametrize(
    ("appliances", "text"),
    [
        ([_appliance(applianceType="HCX-WAN-IX")], "only Network Extension appliances"),
        ([_appliance("ne-other")], "no appliance with id"),
    ],
)
async def test_build_refuses_an_unknown_or_wrong_appliance(
    appliances: list[dict[str, Any]], text: str
) -> None:
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        _login(mock)
        _mount_reads(mock, appliances=appliances)
        create = mock.post(_GROUPS).respond(202, json=_accepted())
        result = await _dispatch(_SET, _BUILD, approved=True)

    assert result.status == "error"
    assert create.call_count == 0
    assert text in result.extras["exception_message"]


@pytest.mark.asyncio
@pytest.mark.usefixtures("_registered")
async def test_build_starts_nothing_when_the_appliance_is_in_a_group() -> None:
    params = {"action": "build", "appliance_ids": ["ne-0101"]}
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        _login(mock)
        _mount_reads(mock, appliances=[_appliance("ne-0101")], groups=[_group()])
        create = mock.post(_GROUPS).respond(202, json=_accepted())
        result = await _dispatch(_SET, params, approved=True)

    assert result.status == "ok", result.error
    assert create.call_count == 0
    assert result.result["status"] == "already_grouped"
    assert result.result["group_id"] == _GROUP_ID
    assert result.result["group_state"] == "HEALTHY"


@pytest.mark.asyncio
@pytest.mark.usefixtures("_registered")
async def test_build_starts_nothing_while_a_task_runs_on_the_appliance() -> None:
    task = {
        "interconnectTaskId": "task-running",
        "status": "RUNNING",
        "entityDetails": {"id": _APPLIANCE_ID},
    }
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        _login(mock)
        _mount_reads(mock, tasks=[task])
        create = mock.post(_GROUPS).respond(202, json=_accepted())
        result = await _dispatch(_SET, _BUILD, approved=True)

    assert result.status == "ok", result.error
    assert create.call_count == 0
    assert result.result["status"] == "already_running"
    assert result.result["interconnect_task_id"] == "task-running"


@pytest.mark.asyncio
@pytest.mark.usefixtures("_registered")
async def test_build_answer_without_a_task_id_is_an_error_with_the_vendor_text() -> None:
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        _login(mock)
        _mount_reads(mock)
        mock.post(_GROUPS).respond(
            202, json={"data": {}, "errors": [{"message": "Licence does not allow HA"}]}
        )
        result = await _dispatch(_SET, _BUILD, approved=True)

    assert result.status == "error"
    assert "Licence does not allow HA" in result.extras["exception_message"]


# ---------------------------------------------------------------------------
# hcx.network_extension.ha.set -- dissolve
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.usefixtures("_registered")
async def test_dissolve_starts_the_task_without_force() -> None:
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        _login(mock)
        tasks = _mount_reads(mock, groups=[_group()])
        delete = mock.delete(f"{_GROUPS}/{_GROUP_ID}").respond(202, json=_accepted())
        result = await _dispatch(_SET, _DISSOLVE, approved=True)

    assert result.status == "ok", result.error
    assert delete.calls[0].request.url.params["force"] == "false"
    assert _body(tasks)["filter"]["entityIds"] == [_GROUP_ID]
    assert result.result["status"] == "accepted"
    assert result.result["action"] == "dissolve"
    assert result.result["interconnect_task_id"] == _TASK_ID
    assert result.result["group_state"] == "HEALTHY"
    assert result.result["status_read"] == "hcx.task.get"


@pytest.mark.asyncio
@pytest.mark.usefixtures("_registered")
async def test_dissolve_of_a_missing_group_starts_nothing() -> None:
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        _login(mock)
        _mount_reads(mock, groups=[])
        delete = mock.route(method="DELETE").respond(202, json=_accepted())
        result = await _dispatch(_SET, _DISSOLVE, approved=True)

    assert result.status == "ok", result.error
    assert delete.call_count == 0
    assert result.result["status"] == "not_found"


@pytest.mark.asyncio
@pytest.mark.usefixtures("_registered")
async def test_dissolve_starts_nothing_while_a_task_runs_on_the_group() -> None:
    task = {
        "interconnectTaskId": "task-running",
        "status": "QUEUED",
        "entityDetails": {"id": _GROUP_ID},
    }
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        _login(mock)
        _mount_reads(mock, groups=[_group()], tasks=[task])
        delete = mock.route(method="DELETE").respond(202, json=_accepted())
        result = await _dispatch(_SET, _DISSOLVE, approved=True)

    assert result.status == "ok", result.error
    assert delete.call_count == 0
    assert result.result["status"] == "already_running"
    assert result.result["interconnect_task_id"] == "task-running"


@pytest.mark.asyncio
@pytest.mark.usefixtures("_registered")
async def test_dissolve_vendor_error_reaches_the_answer() -> None:
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        _login(mock)
        _mount_reads(mock, groups=[_group()])
        mock.delete(f"{_GROUPS}/{_GROUP_ID}").respond(
            409, json={"errors": [{"code": "<code>", "message": "Extensions cannot be moved"}]}
        )
        result = await _dispatch(_SET, _DISSOLVE, approved=True)

    assert result.status == "error"
    assert "Extensions cannot be moved" in result.extras["upstream_message"]


@pytest.mark.asyncio
@pytest.mark.usefixtures("_registered")
async def test_dissolve_keeps_the_id_one_path_segment() -> None:
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        _login(mock)
        other = mock.route().respond(200, json={"items": []})
        result = await _dispatch(_SET, {"action": "dissolve", "group_id": "../x"}, approved=True)

    assert result.status == "error"
    assert other.call_count == 0


# ---------------------------------------------------------------------------
# The dissolve preview
# ---------------------------------------------------------------------------


@dataclass
class _FakeDescriptor:
    op_id: str


async def _preview(params: dict[str, Any]) -> dict[str, Any] | None:
    instance = get_or_create_connector_instance(HcxConnector)
    instance._session_loader = _session_loader  # type: ignore[attr-defined]
    return await build_proposed_effect(
        PreviewContext(
            descriptor=_FakeDescriptor(op_id=_SET),  # type: ignore[arg-type]
            connector_instance=instance,
            operator=_operator(),
            target=_HcxTarget(),
            params=params,
            connector_id=HCX_CONNECTOR_ID,
        )
    )


def _radius(effect: dict[str, Any] | None) -> dict[str, Any]:
    assert effect is not None
    radius: dict[str, Any] = (
        effect["preview"]["blast_radius"] if "preview" in effect else effect["blast_radius"]
    )
    return radius


def _extension(stretch_id: str, appliance_id: str) -> dict[str, Any]:
    return {
        "stretchId": stretch_id,
        "sourceNetwork": {"networkName": f"<network-{stretch_id}>"},
        "sourceAppliance": {"applianceId": appliance_id},
    }


_EXTENSION_ROWS = [
    _extension("stretch-0002", "ne-0101"),
    _extension("stretch-0001", "ne-0101"),
    _extension("stretch-other", "ne-0909"),
]


@pytest.mark.asyncio
async def test_dissolve_preview_lists_the_extensions_the_group_serves() -> None:
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        _login(mock)
        mock.get(_GROUPS).respond(200, json={"items": [_group()]})
        mock.get(_EXTENSIONS).respond(200, json={"items": _EXTENSION_ROWS})
        radius = _radius(await _preview(_DISSOLVE))

    assert radius["object"]["kind"] == "ha_group"
    assert radius["object"]["id"] == _GROUP_ID
    assert radius["object"]["group_state"] == "HEALTHY"
    assert [c["id"] for c in radius["children"]] == ["stretch-0001", "stretch-0002"]
    assert radius["children"][0]["name"] == "<network-stretch-0001>"
    assert radius["match_count"] == 2
    assert "keep running on the appliance pair that was active" in radius["irreversibility"]


@pytest.mark.asyncio
async def test_dissolve_preview_refuses_a_group_that_does_not_exist() -> None:
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        _login(mock)
        mock.get(_GROUPS).respond(200, json={"items": []})
        effect = await _preview(_DISSOLVE)

    assert effect is not None
    assert effect["preview_unavailable"] is True
    assert "does not exist" in effect["preview_error"]


@pytest.mark.asyncio
async def test_build_preview_has_no_blast_radius() -> None:
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        vendor = mock.route().respond(200, json={})
        effect = await _preview(_BUILD)

    assert vendor.call_count == 0
    assert effect is None or "blast_radius" not in json.dumps(effect)


# ---------------------------------------------------------------------------
# The public path: preview_operation, call_operation and the approval request
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.usefixtures("_target_row")
async def test_preview_operation_shows_no_plan_and_contacts_no_manager() -> None:
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        vendor = mock.route().respond(200, json={"items": []})
        preview = await preview_operation(_operator(), _args(_DISSOLVE))

    assert preview["status"] == "ok", preview
    assert vendor.call_count == 0
    assert preview["plan_shown"] is False
    assert preview["plan_note"] == PLAN_NOT_SHOWN_NOTE
    assert "blast_radius" not in json.dumps(preview)
    assert preview["preview_hash"]


@pytest.mark.asyncio
@pytest.mark.usefixtures("_target_row")
async def test_dissolve_approval_request_lists_the_extensions_then_runs() -> None:
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        _login(mock)
        _mount_reads(mock, groups=[_group()])
        mock.get(_EXTENSIONS).respond(200, json={"items": _EXTENSION_ROWS})
        delete = mock.delete(f"{_GROUPS}/{_GROUP_ID}").respond(202, json=_accepted())

        call = await call_operation(_operator(), _args(_DISSOLVE))
        assert call["status"] == "awaiting_approval", call
        assert delete.call_count == 0
        request_id, effect = await _approval_effect(call)
        resumed = await _approve_and_resume(request_id)

    assert effect["preview_populated"] is True
    radius = effect["blast_radius"]
    assert radius["object"]["id"] == _GROUP_ID
    assert [c["id"] for c in radius["children"]] == ["stretch-0001", "stretch-0002"]
    assert resumed.status == "ok", resumed
    assert resumed.result["status"] == "accepted"
    assert resumed.result["interconnect_task_id"] == _TASK_ID
    assert delete.call_count == 1


@pytest.mark.asyncio
@pytest.mark.usefixtures("_target_row")
async def test_dissolve_of_a_missing_group_waits_for_approval_then_says_not_found() -> None:
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        _login(mock)
        _mount_reads(mock, groups=[])
        delete = mock.route(method="DELETE").respond(202, json=_accepted())

        call = await call_operation(_operator(), _args(_DISSOLVE))
        assert call["status"] == "awaiting_approval", call
        request_id, effect = await _approval_effect(call)
        resumed = await _approve_and_resume(request_id)

    assert effect["preview_unavailable"] is True
    assert "does not exist" in effect["preview_error"]
    assert resumed.status == "ok", resumed
    assert resumed.result["status"] == "not_found"
    assert delete.call_count == 0


# ---------------------------------------------------------------------------
# Status reads: hcx.task.get follows the started task
# ---------------------------------------------------------------------------


def _task(status: str, **extra: Any) -> dict[str, Any]:
    return {
        "interconnectTaskId": _TASK_ID,
        "type": "<task-type>",
        "status": status,
        "progress": 40,
        "message": "<status-text>",
        "entityDetails": {"id": _GROUP_ID, "type": "<entity-type>", "name": "<ha-group-name>"},
        "steps": [{"title": "<step>", "status": status, "jobId": "job-0001"}],
        **extra,
    }


async def _start_build(mock: respx.MockRouter) -> str:
    """Start a build; returns the task id the answer names for ``hcx.task.get``."""
    _mount_reads(mock)
    mock.post(_GROUPS).respond(202, json=_accepted())
    started = await _dispatch(_SET, _BUILD, approved=True)
    assert started.status == "ok", started.error
    assert started.result["status"] == "accepted"
    assert started.result["status_read"] == "hcx.task.get"
    return str(started.result["interconnect_task_id"])


@pytest.mark.asyncio
@pytest.mark.usefixtures("_registered")
async def test_task_get_follows_the_started_task_to_its_end() -> None:
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        _login(mock)
        task_id = await _start_build(mock)
        task = mock.get(_TASK_PATH)
        task.side_effect = [
            httpx.Response(200, json=_task("RUNNING")),
            httpx.Response(200, json=_task("SUCCESS", progress=100)),
        ]
        running = await _dispatch("hcx.task.get", {"task_id": task_id})
        done = await _dispatch("hcx.task.get", {"task_id": task_id})

    assert running.status == "ok", running.error
    assert (running.result["status"], running.result["done"]) == ("RUNNING", False)
    assert running.result["progress"] == 40
    assert running.result["entity_id"] == _GROUP_ID
    assert done.status == "ok", done.error
    assert (done.result["status"], done.result["done"]) == ("SUCCESS", True)
    assert done.result["steps"][0]["job_id"] == "job-0001"


@pytest.mark.asyncio
@pytest.mark.usefixtures("_registered")
async def test_task_get_gives_the_vendor_error_of_a_failed_task() -> None:
    failed = _task(
        "FAILED",
        message="HA group creation failed",
        steps=[
            {
                "title": "<step>",
                "status": "FAILED",
                "jobId": "job-0001",
                "errors": [{"message": "Standby appliance is down"}],
            }
        ],
    )
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        _login(mock)
        task_id = await _start_build(mock)
        mock.get(_TASK_PATH).respond(200, json=failed)
        result = await _dispatch("hcx.task.get", {"task_id": task_id})

    assert result.status == "ok", result.error
    assert result.result["done"] is True
    assert result.result["error"] == "HA group creation failed; Standby appliance is down"
    assert result.result["steps"][0]["job_id"] == "job-0001"


@pytest.mark.asyncio
@pytest.mark.usefixtures("_registered")
async def test_task_get_after_the_start_logs_in_again_when_the_login_expired() -> None:
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        login = mock.post(_SESSIONS)
        login.side_effect = [
            httpx.Response(200, headers={"x-hm-authorization": "ha-hm-token-1"}),
            httpx.Response(200, headers={"x-hm-authorization": "ha-hm-token-2"}),
        ]
        task_id = await _start_build(mock)
        task = mock.get(_TASK_PATH)
        task.side_effect = [httpx.Response(401), httpx.Response(200, json=_task("SUCCESS"))]
        result = await _dispatch("hcx.task.get", {"task_id": task_id})

    assert result.status == "ok", result.error
    assert login.call_count == 2
    assert task.calls[1].request.headers["x-hm-authorization"] == "ha-hm-token-2"
    assert (result.result["status"], result.result["done"]) == ("SUCCESS", True)


@pytest.mark.asyncio
@pytest.mark.usefixtures("_registered")
@pytest.mark.parametrize(
    "transport_error",
    [httpx.ConnectError("[Errno 111] Connection refused"), httpx.ReadTimeout("timed out")],
    ids=["refused", "timeout"],
)
async def test_task_get_after_the_start_says_not_reachable_when_the_manager_does_not_answer(
    transport_error: httpx.TransportError,
) -> None:
    hcx = get_or_create_connector_instance(HcxConnector)
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        _login(mock)
        task_id = await _start_build(mock)
        task = mock.get(_TASK_PATH).mock(side_effect=transport_error)
        with patch.object(hcx._request_json.retry, "wait", wait_none()):  # type: ignore[attr-defined]
            result = await _dispatch("hcx.task.get", {"task_id": task_id})

    assert result.status == "ok", result.error
    assert task.called
    assert result.result["status"] == "not_reachable"
    assert result.result["interconnect_task_id"] == task_id
    assert result.result["done"] is False
    assert result.result["checked_at"]


@pytest.mark.asyncio
@pytest.mark.usefixtures("_registered")
async def test_a_failed_job_shows_its_error_in_job_get() -> None:
    job = {"jobId": "job-0001", "isDone": True, "didFail": True, "errorMessage": "Standby is down"}
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        _login(mock)
        mock.get("/hybridity/api/jobs/job-0001").respond(200, json=job)
        result = await _dispatch("hcx.job.get", {"job_id": "job-0001"})

    assert result.status == "ok", result.error
    assert result.result["didFail"] is True
    assert result.result["errorMessage"] == "Standby is down"


@pytest.mark.asyncio
@pytest.mark.usefixtures("_registered")
async def test_results_hold_no_password_or_token() -> None:
    group = _group(password="made-up-ha-password", licenseKey="made-up-key")
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        _login(mock)
        mock.get(_GROUPS).respond(200, json={"items": [group]})
        result = await _dispatch(_LIST, {})

    assert result.status == "ok", result.error
    rendered = json.dumps(result.result) + json.dumps(result.extras, default=str)
    for secret in (_PASSWORD, _TOKEN, "made-up-ha-password", "made-up-key"):
        assert secret not in rendered
