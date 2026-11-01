# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""VCF Automation day-2 ops: actions, lease, delete, machines and disks.

Drives each ``vcfa.tenant.*`` day-2 op through the real dispatcher against a
respx-mocked appliance. For every change: it waits for approval, the success
path, and a vendor error (the error text reaches the answer). The delete is
``destructive``: the tests cover its blast-radius preview, the refusal of a
missing deployment, and the refusal without a ``preview_hash``. The handlers
are shared with Aria Automation 8 (``tests/test_connectors_vra8_day2.py``).
All names and ids are made up.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Iterator
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest
import respx
from sqlalchemy import select

from meho_backplane.connectors.vcf_automation import (
    VCFA_CONNECTOR_ID,
    VCFA_IMPL_ID,
    VCFA_PRODUCT,
    VCFA_VERSION,
    VcfAutomationConnector,
    _day2,
    _day2_create,
)
from meho_backplane.connectors.vcf_automation.day2_ops import VCFA_DAY2_OPS
from meho_backplane.connectors.vcf_automation.deploy_ops import VCFA_DEPLOY_OPS
from meho_backplane.connectors.vcf_automation.typed_ops import VCFA_TYPED_OPS
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import EndpointDescriptor
from meho_backplane.operations import (
    PassThroughReducer,
    dispatch,
    reset_dispatcher_caches,
    set_default_reducer,
)
from meho_backplane.operations._handler_resolve import reset_handler_cache
from meho_backplane.operations._preview import _PREVIEW_BUILDERS
from tests._vcfa_provisioning_support import (
    BASE_URL,
    OPERATOR,
    TENANT_TOKEN,
    mount_logins,
    resolved_target,
    run,
    seed_target,
    wire_connector,
)

#: The risk level and approval flag of each op, as the Task table says.
_EXPECTED: dict[str, tuple[str, bool]] = {
    "vcfa.tenant.deployment.actions.list": ("safe", False),
    "vcfa.tenant.deployment.action.run": ("dangerous", True),
    "vcfa.tenant.resource.action.run": ("dangerous", True),
    "vcfa.tenant.deployment.lease.change": ("caution", True),
    "vcfa.tenant.deployment.delete": ("destructive", True),
    "vcfa.tenant.machine.list": ("safe", False),
    "vcfa.tenant.block_device.list": ("safe", False),
    "vcfa.tenant.machine.create": ("dangerous", True),
    "vcfa.tenant.block_device.create": ("dangerous", True),
}

_API_VERSION = "2021-07-15"
_DEPLOYMENT = "/deployment/api/deployments/dep-1"
_MACHINES = "/iaas/api/machines"
_DISKS = "/iaas/api/block-devices"
_REQUEST = {
    "id": "req-1",
    "name": "Power Off",
    "actionId": "Deployment.PowerOff",
    "status": "PENDING",
    "deploymentId": "dep-1",
    "requestedBy": "<user>",
}


@pytest.fixture(autouse=True)
def _env_and_caches(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    from meho_backplane.settings import get_settings

    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.setattr(
        "meho_backplane.operations.typed_register.encode_endpoint_text",
        AsyncMock(return_value=[0.1] * 384),
    )
    monkeypatch.setattr(_day2_create, "DISK_POLL_SECONDS", 0.0)
    get_settings.cache_clear()
    reset_dispatcher_caches()
    reset_handler_cache()
    yield
    reset_dispatcher_caches()
    reset_handler_cache()
    get_settings.cache_clear()


@pytest.fixture
async def vcfa() -> AsyncIterator[VcfAutomationConnector]:
    set_default_reducer(PassThroughReducer())
    await VcfAutomationConnector.register_typed_operations()
    await seed_target()
    connector = wire_connector()
    yield connector
    await connector.aclose()


def _body(route: respx.Route) -> Any:
    return json.loads(route.calls.last.request.content)


# ---------------------------------------------------------------------------
# Risk level and approval
# ---------------------------------------------------------------------------


def test_each_op_has_the_risk_level_and_approval_of_the_table() -> None:
    ops = (*VCFA_DAY2_OPS, *(op for op in VCFA_DEPLOY_OPS if op.op_id.endswith(".delete")))
    assert {op.op_id: (op.safety_level, op.requires_approval) for op in ops} == _EXPECTED


async def test_registered_rows_carry_the_risk_level_and_approval() -> None:
    await VcfAutomationConnector.register_typed_operations()
    async with get_sessionmaker()() as session:
        rows = (
            await session.execute(
                select(EndpointDescriptor).where(
                    EndpointDescriptor.product == VCFA_PRODUCT,
                    EndpointDescriptor.version == VCFA_VERSION,
                    EndpointDescriptor.impl_id == VCFA_IMPL_ID,
                )
            )
        ).scalars()
        by_op = {row.op_id: row for row in rows}
    for op_id, (safety, approval) in _EXPECTED.items():
        assert by_op[op_id].source_kind == "typed", op_id
        assert (by_op[op_id].safety_level, by_op[op_id].requires_approval) == (safety, approval)


def test_the_delete_has_a_preview_builder() -> None:
    assert _PREVIEW_BUILDERS["vcfa.tenant.deployment.delete"] is _day2.deployment_delete_preview


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


async def test_actions_list_of_a_deployment(vcfa: VcfAutomationConnector) -> None:
    actions = [
        {
            "id": "Deployment.PowerOff",
            "name": "PowerOff",
            "displayName": "Power Off",
            "valid": True,
            "actionType": "RESOURCE_ACTION",
            "schema": {"properties": {}},
            "formDefinition": {"big": "form"},
        }
    ]
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        route = mock.get(f"{_DEPLOYMENT}/actions").respond(200, json=actions)
        result = await run("vcfa.tenant.deployment.actions.list", {"deployment_id": "dep-1"})
    assert result["status"] == "ok", result
    items = result["result"]["items"]
    assert [row["id"] for row in items] == ["Deployment.PowerOff"]
    assert "formDefinition" not in items[0]
    assert route.calls.last.request.headers["Authorization"] == f"Bearer {TENANT_TOKEN}"


async def test_actions_list_of_a_resource_quotes_the_id(vcfa: VcfAutomationConnector) -> None:
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        route = mock.get("/deployment/api/resources/m%2F1/actions").respond(200, json=[])
        result = await run("vcfa.tenant.deployment.actions.list", {"resource_id": "m/1"})
    assert result["result"] == {"items": []}, result
    assert route.called


async def test_actions_list_needs_exactly_one_id(vcfa: VcfAutomationConnector) -> None:
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        result = await run(
            "vcfa.tenant.deployment.actions.list", {"deployment_id": "d", "resource_id": "r"}
        )
    assert result["status"] != "ok"
    assert result["extras"]["error_code"] == "invalid_params", result


async def test_machine_list_is_flat_and_reads_every_page(
    vcfa: VcfAutomationConnector, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(_day2, "_PAGE_SIZE", 1)
    monkeypatch.setattr(_day2, "_MAX_PAGES", 2)
    pages = [
        {
            "content": [
                {
                    "id": "m-1",
                    "name": "<vm-a>",
                    "projectId": "p-1",
                    "powerState": "ON",
                    "address": "<address>",
                    "customProperties": {"secret": "x"},
                    "_links": {
                        "cloud-zone": {"href": "/iaas/api/zones/z-1"},
                        "disks": {"hrefs": ["/iaas/api/block-devices/b-1"]},
                    },
                }
            ],
            "totalElements": 2,
        },
        {"content": [{"id": "m-2", "name": "<vm-b>", "powerState": "OFF"}], "totalElements": 2},
    ]
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        route = mock.get(_MACHINES).mock(
            side_effect=[httpx.Response(200, json=page) for page in pages]
        )
        result = await run("vcfa.tenant.machine.list", {"$filter": "projectId eq 'p-1'"})
    items = result["result"]["items"]
    assert [row["id"] for row in items] == ["m-1", "m-2"]
    assert items[0]["zoneIds"] == ["z-1"]
    assert items[0]["diskIds"] == ["b-1"]
    assert "customProperties" not in items[0]
    params = route.calls[1].request.url.params
    assert params["apiVersion"] == _API_VERSION
    assert params["$skip"] == "1"
    assert params["$filter"] == "projectId eq 'p-1'"


async def test_machine_list_refuses_a_list_past_the_page_limit(
    vcfa: VcfAutomationConnector, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Three rows, one a page, two pages at most: an error, not two rows."""
    monkeypatch.setattr(_day2, "_PAGE_SIZE", 1)
    monkeypatch.setattr(_day2, "_MAX_PAGES", 2)

    def page(request: httpx.Request) -> httpx.Response:
        skip = request.url.params["$skip"]
        return httpx.Response(200, json={"content": [{"id": f"m-{skip}"}], "totalElements": 3})

    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        route = mock.get(_MACHINES).mock(side_effect=page)
        result = await run("vcfa.tenant.machine.list", {})
    assert result["status"] == "error", result
    assert result["extras"]["exception_class"] == "IncompleteListError"
    assert "2 of 3" in result["extras"]["exception_message"]
    assert "$filter" in result["extras"]["remediation"]
    assert route.call_count == 2


async def test_block_device_list_names_the_machines(vcfa: VcfAutomationConnector) -> None:
    disk = {
        "id": "b-1",
        "name": "<disk>",
        "capacityInGB": 10,
        "status": "ATTACHED",
        "_links": {"machines": {"hrefs": ["/iaas/api/machines/m-1"]}},
    }
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        mock.get(_DISKS).respond(200, json={"content": [disk], "totalElements": 1})
        result = await run("vcfa.tenant.block_device.list", {})
    row = result["result"]["items"][0]
    assert row["machineIds"] == ["m-1"]
    assert row["capacityInGB"] == 10


# ---------------------------------------------------------------------------
# Day-2 actions
# ---------------------------------------------------------------------------


async def test_deployment_action_runs_a_built_in_action(vcfa: VcfAutomationConnector) -> None:
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        route = mock.post(f"{_DEPLOYMENT}/requests").respond(200, json=_REQUEST)
        result = await run(
            "vcfa.tenant.deployment.action.run",
            {"deployment_id": "dep-1", "action_id": "Deployment.PowerOff", "reason": "<why>"},
        )
    out = result["result"]
    assert out["status"] == "submitted", result
    assert out["request"]["id"] == "req-1"
    assert "vcfa.tenant.machine.list" in out["guidance"]
    assert _body(route) == {"actionId": "Deployment.PowerOff", "inputs": {}, "reason": "<why>"}


@pytest.mark.parametrize(
    ("op_id", "params"),
    [
        (
            "vcfa.tenant.deployment.action.run",
            {"deployment_id": "dep-1", "action_id": "Deployment.Delete"},
        ),
        (
            "vcfa.tenant.deployment.action.run",
            {"deployment_id": "dep-1", "action_id": "Custom.RunMyWorkflow"},
        ),
        (
            "vcfa.tenant.resource.action.run",
            {"resource_id": "m-1", "action_id": "Cloud.vSphere.Machine.Delete"},
        ),
        (
            "vcfa.tenant.resource.action.run",
            {"resource_id": "m-1", "action_id": "Cloud.vSphere.Machine.Remove.Disk"},
        ),
    ],
)
async def test_delete_and_custom_actions_fail_before_dispatch(
    vcfa: VcfAutomationConnector, op_id: str, params: dict[str, Any]
) -> None:
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        writes = mock.post(url__regex=r"/deployment/api/.*/requests").respond(200, json={})
        result = await run(op_id, params)
    assert result["status"] != "ok"
    assert result["extras"]["error_code"] == "invalid_params", result
    assert not writes.called


async def test_the_handler_refuses_an_action_outside_the_list(
    vcfa: VcfAutomationConnector,
) -> None:
    with pytest.raises(ValueError, match="not an allowed"):
        await _day2.deployment_action_run(
            vcfa,
            OPERATOR,
            await resolved_target(),
            {"deployment_id": "dep-1", "action_id": "Deployment.Delete"},
            prefix="vcfa.tenant",
        )


async def test_resource_action_resize_sends_the_inputs(vcfa: VcfAutomationConnector) -> None:
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        route = mock.post("/deployment/api/resources/m-1/requests").respond(200, json=_REQUEST)
        result = await run(
            "vcfa.tenant.resource.action.run",
            {
                "resource_id": "m-1",
                "action_id": "Cloud.vSphere.Machine.Resize",
                "inputs": {"cpuCount": 4, "totalMemoryMB": 8192},
            },
        )
    assert result["result"]["status"] == "submitted", result
    assert _body(route)["inputs"] == {"cpuCount": 4, "totalMemoryMB": 8192}


def test_follow_up_names_only_reads_this_connector_has() -> None:
    """``vcfa.tenant.deployment.get`` reads the IaaS API: no request state, no lease.

    So the guidance after an action or a lease change must not send the agent
    there to wait; it names ``machine.list`` and says what MEHO cannot show.
    """
    deployment_get = next(op for op in VCFA_TYPED_OPS if op.op_id == "vcfa.tenant.deployment.get")
    fields = deployment_get.response_schema["properties"]
    assert "lastRequest" not in fields
    assert "leaseExpireAt" not in fields
    by_id = {op.op_id: op for op in VCFA_DAY2_OPS}
    machine_row = by_id["vcfa.tenant.machine.list"].response_schema["properties"]["items"]["items"]
    assert "powerState" in machine_row["properties"]

    action = by_id["vcfa.tenant.deployment.action.run"].llm_instructions
    resource = by_id["vcfa.tenant.resource.action.run"].llm_instructions
    lease = by_id["vcfa.tenant.deployment.lease.change"].llm_instructions
    request_texts = [
        _day2._request_answer({}, "vcfa.tenant")["guidance"],
        action["next_step"],
        resource["next_step"],
        lease["next_step"],
    ]
    for text in request_texts:
        assert "vcfa.tenant.machine.list" in text, text
        assert "deploymentId eq" in text, text
        assert "10 minutes" in text, text
    for text in [*request_texts, lease["preconditions"]]:
        assert "lastRequest" not in text, text
        assert "leaseExpireAt" not in text, text
        assert "vcfa.vra8" not in text, text
    assert "cannot read the lease end" in lease["preconditions"]
    assert "cannot read the lease end" in lease["next_step"]


async def test_lease_change_sends_the_lease_action(vcfa: VcfAutomationConnector) -> None:
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        route = mock.post(f"{_DEPLOYMENT}/requests").respond(200, json=_REQUEST)
        result = await run(
            "vcfa.tenant.deployment.lease.change",
            {"deployment_id": "dep-1", "lease_expiration_date": "2030-01-31T00:00:00Z"},
        )
    assert result["result"]["status"] == "submitted", result
    assert _body(route) == {
        "actionId": "Deployment.ChangeLease",
        "inputs": {"Lease Expiration Date": "2030-01-31T00:00:00Z"},
    }


_CHANGES: list[tuple[str, dict[str, Any], str, str]] = [
    (
        "vcfa.tenant.deployment.action.run",
        {"deployment_id": "dep-1", "action_id": "Deployment.PowerOn"},
        "POST",
        f"{_DEPLOYMENT}/requests",
    ),
    (
        "vcfa.tenant.resource.action.run",
        {"resource_id": "m-1", "action_id": "Cloud.vSphere.Machine.Reboot"},
        "POST",
        "/deployment/api/resources/m-1/requests",
    ),
    (
        "vcfa.tenant.deployment.lease.change",
        {"deployment_id": "dep-1", "lease_expiration_date": "2030-01-31T00:00:00Z"},
        "POST",
        f"{_DEPLOYMENT}/requests",
    ),
    (
        "vcfa.tenant.block_device.create",
        {"machine_id": "m-1", "block_device_id": "b-1"},
        "POST",
        "/iaas/api/machines/m-1/disks",
    ),
]


@pytest.mark.parametrize(("op_id", "params", "method", "path"), _CHANGES)
async def test_a_change_waits_for_approval(
    vcfa: VcfAutomationConnector, op_id: str, params: dict[str, Any], method: str, path: str
) -> None:
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        write = mock.request(method, path).respond(200, json=_REQUEST)
        result = await run(op_id, params, approved=False)
    assert result["status"] == "awaiting_approval", result
    assert not write.called


@pytest.mark.parametrize(("op_id", "params", "method", "path"), _CHANGES)
async def test_a_vendor_error_reaches_the_answer(
    vcfa: VcfAutomationConnector, op_id: str, params: dict[str, Any], method: str, path: str
) -> None:
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        mock.request(method, path).respond(409, json={"message": "Deployment is locked"})
        result = await run(op_id, params)
    assert result["status"] == "error", result
    assert result["extras"]["http_status"] == 409
    assert "Deployment is locked" in json.dumps(result)


async def test_machine_create_waits_for_approval(vcfa: VcfAutomationConnector) -> None:
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        write = mock.post(_MACHINES).respond(202, json={"id": "t-1", "status": "INPROGRESS"})
        result = await run("vcfa.tenant.machine.create", _MACHINE, approved=False)
    assert result["status"] == "awaiting_approval", result
    assert not write.called


# ---------------------------------------------------------------------------
# Machines and disks
# ---------------------------------------------------------------------------

_MACHINE = {
    "name": "<vm-name>",
    "project_id": "p-1",
    "flavor": "<flavor>",
    "image": "<image>",
    "tags": [{"key": "team", "value": "<team>"}],
}


async def test_machine_create_submits(vcfa: VcfAutomationConnector) -> None:
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        lookup = mock.get(_MACHINES).respond(200, json={"content": []})
        create = mock.post(_MACHINES).respond(
            202, json={"id": "t-1", "status": "INPROGRESS", "progress": 0, "selfLink": "x"}
        )
        result = await run("vcfa.tenant.machine.create", _MACHINE)
    out = result["result"]
    assert out["status"] == "submitted", result
    assert out["request"]["id"] == "t-1"
    assert _body(create) == {
        "name": "<vm-name>",
        "projectId": "p-1",
        "flavor": "<flavor>",
        "image": "<image>",
        "tags": [{"key": "team", "value": "<team>"}],
    }
    assert create.calls.last.request.url.params["apiVersion"] == _API_VERSION
    assert lookup.calls.last.request.url.params["$filter"] == (
        "name eq '<vm-name>' and projectId eq 'p-1'"
    )


async def test_machine_create_twice_is_unchanged(vcfa: VcfAutomationConnector) -> None:
    existing = {"id": "m-1", "name": "<vm-name>", "projectId": "p-1", "powerState": "ON"}
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        mock.get(_MACHINES).respond(200, json={"content": [existing]})
        create = mock.post(_MACHINES).respond(202, json={})
        result = await run("vcfa.tenant.machine.create", _MACHINE)
    assert result["result"]["status"] == "unchanged", result
    assert result["result"]["machine"]["id"] == "m-1"
    assert not create.called


async def test_machine_create_vendor_error(vcfa: VcfAutomationConnector) -> None:
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        mock.get(_MACHINES).respond(200, json={"content": []})
        mock.post(_MACHINES).respond(400, json={"message": "No placement for flavor"})
        result = await run("vcfa.tenant.machine.create", _MACHINE)
    assert result["status"] == "error"
    assert "No placement for flavor" in json.dumps(result)


_NEW_DISK = {"machine_id": "m-1", "name": "<disk>", "project_id": "p-1", "capacity_gb": 10}


async def test_block_device_create_waits_then_attaches(vcfa: VcfAutomationConnector) -> None:
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        create = mock.post(_DISKS).respond(202, json={"id": "t-1", "status": "INPROGRESS"})
        mock.get("/iaas/api/request-tracker/t-1").mock(
            side_effect=[
                httpx.Response(200, json={"id": "t-1", "status": "INPROGRESS"}),
                httpx.Response(
                    200,
                    json={
                        "id": "t-1",
                        "status": "FINISHED",
                        "resources": ["/iaas/api/block-devices/b-9"],
                    },
                ),
            ]
        )
        attach = mock.post("/iaas/api/machines/m-1/disks").respond(
            200, json={"id": "t-2", "status": "INPROGRESS"}
        )
        result = await run("vcfa.tenant.block_device.create", _NEW_DISK)
    out = result["result"]
    assert out["status"] == "submitted", result
    assert out["block_device_id"] == "b-9"
    assert _body(create) == {
        "name": "<disk>",
        "projectId": "p-1",
        "capacityInGB": 10,
        "persistent": False,
    }
    assert _body(attach) == {"blockDeviceId": "b-9"}


async def test_block_device_create_still_running_is_in_progress(
    vcfa: VcfAutomationConnector, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(_day2_create, "DISK_WAIT_SECONDS", 0.0)
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        mock.post(_DISKS).respond(202, json={"id": "t-1", "status": "INPROGRESS"})
        attach = mock.post("/iaas/api/machines/m-1/disks").respond(200, json={})
        result = await run("vcfa.tenant.block_device.create", _NEW_DISK)
    assert result["result"]["status"] == "in_progress", result
    assert not attach.called


async def test_block_device_create_failed(vcfa: VcfAutomationConnector) -> None:
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        mock.post(_DISKS).respond(
            202, json={"id": "t-1", "status": "FAILED", "message": "No storage"}
        )
        attach = mock.post("/iaas/api/machines/m-1/disks").respond(200, json={})
        result = await run("vcfa.tenant.block_device.create", _NEW_DISK)
    out = result["result"]
    assert out["status"] == "failed", result
    assert out["create_request"]["message"] == "No storage"
    assert not attach.called


async def test_block_device_create_needs_a_disk_or_its_size(
    vcfa: VcfAutomationConnector,
) -> None:
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        result = await run("vcfa.tenant.block_device.create", {"machine_id": "m-1"})
    assert result["extras"]["error_code"] == "invalid_params", result


# ---------------------------------------------------------------------------
# Delete a deployment (destructive)
# ---------------------------------------------------------------------------


async def test_deployment_delete_submitted(vcfa: VcfAutomationConnector) -> None:
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        route = mock.delete(_DEPLOYMENT).respond(
            200, json={"id": "r-1", "name": "Delete", "status": "PENDING", "deploymentId": "dep-1"}
        )
        result = await run("vcfa.tenant.deployment.delete", {"id": "dep-1"})
    out = result["result"]
    assert out["status"] == "submitted", result
    assert out["request"]["id"] == "r-1"
    assert "vcfa.tenant.deployment.get" in out["guidance"]
    assert route.called


async def _preview(connector: Any, deployment_id: str = "dep-1") -> dict[str, Any] | None:
    ctx: Any = SimpleNamespace(
        connector_instance=connector,
        operator=OPERATOR,
        target=await resolved_target() if connector is not None else None,
        params={"id": deployment_id},
    )
    return await _day2.deployment_delete_preview(ctx)


async def test_delete_preview_lists_the_blast_radius(vcfa: VcfAutomationConnector) -> None:
    deployment = {
        "id": "dep-1",
        "name": "<deployment>",
        "projectId": "p-1",
        "status": "CREATE_SUCCESSFUL",
        "resources": [
            {"id": "m-1", "name": "<vm>", "type": "Cloud.vSphere.Machine"},
            {"id": "b-1", "name": "<disk>", "type": "Cloud.vSphere.Disk"},
        ],
    }
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        read = mock.get(_DEPLOYMENT).respond(200, json=deployment)
        preview = await _preview(vcfa)
    assert preview is not None
    blast = preview["blast_radius"]
    assert blast["object"] == {
        "kind": "deployment",
        "id": "dep-1",
        "name": "<deployment>",
        "project_id": "p-1",
    }
    assert blast["children"] == [
        {"kind": "Cloud.vSphere.Machine", "id": "m-1", "name": "<vm>"},
        {"kind": "Cloud.vSphere.Disk", "id": "b-1", "name": "<disk>"},
    ]
    assert blast["irreversibility"] == "permanent"
    assert read.calls.last.request.url.params["expand"] == "resources"


async def test_delete_preview_refuses_a_missing_deployment(
    vcfa: VcfAutomationConnector,
) -> None:
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        mock.get(_DEPLOYMENT).respond(404, json={"message": "Not found"})
        with pytest.raises(LookupError, match="dep-1"):
            await _preview(vcfa)


async def test_delete_preview_without_a_connector_names_the_id() -> None:
    assert await _preview(None) == {"action": "delete_deployment", "id": "dep-1"}


async def test_delete_is_refused_without_a_preview_hash(vcfa: VcfAutomationConnector) -> None:
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        delete = mock.delete(_DEPLOYMENT).respond(200, json={})
        result = await dispatch(
            operator=OPERATOR,
            connector_id=VCFA_CONNECTOR_ID,
            op_id="vcfa.tenant.deployment.delete",
            target=await resolved_target(),
            params={"id": "dep-1"},
        )
    assert result.status == "denied", result
    assert result.extras["error_code"] == "preview_binding_required"
    assert not delete.called
