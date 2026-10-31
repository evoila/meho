# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Aria Automation 8 day-2 ops (``vcfa.vra8.*``) on the shared handlers.

The handlers and the metadata are written once for VCF Automation 9 and Aria
Automation 8 (``tests/test_connectors_vcf_automation_day2.py`` covers them in
depth). These tests show that the vRA 8 connector registers all nine ops with
the risk level of the Task table, that each one runs through the vRA 8 login
on the same vendor path, that a change waits for approval, that a vendor error
reaches the answer, and that the delete has its blast-radius preview. All
names and ids are made up.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
import respx
from sqlalchemy import select

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors.registry import clear_registry, register_connector_v2
from meho_backplane.connectors.vcf_automation import _day2
from meho_backplane.connectors.vra8 import (
    VRA8_CONNECTOR_ID,
    VRA8_DAY2_OPS,
    VRA8_IMPL_ID,
    VRA8_PRODUCT,
    VRA8_VERSION,
    Vra8Connector,
    register_vra8_typed_operations,
)
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import EndpointDescriptor
from meho_backplane.operations import dispatch, reset_dispatcher_caches
from meho_backplane.operations._handler_resolve import (
    get_or_create_connector_instance,
    reset_handler_cache,
)
from meho_backplane.operations._preview import _PREVIEW_BUILDERS
from meho_backplane.settings import get_settings

_HOST = "vra-day2.test.invalid"
_BASE_URL = f"https://{_HOST}"

_EXPECTED: dict[str, tuple[str, bool]] = {
    "vcfa.vra8.deployment.actions.list": ("safe", False),
    "vcfa.vra8.deployment.action.run": ("dangerous", True),
    "vcfa.vra8.resource.action.run": ("dangerous", True),
    "vcfa.vra8.deployment.lease.change": ("caution", True),
    "vcfa.vra8.deployment.delete": ("destructive", True),
    "vcfa.vra8.machine.list": ("safe", False),
    "vcfa.vra8.block_device.list": ("safe", False),
    "vcfa.vra8.machine.create": ("dangerous", True),
    "vcfa.vra8.block_device.create": ("dangerous", True),
}

# (op_id, params, method, vendor path, vendor answer)
_CASES: list[tuple[str, dict[str, Any], str, str, Any]] = [
    (
        "vcfa.vra8.deployment.actions.list",
        {"deployment_id": "dep-1"},
        "GET",
        "/deployment/api/deployments/dep-1/actions",
        [{"id": "Deployment.PowerOn", "valid": True}],
    ),
    (
        "vcfa.vra8.deployment.action.run",
        {"deployment_id": "dep-1", "action_id": "Deployment.EditTags", "inputs": {}},
        "POST",
        "/deployment/api/deployments/dep-1/requests",
        {"id": "req-1", "status": "PENDING"},
    ),
    (
        "vcfa.vra8.resource.action.run",
        {"resource_id": "m-1", "action_id": "Cloud.vSphere.Machine.Snapshot.Create"},
        "POST",
        "/deployment/api/resources/m-1/requests",
        {"id": "req-1", "status": "PENDING"},
    ),
    (
        "vcfa.vra8.deployment.lease.change",
        {"deployment_id": "dep-1", "lease_expiration_date": "2030-01-31T00:00:00Z"},
        "POST",
        "/deployment/api/deployments/dep-1/requests",
        {"id": "req-1", "status": "PENDING"},
    ),
    (
        "vcfa.vra8.deployment.delete",
        {"id": "dep-1"},
        "DELETE",
        "/deployment/api/deployments/dep-1",
        {"id": "req-1", "status": "PENDING", "deploymentId": "dep-1"},
    ),
    (
        "vcfa.vra8.machine.list",
        {},
        "GET",
        "/iaas/api/machines",
        {"content": [{"id": "m-1", "name": "<vm>", "powerState": "ON"}], "totalElements": 1},
    ),
    (
        "vcfa.vra8.block_device.list",
        {},
        "GET",
        "/iaas/api/block-devices",
        {"content": [{"id": "b-1", "name": "<disk>"}], "totalElements": 1},
    ),
    (
        "vcfa.vra8.block_device.create",
        {"machine_id": "m-1", "block_device_id": "b-1"},
        "POST",
        "/iaas/api/machines/m-1/disks",
        {"id": "t-1", "status": "INPROGRESS"},
    ),
]


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.setattr(
        "meho_backplane.operations.typed_register.encode_endpoint_text",
        AsyncMock(return_value=[0.1] * 384),
    )
    get_settings.cache_clear()
    reset_dispatcher_caches()
    reset_handler_cache()
    clear_registry()
    register_connector_v2(
        product=VRA8_PRODUCT, version=VRA8_VERSION, impl_id=VRA8_IMPL_ID, cls=Vra8Connector
    )
    yield
    reset_dispatcher_caches()
    reset_handler_cache()
    clear_registry()
    get_settings.cache_clear()


class _Target:
    """A vRA 8.18 target (in the ``vcfa-vra8`` band ``>=8.0,<9.0``)."""

    def __init__(self) -> None:
        self.product = VRA8_PRODUCT
        self.fingerprint = type("_FP", (), {"version": "8.18"})()
        self.preferred_impl_id: str | None = None
        self.id: UUID = uuid.uuid4()
        self.tenant_id: UUID = UUID("00000000-0000-0000-0000-0000000044a6")
        self.name = "vra-day2"
        self.host = _HOST
        self.port = 443
        self.secret_ref = "targets/vra-day2"
        self.auth_model = "shared_service_account"
        self.extras: dict[str, Any] = {}


_OPERATOR = Operator(
    sub="vra-day2-test",
    name="vRA Day-2 Test",
    email=None,
    raw_jwt="<vra-day2-jwt>",
    tenant_id=UUID("00000000-0000-0000-0000-0000000044a6"),
    tenant_role=TenantRole.TENANT_ADMIN,
)


async def _loader(_target: object, _operator: Operator) -> dict[str, str]:
    return {"username": "<user>", "password": "<password>"}


async def _connector() -> Vra8Connector:
    await register_vra8_typed_operations()
    instance = get_or_create_connector_instance(Vra8Connector)
    assert isinstance(instance, Vra8Connector)
    instance._credentials_loader = _loader
    return instance


def _mount_login(mock: respx.MockRouter) -> None:
    mock.post("/csp/gateway/am/api/login").respond(200, json={"refresh_token": "<rt>"})
    mock.post("/iaas/api/login").respond(200, json={"token": "vra-bearer"})


async def _run(op_id: str, params: dict[str, Any], *, approved: bool = True) -> Any:
    return await dispatch(
        operator=_OPERATOR,
        connector_id=VRA8_CONNECTOR_ID,
        op_id=op_id,
        target=_Target(),
        params=params,
        _approved=approved,
    )


def test_each_op_has_the_risk_level_and_approval_of_the_table() -> None:
    ops = {op.op_id: (op.safety_level, op.requires_approval) for op in VRA8_DAY2_OPS}
    assert ops == _EXPECTED
    assert {op.group_key for op in VRA8_DAY2_OPS} == {"vcfa-vra8-day2"}


async def test_registered_rows_carry_the_risk_level_and_approval() -> None:
    await _connector()
    async with get_sessionmaker()() as session:
        rows = (
            await session.execute(
                select(EndpointDescriptor).where(EndpointDescriptor.impl_id == VRA8_IMPL_ID)
            )
        ).scalars()
        by_op = {row.op_id: row for row in rows}
    for op_id, (safety, approval) in _EXPECTED.items():
        assert by_op[op_id].source_kind == "typed", op_id
        assert (by_op[op_id].safety_level, by_op[op_id].requires_approval) == (safety, approval)


@pytest.mark.parametrize(("op_id", "params", "method", "path", "answer"), _CASES)
async def test_each_op_runs_on_the_vra8_login(
    op_id: str, params: dict[str, Any], method: str, path: str, answer: Any
) -> None:
    await _connector()
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        _mount_login(mock)
        route = mock.request(method, path).respond(200, json=answer)
        result = await _run(op_id, params)
    assert result.status == "ok", result.error
    assert route.called
    assert route.calls.last.request.headers["Authorization"] == "Bearer vra-bearer"
    assert "vcfa.tenant." not in json.dumps(result.result)


async def test_machine_create_runs_on_the_vra8_login() -> None:
    await _connector()
    params = {"name": "<vm>", "project_id": "p-1", "flavor": "<flavor>", "image": "<image>"}
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        _mount_login(mock)
        mock.get("/iaas/api/machines").respond(200, json={"content": []})
        create = mock.post("/iaas/api/machines").respond(
            202, json={"id": "t-1", "status": "INPROGRESS"}
        )
        result = await _run("vcfa.vra8.machine.create", params)
    assert result.status == "ok", result.error
    assert result.result["status"] == "submitted"
    assert "vcfa.vra8.machine.list" in result.result["guidance"]
    assert create.called


async def test_a_change_waits_for_approval() -> None:
    await _connector()
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        _mount_login(mock)
        write = mock.post("/deployment/api/deployments/dep-1/requests").respond(200, json={})
        result = await _run(
            "vcfa.vra8.deployment.lease.change",
            {"deployment_id": "dep-1", "lease_expiration_date": "2030-01-31T00:00:00Z"},
            approved=False,
        )
    assert result.status == "awaiting_approval", result
    assert not write.called


async def test_a_vendor_error_reaches_the_answer() -> None:
    await _connector()
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        _mount_login(mock)
        mock.post("/deployment/api/resources/m-1/requests").respond(
            400, json={"message": "Action is not valid now"}
        )
        result = await _run(
            "vcfa.vra8.resource.action.run",
            {"resource_id": "m-1", "action_id": "Cloud.vSphere.Machine.PowerOff"},
        )
    assert result.status == "error"
    assert "Action is not valid now" in json.dumps(result.model_dump(mode="json"))


async def test_a_custom_action_fails_before_dispatch() -> None:
    await _connector()
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        _mount_login(mock)
        write = mock.post("/deployment/api/deployments/dep-1/requests").respond(200, json={})
        result = await _run(
            "vcfa.vra8.deployment.action.run",
            {"deployment_id": "dep-1", "action_id": "Custom.RunAnything"},
        )
    assert result.extras["error_code"] == "invalid_params", result
    assert not write.called


async def test_the_delete_has_a_blast_radius_preview() -> None:
    assert _PREVIEW_BUILDERS["vcfa.vra8.deployment.delete"] is _day2.deployment_delete_preview
    connector = await _connector()
    ctx: Any = SimpleNamespace(
        connector_instance=connector, operator=_OPERATOR, target=_Target(), params={"id": "dep-1"}
    )
    deployment = {
        "id": "dep-1",
        "name": "<deployment>",
        "projectId": "p-1",
        "resources": [{"id": "m-1", "name": "<vm>", "type": "Cloud.vSphere.Machine"}],
    }
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        _mount_login(mock)
        mock.get("/deployment/api/deployments/dep-1").respond(200, json=deployment)
        preview = await _day2.deployment_delete_preview(ctx)
    assert preview is not None
    assert preview["blast_radius"]["children"] == [
        {"kind": "Cloud.vSphere.Machine", "id": "m-1", "name": "<vm>"}
    ]


async def test_the_delete_is_refused_without_a_preview_hash() -> None:
    await _connector()
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        _mount_login(mock)
        delete = mock.delete("/deployment/api/deployments/dep-1").respond(200, json={})
        result = await _run("vcfa.vra8.deployment.delete", {"id": "dep-1"}, approved=False)
    assert result.status == "denied", result
    assert result.extras["error_code"] == "preview_binding_required"
    assert not delete.called
