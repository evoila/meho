# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for the HCX network extension operations, with a respx-mocked HCX Manager.

* ``hcx.network_extension.list`` returns flat rows; a long list goes
  behind a result handle;
* every change waits for approval, and is ``dangerous`` with
  ``requires_approval``; the read is ``safe``;
* extend and unextend start a vendor job: the answer says ``accepted``
  with the job id and the status read, and a running or existing
  extension starts nothing;
* a vendor error reaches the answer;
* Mobility Optimized Networking runs its steps in order and, when a step
  fails, lists what ran, what failed and what did not run;
* the status read ``hcx.job.get``: a failed job, a login that expired
  between the start and the read, and a Manager that does not answer.

Fixture values are made up; no real host, user or id appears here.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator, Iterator
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID

import httpx
import pytest
import respx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors.hcx import (
    HCX_CONNECTOR_ID,
    HCX_IMPL_ID,
    HCX_PRODUCT,
    HCX_VERSION,
    HcxConnector,
    register_hcx_typed_operations,
)
from meho_backplane.connectors.hcx.network_extension import is_busy
from meho_backplane.connectors.hcx.network_extension_ops import (
    GROUP_NETWORK_EXTENSION,
    HCX_NETWORK_EXTENSION_OPS,
)
from meho_backplane.connectors.hcx.typed_ops import HCX_TYPED_WHEN_TO_USE_BY_GROUP
from meho_backplane.connectors.hcx.typed_reads import job_outcome
from meho_backplane.connectors.registry import clear_registry, register_connector_v2
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import EndpointDescriptor
from meho_backplane.operations import dispatch, reset_dispatcher_caches, set_default_reducer
from meho_backplane.operations._handler_resolve import (
    get_or_create_connector_instance,
    reset_handler_cache,
)
from meho_backplane.operations._validate import validate_params
from meho_backplane.operations.jsonflux_reducer import JsonFluxReducer
from meho_backplane.settings import get_settings

_HOST = "hcx-netext.test.invalid"
_BASE_URL = f"https://{_HOST}"
_SESSIONS = "/hybridity/api/sessions"
_LIST = "/hybridity/api/l2Extensions"
_POLICY_ROUTES = "/hybridity/api/l2Extensions/policyRoutes"
_TOKEN = "netext-hm-token-1"
_PASSWORD = "netext-password-value"
_STRETCH_ID = "stretch-0001"
_NETWORK_ID = "network-0001"
_JOB_ID = "job-0001"
_TENANT_ID = UUID("00000000-0000-0000-0000-0000000000d5")

_CHANGES = (
    "hcx.network_extension.extend",
    "hcx.network_extension.unextend",
    "hcx.network_extension.mon.set",
)

_EXTEND_PARAMS: dict[str, Any] = {
    "source_network": {
        "network_id": _NETWORK_ID,
        "network_name": "netext-network",
        "network_type": "DistributedVirtualPortgroup",
    },
    "destination": {"endpoint_id": "endpoint-0002", "resource_id": "resource-0002"},
    "destination_gateway_id": "router-0002",
    "source_appliance_id": "appliance-0001",
    "gateway": "<gateway-address>",
    "netmask": "<netmask>",
    "mobility_optimized_networking": True,
}


def _extension(state: str = "SUCCESS", **extra: Any) -> dict[str, Any]:
    return {
        "stretchId": _STRETCH_ID,
        "gateway": "<gateway-address>",
        "netmask": "<netmask>",
        "operationStatus": {"state": state},
        "sourceNetwork": {
            "networkId": _NETWORK_ID,
            "networkName": "netext-network",
            "networkType": "DistributedVirtualPortgroup",
        },
        "destinationNetwork": {"networkName": "netext-network-cloud", "gatewayId": "router-0002"},
        "destination": {"endpointId": "endpoint-0002", "resourceId": "resource-0002"},
        "sourceAppliance": {"applianceId": "appliance-0001"},
        "features": {"egressOptimization": False, "mobilityOptimizedNetworking": False},
        **extra,
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
        self.tenant_id: UUID = uuid.UUID("00000000-0000-0000-0000-0000000000d0")
        self.name = "hcx-netext"
        self.host = _HOST
        self.port = 443
        self.secret_ref = "targets/op-netext/hcx-netext"
        self.auth_model = "shared_service_account"


def _operator() -> Operator:
    return Operator(
        sub="op-netext-hcx-typed",
        name="HCX Network Extension Operator",
        email=None,
        raw_jwt="op.netext.hcx.typed.jwt",
        tenant_id=_TENANT_ID,
        tenant_role=TenantRole.OPERATOR,
    )


async def _session_loader(_target: object, _operator: Operator) -> dict[str, str]:
    return {"username": "hcx-netext-svc", "password": _PASSWORD}


@pytest.fixture
async def _registered(_stub_embedding: AsyncMock, session: AsyncSession) -> None:
    await register_hcx_typed_operations()
    instance = get_or_create_connector_instance(HcxConnector)
    instance._session_loader = _session_loader  # type: ignore[attr-defined]


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


def _body(route: respx.Route, index: int = 0) -> Any:
    return json.loads(route.calls[index].request.content or b"null")


def _op(op_id: str) -> Any:
    return next(op for op in HCX_NETWORK_EXTENSION_OPS if op.op_id == op_id)


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
    expected = {
        "hcx.network_extension.list": ("safe", False),
        "hcx.network_extension.extend": ("dangerous", True),
        "hcx.network_extension.unextend": ("dangerous", True),
        "hcx.network_extension.mon.set": ("dangerous", True),
    }
    assert {op.op_id for op in HCX_NETWORK_EXTENSION_OPS} == set(expected)
    for op_id, (safety, approval) in expected.items():
        row = rows[op_id]
        assert row.source_kind == "typed"
        assert (row.safety_level, row.requires_approval) == (safety, approval)
        assert row.parameter_schema["examples"]
        assert "\n" not in row.summary and len(row.description) <= 300
    assert GROUP_NETWORK_EXTENSION in HCX_TYPED_WHEN_TO_USE_BY_GROUP


def test_examples_validate_and_use_placeholders() -> None:
    for op in HCX_NETWORK_EXTENSION_OPS:
        for example in op.parameter_schema["examples"]:
            assert validate_params(op.parameter_schema, example) == []
            for value in json.dumps(example).split('"'):
                assert "." not in value or value.startswith("hcx"), value


def test_mon_set_needs_mon_or_policy_routes() -> None:
    schema = _op("hcx.network_extension.mon.set").parameter_schema
    assert validate_params(schema, {"stretch_id": _STRETCH_ID})
    only_mon = {"stretch_id": _STRETCH_ID, "mobility_optimized_networking": False}
    assert validate_params(schema, only_mon) == []
    only_routes = {"stretch_id": _STRETCH_ID, "policy_routes": {"allow": []}}
    assert validate_params(schema, only_routes) == []


@pytest.mark.asyncio
@pytest.mark.usefixtures("_registered")
@pytest.mark.parametrize(
    ("op_id", "params"),
    [
        ("hcx.network_extension.extend", _EXTEND_PARAMS),
        ("hcx.network_extension.unextend", {"stretch_id": _STRETCH_ID}),
        (
            "hcx.network_extension.mon.set",
            {"stretch_id": _STRETCH_ID, "mobility_optimized_networking": True},
        ),
    ],
)
async def test_every_change_waits_for_approval(op_id: str, params: dict[str, Any]) -> None:
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        _login(mock)
        vendor = mock.route(path__startswith="/hybridity/api/l2Extensions").respond(200, json={})
        other = mock.route(path__startswith="/hybridity/api/networkExtensions").respond(202)
        result = await _dispatch(op_id, params)

    assert op_id in _CHANGES
    assert result.status == "awaiting_approval", result
    assert vendor.call_count == 0
    assert other.call_count == 0


# ---------------------------------------------------------------------------
# hcx.network_extension.list
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.usefixtures("_registered")
async def test_list_returns_flat_rows() -> None:
    row = _extension(operationStatus={"state": "SUCCESS", "jobId": _JOB_ID})
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        _login(mock)
        route = mock.get(_LIST).respond(200, json={"items": [row]})
        result = await _dispatch("hcx.network_extension.list", {})

    assert result.status == "ok", result.error
    assert route.calls[0].request.headers["x-hm-authorization"] == _TOKEN
    item = result.result["items"][0]
    assert item["stretch_id"] == _STRETCH_ID
    assert item["state"] == "SUCCESS"
    assert item["job_id"] == _JOB_ID
    assert item["source_network_id"] == _NETWORK_ID
    assert item["destination_gateway_id"] == "router-0002"
    assert item["destination_endpoint_id"] == "endpoint-0002"
    assert item["mobility_optimized_networking"] is False
    assert item["sourceNetwork"]["networkName"] == "netext-network"


@pytest.mark.asyncio
@pytest.mark.usefixtures("_registered")
async def test_long_list_goes_behind_a_result_handle() -> None:
    set_default_reducer(JsonFluxReducer())
    rows = [_extension(stretchId=f"stretch-{i:04d}") for i in range(80)]
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        _login(mock)
        mock.get(_LIST).respond(200, json={"items": rows})
        result = await _dispatch("hcx.network_extension.list", {})

    assert result.status == "ok", result.error
    assert result.handle is not None
    assert result.result["row_count"] == 80


# ---------------------------------------------------------------------------
# hcx.network_extension.extend
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.usefixtures("_registered")
async def test_extend_starts_the_job_and_says_accepted() -> None:
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        _login(mock)
        mock.get(_LIST).respond(200, json={"items": []})
        create = mock.post(_LIST).respond(200, json={"id": _JOB_ID})
        result = await _dispatch("hcx.network_extension.extend", _EXTEND_PARAMS, approved=True)

    assert result.status == "ok", result.error
    assert result.result["status"] == "accepted"
    assert result.result["job_id"] == _JOB_ID
    assert result.result["status_read"] == "hcx.job.get"
    body = _body(create)
    assert body["sourceNetwork"] == {
        "networkId": _NETWORK_ID,
        "networkName": "netext-network",
        "networkType": "DistributedVirtualPortgroup",
    }
    assert body["destination"]["endpointId"] == "endpoint-0002"
    assert body["destination"]["resourceId"] == "resource-0002"
    assert body["destinationNetwork"] == {"gatewayId": "router-0002"}
    assert body["sourceAppliance"] == {"applianceId": "appliance-0001"}
    assert body["features"] == {"egressOptimization": False, "mobilityOptimizedNetworking": True}
    assert (body["gateway"], body["netmask"], body["dns"]) == ("<gateway-address>", "<netmask>", [])
    assert "vcGuid" not in body


@pytest.mark.asyncio
@pytest.mark.usefixtures("_registered")
@pytest.mark.parametrize("state", ["EXTENSION_IN_PROGRESS", "SUCCESS"])
async def test_extend_starts_nothing_when_the_network_has_an_extension(state: str) -> None:
    row = _extension(operationStatus={"state": state, "jobId": _JOB_ID})
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        _login(mock)
        mock.get(_LIST).respond(200, json={"items": [row]})
        create = mock.post(_LIST).respond(200, json={"id": "job-new"})
        result = await _dispatch("hcx.network_extension.extend", _EXTEND_PARAMS, approved=True)

    assert result.status == "ok", result.error
    assert create.call_count == 0
    assert result.result["status"] == "already_extended"
    assert result.result["job_id"] == _JOB_ID
    assert result.result["stretch_id"] == _STRETCH_ID
    assert result.result["state"] == state


@pytest.mark.asyncio
@pytest.mark.usefixtures("_registered")
async def test_extend_vendor_error_reaches_the_answer() -> None:
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        _login(mock)
        mock.get(_LIST).respond(200, json={"items": []})
        mock.post(_LIST).respond(
            400, json={"errors": [{"code": "<code>", "message": "Gateway is not valid"}]}
        )
        result = await _dispatch("hcx.network_extension.extend", _EXTEND_PARAMS, approved=True)

    assert result.status == "error"
    assert result.extras["http_status"] == 400
    assert "Gateway is not valid" in result.extras["upstream_message"]


# ---------------------------------------------------------------------------
# hcx.network_extension.unextend
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.usefixtures("_registered")
@pytest.mark.parametrize(("connect", "flag"), [(True, "true"), (False, "false")])
async def test_unextend_starts_the_job_with_the_router_flag(connect: bool, flag: str) -> None:
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        _login(mock)
        mock.get(_LIST).respond(200, json={"items": [_extension()]})
        delete = mock.delete(f"{_LIST}/{_STRETCH_ID}").respond(200, json={"id": _JOB_ID})
        result = await _dispatch(
            "hcx.network_extension.unextend",
            {"stretch_id": _STRETCH_ID, "connect_to_local_router": connect},
            approved=True,
        )

    assert result.status == "ok", result.error
    assert delete.call_count == 1
    assert delete.calls[0].request.url.params["connectToLocalRouter"] == flag
    assert result.result["status"] == "accepted"
    assert result.result["job_id"] == _JOB_ID
    assert result.result["status_read"] == "hcx.job.get"


@pytest.mark.asyncio
@pytest.mark.usefixtures("_registered")
async def test_unextend_starts_nothing_while_work_runs_on_the_extension() -> None:
    row = _extension(operationStatus={"state": "UNEXTEND_IN_PROGRESS", "jobId": _JOB_ID})
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        _login(mock)
        mock.get(_LIST).respond(200, json={"items": [row]})
        delete = mock.delete(f"{_LIST}/{_STRETCH_ID}").respond(200, json={"id": "job-new"})
        result = await _dispatch(
            "hcx.network_extension.unextend", {"stretch_id": _STRETCH_ID}, approved=True
        )

    assert result.status == "ok", result.error
    assert delete.call_count == 0
    assert result.result["status"] == "already_running"
    assert result.result["job_id"] == _JOB_ID


@pytest.mark.asyncio
@pytest.mark.usefixtures("_registered")
async def test_unextend_of_a_missing_extension_starts_nothing() -> None:
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        _login(mock)
        mock.get(_LIST).respond(200, json={"items": []})
        delete = mock.route(method="DELETE").respond(200, json={"id": "job-new"})
        result = await _dispatch(
            "hcx.network_extension.unextend", {"stretch_id": _STRETCH_ID}, approved=True
        )

    assert result.status == "ok", result.error
    assert delete.call_count == 0
    assert result.result["status"] == "not_found"


@pytest.mark.asyncio
@pytest.mark.usefixtures("_registered")
async def test_unextend_vendor_error_reaches_the_answer() -> None:
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        _login(mock)
        mock.get(_LIST).respond(200, json={"items": [_extension()]})
        mock.delete(f"{_LIST}/{_STRETCH_ID}").respond(
            500, json={"errors": [{"code": "<code>", "message": "Appliance is down"}]}
        )
        result = await _dispatch(
            "hcx.network_extension.unextend", {"stretch_id": _STRETCH_ID}, approved=True
        )

    assert result.status == "error"
    assert "Appliance is down" in result.extras["upstream_message"]


@pytest.mark.asyncio
@pytest.mark.usefixtures("_registered")
async def test_unextend_keeps_the_id_one_path_segment() -> None:
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        _login(mock)
        other = mock.route().respond(200, json={"items": []})
        result = await _dispatch(
            "hcx.network_extension.unextend", {"stretch_id": "../x"}, approved=True
        )

    assert result.status == "error"
    assert other.call_count == 0


@pytest.mark.parametrize(
    ("state", "busy"),
    [
        ("EXTENSION_IN_PROGRESS", True),
        ("unextend_in_progress", True),
        ("Tearing Down", True),
        ("SUCCESS", False),
        ("FAILED", False),
        (None, False),
    ],
)
def test_is_busy(state: Any, busy: bool) -> None:
    assert is_busy(state) is busy


# ---------------------------------------------------------------------------
# hcx.network_extension.mon.set
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.usefixtures("_registered")
async def test_mon_set_turns_mon_on_and_sets_the_policy_routes() -> None:
    params = {
        "stretch_id": _STRETCH_ID,
        "mobility_optimized_networking": True,
        "policy_routes": {"allow": ["<subnet-a>/<prefix>"], "deny": ["<subnet-b>/<prefix>"]},
    }
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        _login(mock)
        mock.get(_LIST).respond(200, json={"items": [_extension()]})
        mon = mock.put(f"/hybridity/api/networkExtensions/{_STRETCH_ID}").respond(
            202, json={"warnings": [{"message": "MON takes a moment"}], "errors": []}
        )
        routes = mock.put(_POLICY_ROUTES).respond(200, json={"endpointId": "endpoint-0002"})
        result = await _dispatch("hcx.network_extension.mon.set", params, approved=True)

    assert result.status == "ok", result.error
    assert result.result["status"] == "accepted"
    assert result.result["steps_ran"] == ["mobility_optimized_networking", "policy_routes"]
    assert result.result["step_failed"] is None
    assert result.result["steps_not_run"] == []
    assert result.result["warnings"] == ["MON takes a moment"]
    mon_body = _body(mon)
    assert mon_body["stretchId"] == _STRETCH_ID
    assert mon_body["features"]["mobilityOptimizedNetworking"] is True
    assert "operationStatus" not in mon_body
    assert _body(routes) == {
        "endpointId": "endpoint-0002",
        "allow": ["<subnet-a>/<prefix>"],
        "deny": ["<subnet-b>/<prefix>"],
    }


@pytest.mark.asyncio
@pytest.mark.usefixtures("_registered")
async def test_mon_set_failure_at_step_two_lists_what_ran_and_what_failed() -> None:
    params = {
        "stretch_id": _STRETCH_ID,
        "mobility_optimized_networking": True,
        "policy_routes": {"allow": ["<subnet-a>/<prefix>"]},
    }
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        _login(mock)
        mock.get(_LIST).respond(200, json={"items": [_extension()]})
        mon = mock.put(f"/hybridity/api/networkExtensions/{_STRETCH_ID}").respond(202, json={})
        mock.put(_POLICY_ROUTES).respond(
            500, json={"errors": [{"code": "<code>", "message": "Router rejected the routes"}]}
        )
        result = await _dispatch("hcx.network_extension.mon.set", params, approved=True)

    assert result.status == "ok", result.error
    assert mon.call_count == 1
    assert result.result["status"] == "partial"
    assert result.result["steps_ran"] == ["mobility_optimized_networking"]
    assert result.result["step_failed"]["step"] == "policy_routes"
    assert "Router rejected the routes" in result.result["step_failed"]["error"]
    assert result.result["steps_not_run"] == []


@pytest.mark.asyncio
@pytest.mark.usefixtures("_registered")
async def test_mon_set_failure_at_step_one_runs_no_later_step() -> None:
    params = {
        "stretch_id": _STRETCH_ID,
        "mobility_optimized_networking": False,
        "policy_routes": {"allow": ["<subnet-a>/<prefix>"]},
    }
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        _login(mock)
        mock.get(_LIST).respond(200, json={"items": [_extension()]})
        mock.put(f"/hybridity/api/networkExtensions/{_STRETCH_ID}").respond(
            202, json={"errors": [{"code": "<code>", "message": "MON is not supported here"}]}
        )
        routes = mock.put(_POLICY_ROUTES).respond(200, json={})
        result = await _dispatch("hcx.network_extension.mon.set", params, approved=True)

    assert result.status == "ok", result.error
    assert routes.call_count == 0
    assert result.result["status"] == "failed"
    assert result.result["steps_ran"] == []
    assert result.result["step_failed"] == {
        "step": "mobility_optimized_networking",
        "error": "MON is not supported here",
    }
    assert result.result["steps_not_run"] == ["policy_routes"]


@pytest.mark.asyncio
@pytest.mark.usefixtures("_registered")
async def test_mon_set_of_a_missing_extension_changes_nothing() -> None:
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        _login(mock)
        mock.get(_LIST).respond(200, json={"items": []})
        put = mock.route(method="PUT").respond(202, json={})
        result = await _dispatch(
            "hcx.network_extension.mon.set",
            {"stretch_id": _STRETCH_ID, "mobility_optimized_networking": True},
            approved=True,
        )

    assert result.status == "error"
    assert put.call_count == 0
    assert _STRETCH_ID in result.extras["exception_message"]


# ---------------------------------------------------------------------------
# The status read: hcx.job.get
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("job", "outcome"),
    [
        ({"isQueued": True, "isDone": False}, "queued"),
        ({"isDone": False, "percentComplete": 40}, "running"),
        ({"isDone": True, "didFail": False}, "succeeded"),
        ({"isDone": True, "didFail": True, "isRolledBack": True}, "failed"),
        ({"isDone": True, "isCancelled": True}, "cancelled"),
        ({"isDone": True, "isRolledBack": True}, "rolled_back"),
    ],
)
def test_job_outcome(job: dict[str, Any], outcome: str) -> None:
    assert job_outcome(job) == outcome


@pytest.mark.asyncio
@pytest.mark.usefixtures("_registered")
async def test_job_get_reports_a_failed_job_with_the_vendor_error() -> None:
    job = {"jobId": _JOB_ID, "isDone": True, "didFail": True, "errorMessage": "Tunnel is down"}
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        _login(mock)
        mock.get(f"/hybridity/api/jobs/{_JOB_ID}").respond(200, json=job)
        result = await _dispatch("hcx.job.get", {"job_id": _JOB_ID})

    assert result.status == "ok", result.error
    assert result.result["outcome"] == "failed"
    assert result.result["error"] == "Tunnel is down"


@pytest.mark.asyncio
@pytest.mark.usefixtures("_registered")
async def test_job_get_logs_in_again_when_the_login_expired_after_the_start() -> None:
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        login = mock.post(_SESSIONS)
        login.side_effect = [
            httpx.Response(200, headers={"x-hm-authorization": "netext-hm-token-1"}),
            httpx.Response(200, headers={"x-hm-authorization": "netext-hm-token-2"}),
        ]
        mock.get(_LIST).respond(200, json={"items": []})
        mock.post(_LIST).respond(200, json={"id": _JOB_ID})
        started = await _dispatch("hcx.network_extension.extend", _EXTEND_PARAMS, approved=True)
        job = mock.get(f"/hybridity/api/jobs/{_JOB_ID}")
        job.side_effect = [
            httpx.Response(401),
            httpx.Response(200, json={"jobId": _JOB_ID, "isDone": False, "percentComplete": 10}),
        ]
        result = await _dispatch("hcx.job.get", {"job_id": started.result["job_id"]})

    assert result.status == "ok", result.error
    assert login.call_count == 2
    assert job.calls[1].request.headers["x-hm-authorization"] == "netext-hm-token-2"
    assert result.result["outcome"] == "running"
    assert result.result["percentComplete"] == 10


@pytest.mark.asyncio
@pytest.mark.usefixtures("_registered")
async def test_job_get_says_not_reachable_when_the_manager_does_not_answer() -> None:
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        _login(mock)
        mock.get(f"/hybridity/api/jobs/{_JOB_ID}").mock(
            side_effect=httpx.ConnectError("connection refused")
        )
        result = await _dispatch("hcx.job.get", {"job_id": _JOB_ID})

    assert result.status == "ok", result.error
    assert result.result["outcome"] == "not_reachable"
    assert result.result["jobId"] == _JOB_ID
    assert result.result["checked_at"]


@pytest.mark.asyncio
@pytest.mark.usefixtures("_registered")
async def test_results_hold_no_password_or_token() -> None:
    row = _extension(password="made-up-ne-password", licenseKey="made-up-key")
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        _login(mock)
        mock.get(_LIST).respond(200, json={"items": [row]})
        result = await _dispatch("hcx.network_extension.list", {})

    assert result.status == "ok", result.error
    rendered = json.dumps(result.result) + json.dumps(result.extras, default=str)
    for secret in (_PASSWORD, _TOKEN, "made-up-ne-password", "made-up-key"):
        assert secret not in rendered
