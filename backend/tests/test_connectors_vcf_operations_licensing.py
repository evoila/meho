# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for the licence-management operations of the VCF Operations 9.x connector.

Checks, without a network call (respx):

* each handler calls the right method and path, with the
  ``X-vRealizeOps-API-use-unsupported`` header and the ``OpsToken`` session;
* the vCenter query sends ``page`` / ``pageSize`` as query params;
* the assign call sends the vendor's JSON array body;
* the calls with an unpublished answer return it under ``payload``;
* the metadata: the reads are safe and read-only, the assign call waits for
  approval, and only the 9.x registrar registers them (not the 8.x one).
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
import respx

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors._shared.vcf_auth import VcfTargetLike
from meho_backplane.connectors.schemas import AuthModel
from meho_backplane.connectors.vcf_operations import VROPS_TYPED_OPS, VcfOperationsConnector
from meho_backplane.connectors.vcf_operations.licensing_ops import (
    VROPS_LICENSING_ASSIGN_OP,
    VROPS_LICENSING_OPS,
)
from meho_backplane.operations._validate import validate_params

_HOST = "vrops-licensing.test.invalid"
_BASE_URL = f"https://{_HOST}"
_ACQUIRE_PATH = "/suite-api/api/auth/token/acquire"
_OPS_TOKEN = "vrops-licensing-token"
_CLOUD = "/suite-api/internal/extension/vcf-license-cloud-integration"
_ENT = "/suite-api/internal/extension/vcf-entitlement"
_HEADER = "X-vRealizeOps-API-use-unsupported"


@dataclass
class _StubTarget:
    name: str = "vrops-licensing"
    host: str = _HOST
    port: int | None = 443
    secret_ref: str = "vrops/vrops-licensing"
    auth_model: str | None = AuthModel.SHARED_SERVICE_ACCOUNT.value
    auth_source: str | None = None
    tls_server_name: str | None = None
    id: UUID = field(default_factory=uuid4)
    tenant_id: UUID = field(default_factory=lambda: UUID(int=0))


async def _stub_loader(_target: VcfTargetLike, _operator: Operator) -> dict[str, str]:
    return {"username": "admin", "password": "stub-password"}


def _operator() -> Operator:
    return Operator(
        sub="test-operator",
        name=None,
        email=None,
        raw_jwt="op.test.jwt",
        tenant_id=UUID(int=0),
        tenant_role=TenantRole.OPERATOR,
    )


Handler = Callable[[VcfOperationsConnector, Operator, Any, dict[str, Any]], Awaitable[Any]]


@pytest.mark.parametrize(
    ("handler", "method", "path", "params", "vendor_answer", "wrapped"),
    [
        (
            VcfOperationsConnector.licensing_registration_status,
            "GET",
            f"{_CLOUD}/registration/status",
            {},
            {"mode": "DISCONNECTED"},
            True,
        ),
        (
            VcfOperationsConnector.licensing_server_list,
            "GET",
            f"{_CLOUD}/license-servers",
            {},
            [{"connectivityStatus": "CONNECTED"}],
            True,
        ),
        (
            VcfOperationsConnector.licensing_entitlement_query,
            "POST",
            f"{_ENT}/entitlements/query",
            {},
            {"entitlementWithVcentersDetails": [], "totalCount": 0},
            False,
        ),
        (
            VcfOperationsConnector.licensing_vcenter_query,
            "POST",
            f"{_ENT}/vcenter-systems/query",
            {},
            {"vcenterSystems": [{"id": "vc-1", "state": "EVALUATION"}]},
            False,
        ),
        (
            VcfOperationsConnector.licensing_server_query,
            "POST",
            f"{_ENT}/license-servers/query",
            {},
            {"licenseServers": []},
            True,
        ),
    ],
    ids=[
        "registration-status",
        "server-list",
        "entitlement-query",
        "vcenter-query",
        "server-query",
    ],
)
@pytest.mark.asyncio
async def test_each_read_calls_the_internal_path_with_the_unsupported_header(
    handler: Handler,
    method: str,
    path: str,
    params: dict[str, Any],
    vendor_answer: Any,
    wrapped: bool,
) -> None:
    connector = VcfOperationsConnector(credentials_loader=_stub_loader)
    async with respx.mock(base_url=_BASE_URL, assert_all_called=True) as mock:
        mock.post(_ACQUIRE_PATH).respond(200, json={"token": _OPS_TOKEN})
        route = mock.request(method, path).respond(200, json=vendor_answer)
        result = await handler(connector, _operator(), _StubTarget(), params)
    await connector.aclose()

    assert result == ({"payload": vendor_answer} if wrapped else vendor_answer)
    request = route.calls[0].request
    assert request.headers[_HEADER] == "true"
    assert request.headers["authorization"] == f"OpsToken {_OPS_TOKEN}"
    if method == "POST":
        assert json.loads(request.content) == {}


@pytest.mark.asyncio
async def test_vcenter_query_sends_paging_as_query_params() -> None:
    connector = VcfOperationsConnector(credentials_loader=_stub_loader)
    async with respx.mock(base_url=_BASE_URL) as mock:
        mock.post(_ACQUIRE_PATH).respond(200, json={"token": _OPS_TOKEN})
        route = mock.post(f"{_ENT}/vcenter-systems/query").respond(200, json={"vcenterSystems": []})
        await connector.licensing_vcenter_query(
            _operator(), _StubTarget(), {"page": 0, "pageSize": 20}
        )
    await connector.aclose()

    sent = route.calls[0].request.url.params
    assert sent["page"] == "0"
    assert sent["pageSize"] == "20"


@pytest.mark.asyncio
async def test_assign_sends_the_vendor_array_body() -> None:
    connector = VcfOperationsConnector(credentials_loader=_stub_loader)
    async with respx.mock(base_url=_BASE_URL) as mock:
        mock.post(_ACQUIRE_PATH).respond(200, json={"token": _OPS_TOKEN})
        route = mock.post(f"{_ENT}/assign").respond(200, json={"status": "OK"})
        result = await connector.licensing_assign(
            _operator(),
            _StubTarget(),
            {"vcenterId": "vc-1", "vcenterHost": "vc-1.test.invalid", "licenseIds": ["lic-1"]},
        )
    await connector.aclose()

    assert result == {"payload": {"status": "OK"}}
    request = route.calls[0].request
    assert request.headers[_HEADER] == "true"
    assert json.loads(request.content) == [
        {
            "vcenter": {
                "id": "vc-1",
                "adapterName": "vc-1.test.invalid",
                "host": "vc-1.test.invalid",
            },
            "allocationIds": ["lic-1"],
        }
    ]


def test_assign_schema_needs_a_vcenter_and_at_least_one_licence() -> None:
    schema = VROPS_LICENSING_ASSIGN_OP.parameter_schema
    assert validate_params(schema, {"vcenterId": "vc-1", "vcenterHost": "vc", "licenseIds": []})
    assert validate_params(schema, {"vcenterHost": "vc", "licenseIds": ["lic-1"]})
    assert not validate_params(
        schema, {"vcenterId": "vc-1", "vcenterHost": "vc", "licenseIds": ["lic-1"]}
    )


def test_reads_are_safe_and_assign_waits_for_approval() -> None:
    by_id = {op.op_id: op for op in VROPS_LICENSING_OPS}
    assert set(by_id) == {
        "vrops.licensing.registration.status",
        "vrops.licensing.server.list",
        "vrops.licensing.entitlement.query",
        "vrops.licensing.vcenter.query",
        "vrops.licensing.server.query",
        "vrops.licensing.assign",
    }
    assign = by_id.pop("vrops.licensing.assign")
    assert assign.safety_level == "dangerous"
    assert assign.requires_approval is True
    assert "read-only" not in assign.tags
    for op in by_id.values():
        assert op.safety_level == "safe"
        assert op.requires_approval is False
        assert "read-only" in op.tags
    for op in VROPS_LICENSING_OPS:
        assert callable(getattr(VcfOperationsConnector, op.handler_attr))


def test_licensing_ops_stay_out_of_the_shared_8x_tuple() -> None:
    """The 8.x connector reuses VROPS_TYPED_OPS; VCF 9 licensing is 9.x only."""
    shared = {op.op_id for op in VROPS_TYPED_OPS}
    assert not shared & {op.op_id for op in VROPS_LICENSING_OPS}


@pytest.mark.asyncio
async def test_9x_registrar_registers_the_licensing_ops(monkeypatch: pytest.MonkeyPatch) -> None:
    from meho_backplane.connectors.vcf_operations import typed_ops

    register = AsyncMock()
    monkeypatch.setattr(
        "meho_backplane.operations.typed_register.register_typed_operation", register
    )
    await typed_ops.register_vcf_operations_typed_operations(embedding_service=None)

    registered = {call.kwargs["op_id"]: call.kwargs for call in register.await_args_list}
    for op in VROPS_LICENSING_OPS:
        assert registered[op.op_id]["version"] == "9.0"
        assert registered[op.op_id]["when_to_use"]
