# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for the Azure compute reads: VMs, disks, images, scale sets and VM sizes.

The dispatch tests run the real dispatcher (policy, audit, parameter check),
the real paging helper, the login cache and, where a stored result matters,
the real result reduction with an in-memory result store. Microsoft's login
service and Azure Resource Manager are mocked with respx; the answers have
the shape Microsoft documents. Every id, name and secret here is made up.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Callable, Iterator
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest
import respx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import meho_backplane.operations.result_query as result_query_module
from meho_backplane.connectors.azure import (
    AZURE_CONNECTOR_ID,
    AZURE_IMPL_ID,
    AZURE_PRODUCT,
    AZURE_TYPED_OPS,
    AZURE_TYPED_WHEN_TO_USE_BY_GROUP,
    AZURE_VERSION,
    AzureConnector,
    _paging,
    compute,
    register_azure_typed_operations,
)
from meho_backplane.connectors.azure.compute_reads import vm_show_impl
from meho_backplane.connectors.azure.compute_rows import size_row, vm_detail
from meho_backplane.connectors.registry import clear_registry, register_connector_v2
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import EndpointDescriptor, OperationGroup
from meho_backplane.delete_names import write_word_in_op_id
from meho_backplane.jsonflux.query.contract import ResultQuerySpec
from meho_backplane.operations import dispatch, reset_dispatcher_caches, set_default_reducer
from meho_backplane.operations._handler_resolve import (
    get_or_create_connector_instance,
    reset_handler_cache,
)
from meho_backplane.operations.jsonflux_reducer import JsonFluxReducer
from meho_backplane.settings import get_settings
from tests._azure_fixtures import (
    ARM_BASE,
    SUBSCRIPTION_A,
    AzureTargetStub,
    CountingLoader,
    login_token,
    operator,
    token_answer,
    token_url,
)
from tests._result_store_helpers import MemoryResultStore

GROUP = "rg-example"
COMPUTE_URL = f"{ARM_BASE}/subscriptions/{SUBSCRIPTION_A}/providers/Microsoft.Compute"
GROUP_COMPUTE_URL = (
    f"{ARM_BASE}/subscriptions/{SUBSCRIPTION_A}/resourceGroups/{GROUP}/providers/Microsoft.Compute"
)
VMS_URL = f"{COMPUTE_URL}/virtualMachines"
COMPUTE_OP_IDS = (
    "azure.vm.list",
    "azure.vm.show",
    "azure.disk.list",
    "azure.image.list",
    "azure.vmss.list",
    "azure.vm.size.list",
)


# ---------------------------------------------------------------------------
# Azure answers (made-up names and ids)
# ---------------------------------------------------------------------------


def _resource_id(kind: str, name: str, group: str = GROUP) -> str:
    return (
        f"/subscriptions/{SUBSCRIPTION_A}/resourceGroups/{group}/providers/Microsoft.Compute/"
        f"{kind}/{name}"
    )


def _vm_id(name: str, group: str = GROUP) -> str:
    return _resource_id("virtualMachines", name, group)


def _disk_id(name: str, group: str = GROUP) -> str:
    return _resource_id("disks", name, group)


def _nic_id(name: str) -> str:
    return (
        f"/subscriptions/{SUBSCRIPTION_A}/resourceGroups/{GROUP}/providers/Microsoft.Network/"
        f"networkInterfaces/{name}"
    )


def vm_answer(name: str, *, group: str = GROUP, data_disks: int = 1) -> dict[str, Any]:
    """One ``VirtualMachine`` as the VM list returns it."""
    return {
        "id": _vm_id(name, group),
        "name": name,
        "type": "Microsoft.Compute/virtualMachines",
        "location": "westeurope",
        "zones": ["1"],
        "tags": {"owner": "team-example", "bad": 7},
        "properties": {
            "vmId": "33333333-0000-0000-0000-00000000aa01",
            "hardwareProfile": {"vmSize": "Standard_D2s_v5"},
            "storageProfile": {
                "imageReference": {"publisher": "example", "offer": "example-os", "sku": "1"},
                "osDisk": {
                    "osType": "Linux",
                    "name": f"{name}-os",
                    "createOption": "FromImage",
                    "caching": "ReadWrite",
                    "managedDisk": {
                        "storageAccountType": "Premium_LRS",
                        "id": _disk_id(f"{name}-os", group),
                    },
                    "diskSizeGB": 30,
                },
                "dataDisks": [
                    {
                        "lun": lun,
                        "name": f"{name}-data{lun}",
                        "createOption": "Empty",
                        "caching": "None",
                        "managedDisk": {
                            "storageAccountType": "StandardSSD_LRS",
                            "id": _disk_id(f"{name}-data{lun}", group),
                        },
                        "diskSizeGB": 128,
                    }
                    for lun in range(data_disks)
                ],
            },
            "osProfile": {
                "computerName": name,
                "adminUsername": "example-admin-user",
                "customData": "ZXhhbXBsZS1jdXN0b20tZGF0YQ==",
                "secrets": [],
            },
            "networkProfile": {
                "networkInterfaces": [
                    {"id": _nic_id(f"{name}-nic"), "properties": {"primary": True}}
                ]
            },
            "provisioningState": "Succeeded",
        },
    }


def _statuses(power: str | None) -> list[dict[str, Any]]:
    statuses: list[dict[str, Any]] = [
        {
            "code": "ProvisioningState/succeeded",
            "level": "Info",
            "displayStatus": "Provisioning succeeded",
            "time": "2026-10-01T08:00:00+00:00",
        }
    ]
    if power is not None:
        statuses.append(
            {"code": f"PowerState/{power}", "level": "Info", "displayStatus": f"VM {power}"}
        )
    return statuses


def status_answer(
    name: str, power: str | None = "running", *, group: str = GROUP
) -> dict[str, Any]:
    """One VM as the list with ``statusOnly=true`` returns it: the instance view only."""
    return {
        "id": _vm_id(name, group),
        "name": name,
        "type": "Microsoft.Compute/virtualMachines",
        "location": "westeurope",
        "properties": {"instanceView": {"statuses": _statuses(power)}},
    }


def disk_answer(
    name: str, *, state: str = "Attached", owner: str | None = "vm-a"
) -> dict[str, Any]:
    """One managed ``Disk``. Azure leaves ``managedBy`` out for a disk without a VM."""
    disk: dict[str, Any] = {
        "id": _disk_id(name),
        "name": name,
        "type": "Microsoft.Compute/disks",
        "location": "westeurope",
        "sku": {"name": "Premium_LRS", "tier": "Premium"},
        "zones": ["1"],
        "properties": {
            "creationData": {"createOption": "Empty"},
            "diskSizeGB": 128,
            "diskSizeBytes": 137438953472,
            "encryption": {"type": "EncryptionAtRestWithPlatformKey"},
            "provisioningState": "Succeeded",
            "diskState": state,
            "tier": "P10",
        },
    }
    if owner is not None:
        disk["managedBy"] = _vm_id(owner)
    return disk


def image_answer(name: str) -> dict[str, Any]:
    """One managed ``Image``."""
    return {
        "id": _resource_id("images", name),
        "name": name,
        "type": "Microsoft.Compute/images",
        "location": "westeurope",
        "tags": {"purpose": "example"},
        "properties": {
            "sourceVirtualMachine": {"id": _vm_id("vm-a")},
            "storageProfile": {
                "osDisk": {
                    "osType": "Linux",
                    "osState": "Generalized",
                    "blobUri": "https://example.blob.core.windows.net/vhds/example.vhd",
                    "caching": "ReadWrite",
                    "diskSizeGB": 30,
                },
                "dataDisks": [{"lun": 0, "diskSizeGB": 64}, {"lun": 1, "diskSizeGB": 64}],
                "zoneResilient": False,
            },
            "provisioningState": "Succeeded",
            "hyperVGeneration": "V2",
        },
    }


def scale_set_answer(name: str) -> dict[str, Any]:
    """One ``VirtualMachineScaleSet``."""
    return {
        "id": _resource_id("virtualMachineScaleSets", name),
        "name": name,
        "type": "Microsoft.Compute/virtualMachineScaleSets",
        "location": "westeurope",
        "sku": {"name": "Standard_D2s_v5", "tier": "Standard", "capacity": 3},
        "zones": ["1", "2"],
        "properties": {
            "orchestrationMode": "Uniform",
            "upgradePolicy": {"mode": "Manual"},
            "virtualMachineProfile": {
                "osProfile": {"computerNamePrefix": name, "adminUsername": "example-admin-user"},
                "storageProfile": {"osDisk": {"osType": "Windows", "createOption": "FromImage"}},
            },
            "provisioningState": "Succeeded",
        },
    }


def size_answer(
    name: str,
    *,
    vcpus: int = 2,
    memory: str = "8",
    restrictions: tuple[dict[str, Any], ...] = (),
    architecture: str = "x64",
) -> dict[str, Any]:
    """One VM size in the Resource SKUs answer. Azure sends capability values as text."""
    return {
        "resourceType": "virtualMachines",
        "name": name,
        "tier": "Standard",
        "size": name.removeprefix("Standard_"),
        "family": "standardDSv5Family",
        "locations": ["westeurope"],
        "locationInfo": [{"location": "westeurope", "zones": ["1", "2", "3"], "zoneDetails": []}],
        "capabilities": [
            {"name": "MaxResourceVolumeMB", "value": "0"},
            {"name": "vCPUs", "value": str(vcpus)},
            {"name": "HyperVGenerations", "value": "V1,V2"},
            {"name": "MemoryGB", "value": memory},
            {"name": "MaxDataDiskCount", "value": "4"},
            {"name": "CpuArchitectureType", "value": architecture},
            {"name": "PremiumIO", "value": "True"},
            {"name": "vCPUsAvailable", "value": str(vcpus)},
            {"name": "ACUs", "value": "195"},
            {"name": "vCPUsPerCore", "value": "2"},
        ],
        "restrictions": list(restrictions),
    }


LOCATION_RESTRICTION: dict[str, Any] = {
    "type": "Location",
    "values": ["westeurope"],
    "restrictionInfo": {"locations": ["westeurope"]},
    "reasonCode": "NotAvailableForSubscription",
}
ZONE_RESTRICTION: dict[str, Any] = {
    "type": "Zone",
    "values": ["westeurope"],
    "restrictionInfo": {"locations": ["westeurope"], "zones": ["2"]},
    "reasonCode": "NotAvailableForSubscription",
}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _azure_only_registry() -> Iterator[None]:
    reset_dispatcher_caches()
    reset_handler_cache()
    clear_registry()
    register_connector_v2(
        product=AZURE_PRODUCT, version=AZURE_VERSION, impl_id=AZURE_IMPL_ID, cls=AzureConnector
    )
    register_connector_v2(product=AZURE_PRODUCT, version="", impl_id="", cls=AzureConnector)
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
    monkeypatch.setattr("meho_backplane.operations._search.get_embedding_service", lambda: service)
    return service


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    async with get_sessionmaker()() as s:
        yield s


@pytest.fixture
async def connector(_stub_embedding: AsyncMock, session: AsyncSession) -> AzureConnector:
    """Run the Azure registrar and give the dispatcher's instance a stub login loader."""
    del session
    await register_azure_typed_operations()
    instance = get_or_create_connector_instance(AzureConnector)
    assert isinstance(instance, AzureConnector)
    instance._credentials_loader = CountingLoader()
    return instance


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch) -> MemoryResultStore:
    """The real result reduction over an in-memory store, also used by result_query."""
    memory = MemoryResultStore()
    set_default_reducer(JsonFluxReducer(store=memory))
    monkeypatch.setattr(result_query_module, "get_result_handle_store", lambda: memory)
    return memory


async def _call(op_id: str, params: dict[str, Any]) -> Any:
    return await dispatch(
        operator=operator(),
        connector_id=AZURE_CONNECTOR_ID,
        op_id=op_id,
        target=AzureTargetStub(),
        params=params,
    )


def _pages(*pages: dict[str, Any]) -> list[httpx.Response]:
    return [httpx.Response(200, json=page) for page in pages]


def _vm_lists(
    model_pages: list[dict[str, Any]], status_pages: list[dict[str, Any]]
) -> Callable[[httpx.Request], httpx.Response]:
    """Answer the subscription's VM list: the model pages, or the status pages for statusOnly."""
    model = iter(model_pages)
    status = iter(status_pages)

    def answer(request: httpx.Request) -> httpx.Response:
        pages = status if request.url.params.get("statusOnly") == "true" else model
        return httpx.Response(200, json=next(pages))

    return answer


async def _query(result: Any, spec: dict[str, Any]) -> dict[str, Any]:
    return await result_query_module.run_result_query(
        operator(), result.handle.handle_id, ResultQuerySpec.model_validate(spec)
    )


# ---------------------------------------------------------------------------
# azure.vm.list
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_vm_list_joins_each_vm_with_its_power_state(connector: AzureConnector) -> None:
    del connector
    vms = {"value": [vm_answer("vm-a", data_disks=2), vm_answer("vm-b"), vm_answer("vm-c")]}
    # Azure does not keep the case of a resource group the same in every answer.
    states = {
        "value": [
            status_answer("vm-a", "running", group=GROUP.upper()),
            status_answer("vm-b", "deallocated"),
            status_answer("vm-c", None),
        ]
    }

    with respx.mock() as mock:
        mock.post(token_url()).respond(200, json=token_answer(login_token()))
        arm = mock.get(VMS_URL).mock(side_effect=_vm_lists([vms], [states]))
        result = await _call("azure.vm.list", {})

    assert result.status == "ok", result.error
    assert arm.call_count == 2
    first, second = (call.request.url.params for call in arm.calls)
    assert first["api-version"] == "2026-04-01" and "statusOnly" not in first
    assert second["api-version"] == "2026-04-01" and second["statusOnly"] == "true"
    rows = result.result["value"]
    assert rows[0] == {
        "name": "vm-a",
        "id": _vm_id("vm-a"),
        "resource_group": GROUP,
        "location": "westeurope",
        "zones": ["1"],
        "size": "Standard_D2s_v5",
        "os_type": "Linux",
        "power_state": "running",
        "provisioning_state": "Succeeded",
        "os_disk_id": _disk_id("vm-a-os"),
        "data_disk_ids": [_disk_id("vm-a-data0"), _disk_id("vm-a-data1")],
        "tags": {"owner": "team-example"},
    }
    assert [row["power_state"] for row in rows] == ["running", "deallocated", None]
    assert result.result["power_state_read"] == {
        "vms_without_state": 1,
        "pages_read": 1,
        "truncated": False,
        "omission": None,
        "omission_kind": None,
    }
    text = json.dumps(result.model_dump(mode="json"))
    assert "example-admin-user" not in text
    assert "ZXhhbXBsZS1jdXN0b20tZGF0YQ==" not in text
    assert login_token() not in text


@pytest.mark.asyncio
async def test_vm_list_of_a_resource_group_takes_the_states_from_the_subscription(
    connector: AzureConnector,
) -> None:
    del connector
    states = {"value": [status_answer("vm-a", "stopped"), status_answer("vm-x", "running")]}

    with respx.mock() as mock:
        mock.post(token_url()).respond(200, json=token_answer(login_token()))
        in_group = mock.get(f"{GROUP_COMPUTE_URL}/virtualMachines").respond(
            200, json={"value": [vm_answer("vm-a")]}
        )
        whole = mock.get(VMS_URL).respond(200, json=states)
        result = await _call("azure.vm.list", {"resource_group": GROUP})

    assert result.status == "ok", result.error
    assert in_group.call_count == 1
    assert whole.call_count == 1
    assert whole.calls[0].request.url.params["statusOnly"] == "true"
    assert [(row["name"], row["power_state"]) for row in result.result["value"]] == [
        ("vm-a", "stopped")
    ]


@pytest.mark.asyncio
async def test_vm_list_without_vms_makes_one_call(connector: AzureConnector) -> None:
    del connector
    with respx.mock() as mock:
        mock.post(token_url()).respond(200, json=token_answer(login_token()))
        arm = mock.get(VMS_URL).respond(200, json={"value": []})
        result = await _call("azure.vm.list", {})

    assert result.status == "ok", result.error
    assert arm.call_count == 1
    assert result.result["value"] == []
    assert result.result["power_state_read"]["pages_read"] == 0
    assert result.result["power_state_read"]["vms_without_state"] == 0


@pytest.mark.asyncio
async def test_vm_list_fails_with_azures_error_when_the_status_read_fails(
    connector: AzureConnector,
) -> None:
    del connector
    vms = {"value": [vm_answer("vm-a")]}
    refused = httpx.Response(
        403, json={"error": {"code": "AuthorizationFailed", "message": "No access."}}
    )

    def answer(request: httpx.Request) -> httpx.Response:
        if request.url.params.get("statusOnly") == "true":
            return refused
        return httpx.Response(200, json=vms)

    with respx.mock() as mock:
        mock.post(token_url()).respond(200, json=token_answer(login_token()))
        arm = mock.get(VMS_URL).mock(side_effect=answer)
        result = await _call("azure.vm.list", {})

    assert result.status == "error"
    assert result.extras["exception_class"] == "AzureForbiddenError"
    assert arm.call_count == 2


@pytest.mark.asyncio
async def test_vm_list_over_50_rows_keeps_the_power_state_block_with_the_stored_result(
    connector: AzureConnector, store: MemoryResultStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """60 VMs go behind a stored-result link; the power_state_read object stays inline.

    The status read stops at its time limit after one page (the clock jumps),
    so 20 VMs have no power state, and the block says so next to the link.
    """
    del connector
    names = [f"vm-{n:02d}" for n in range(60)]
    vms = {"value": [vm_answer(name) for name in names]}
    first_states = {
        "value": [status_answer(name) for name in names[:40]],
        "nextLink": f"{VMS_URL}?api-version=2026-04-01&statusOnly=true&$skiptoken=states2",
    }
    # The clock reads 0 when each list starts and 500 seconds later when the
    # status read asks for its second page.
    readings = iter([0.0, 0.0])
    monkeypatch.setattr(_paging, "_monotonic", lambda: next(readings, 500.0))

    with respx.mock() as mock:
        mock.post(token_url()).respond(200, json=token_answer(login_token()))
        arm = mock.get(VMS_URL).mock(side_effect=_vm_lists([vms], [first_states]))
        result = await _call("azure.vm.list", {})

    assert result.status == "ok", result.error
    assert arm.call_count == 2
    assert result.handle is not None
    summary = result.result
    assert summary["row_count"] == 60
    assert summary["truncated"] is False
    assert summary["pages_read"] == 1
    block = summary["power_state_read"]
    assert block["vms_without_state"] == 20
    assert block["pages_read"] == 1
    assert block["truncated"] is True
    assert block["omission_kind"] == "time_limit"
    assert "MEHO stopped at its limit of 120 seconds" in block["omission"]

    last = await _query(result, {"filter": [{"field": "name", "op": "=", "value": "vm-59"}]})
    assert [(row["name"], row["power_state"]) for row in last["rows"]] == [("vm-59", None)]
    stored = await _query(result, {"path": "/power_state_read"})
    assert "vms_without_state" in json.dumps(stored)


@pytest.mark.asyncio
async def test_vm_list_refuses_a_resource_group_that_changes_the_path(
    connector: AzureConnector,
) -> None:
    del connector
    with respx.mock(assert_all_called=False) as mock:
        arm = mock.get(url__startswith=ARM_BASE).respond(200, json={"value": []})
        result = await _call("azure.vm.list", {"resource_group": "../providers"})

    assert result.status == "error"
    assert result.extras["error_code"] == "invalid_params"
    assert not arm.called


# ---------------------------------------------------------------------------
# azure.vm.show
# ---------------------------------------------------------------------------


def _vm_with_view(name: str) -> dict[str, Any]:
    vm = vm_answer(name, data_disks=2)
    vm["properties"]["availabilitySet"] = {"id": _resource_id("availabilitySets", "as-example")}
    vm["properties"]["instanceView"] = {
        "computerName": f"{name}-host",
        "osName": "exampleos",
        "osVersion": "1.2",
        "hyperVGeneration": "V2",
        "vmAgent": {"vmAgentVersion": "2.0", "statuses": []},
        "statuses": _statuses("deallocated"),
    }
    return vm


@pytest.mark.asyncio
async def test_vm_show_reads_one_vm_with_its_live_state(connector: AzureConnector) -> None:
    del connector
    with respx.mock() as mock:
        mock.post(token_url()).respond(200, json=token_answer(login_token()))
        arm = mock.get(f"{GROUP_COMPUTE_URL}/virtualMachines/vm-a").respond(
            200, json=_vm_with_view("vm-a")
        )
        result = await _call("azure.vm.show", {"resource_group": GROUP, "name": "vm-a"})

    assert result.status == "ok", result.error
    assert arm.call_count == 1
    params = arm.calls[0].request.url.params
    assert params["api-version"] == "2026-04-01"
    assert params["$expand"] == "instanceView"
    assert result.handle is None
    assert result.result == {
        "name": "vm-a",
        "id": _vm_id("vm-a"),
        "resource_group": GROUP,
        "location": "westeurope",
        "vm_id": "33333333-0000-0000-0000-00000000aa01",
        "zones": ["1"],
        "size": "Standard_D2s_v5",
        "os_type": "Linux",
        "os_name": "exampleos",
        "os_version": "1.2",
        "computer_name": "vm-a-host",
        "hyperv_generation": "V2",
        "power_state": "deallocated",
        "provisioning_state": "Succeeded",
        "os_disk": {
            "name": "vm-a-os",
            "id": _disk_id("vm-a-os"),
            "size_gb": 30,
            "sku": "Premium_LRS",
            "caching": "ReadWrite",
        },
        "data_disks": [
            {
                "lun": lun,
                "name": f"vm-a-data{lun}",
                "id": _disk_id(f"vm-a-data{lun}"),
                "size_gb": 128,
                "sku": "StandardSSD_LRS",
                "caching": "None",
            }
            for lun in range(2)
        ],
        "network_interfaces": [{"id": _nic_id("vm-a-nic"), "primary": True}],
        "availability_set_id": _resource_id("availabilitySets", "as-example"),
        "scale_set_id": None,
        "tags": {"owner": "team-example"},
    }
    text = json.dumps(result.result)
    assert "example-admin-user" not in text
    assert "ZXhhbXBsZS1jdXN0b20tZGF0YQ==" not in text


@pytest.mark.asyncio
async def test_vm_show_answers_not_found_for_an_unknown_vm(connector: AzureConnector) -> None:
    del connector
    error = {"error": {"code": "ResourceNotFound", "message": "The resource was not found."}}
    with respx.mock() as mock:
        mock.post(token_url()).respond(200, json=token_answer(login_token()))
        mock.get(f"{GROUP_COMPUTE_URL}/virtualMachines/vm-gone").respond(404, json=error)
        result = await _call("azure.vm.show", {"resource_group": GROUP, "name": "vm-gone"})

    assert result.status == "not_found"


@pytest.mark.asyncio
async def test_vm_show_keeps_each_name_in_one_path_segment(connector: AzureConnector) -> None:
    del connector
    with respx.mock() as mock:
        mock.post(token_url()).respond(200, json=token_answer(login_token()))
        arm = mock.get(url__startswith=ARM_BASE).respond(200, json=_vm_with_view("vm-a"))
        result = await _call("azure.vm.show", {"resource_group": "rg(example)é", "name": "vm-a"})

    assert result.status == "ok", result.error
    raw_path = arm.calls[0].request.url.raw_path.decode()
    expected = (
        "/resourceGroups/rg%28example%29%C3%A9/providers/Microsoft.Compute/virtualMachines/vm-a?"
    )
    assert expected in raw_path


@pytest.mark.parametrize(
    "params",
    [
        {"resource_group": GROUP, "name": "a/b"},
        {"resource_group": "..", "name": "vm-a"},
        {"resource_group": GROUP, "name": "vm?x=1"},
        {"resource_group": GROUP, "name": "vm#x"},
        {"resource_group": GROUP, "name": "vm%2Fx"},
        {"resource_group": GROUP},
    ],
)
@pytest.mark.asyncio
async def test_vm_show_refuses_names_that_change_the_path(
    connector: AzureConnector, params: dict[str, Any]
) -> None:
    del connector
    with respx.mock(assert_all_called=False) as mock:
        arm = mock.get(url__startswith=ARM_BASE).respond(200, json={})
        result = await _call("azure.vm.show", params)

    assert result.status == "error"
    assert result.extras["error_code"] == "invalid_params"
    assert not arm.called


@pytest.mark.asyncio
async def test_the_body_checks_the_names_again_without_repeating_them() -> None:
    """The schema checks first; the body checks again and never echoes the value."""
    with pytest.raises(ValueError) as caught:
        await vm_show_impl(
            AzureConnector(credentials_loader=CountingLoader()),
            operator(),
            AzureTargetStub(),
            {"resource_group": "fake-secret/..", "name": "vm-a"},
        )
    assert "resource_group" in str(caught.value)
    assert "fake-secret" not in str(caught.value)


# ---------------------------------------------------------------------------
# azure.disk.list, azure.image.list, azure.vmss.list
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_disk_list_shows_state_owner_and_encryption(connector: AzureConnector) -> None:
    del connector
    page = {
        "value": [
            disk_answer("disk-a"),
            disk_answer("disk-free", state="Unattached", owner=None),
            "not-a-disk",
        ]
    }
    with respx.mock() as mock:
        mock.post(token_url()).respond(200, json=token_answer(login_token()))
        arm = mock.get(f"{COMPUTE_URL}/disks").respond(200, json=page)
        result = await _call("azure.disk.list", {})

    assert result.status == "ok", result.error
    assert arm.calls[0].request.url.params["api-version"] == "2026-03-02"
    assert result.result["value"] == [
        {
            "name": "disk-a",
            "id": _disk_id("disk-a"),
            "resource_group": GROUP,
            "location": "westeurope",
            "zones": ["1"],
            "size_gb": 128,
            "sku": "Premium_LRS",
            "disk_state": "Attached",
            "owner_vm_id": _vm_id("vm-a"),
            "encryption_type": "EncryptionAtRestWithPlatformKey",
            "os_type": None,
            "tags": {},
        },
        {
            "name": "disk-free",
            "id": _disk_id("disk-free"),
            "resource_group": GROUP,
            "location": "westeurope",
            "zones": ["1"],
            "size_gb": 128,
            "sku": "Premium_LRS",
            "disk_state": "Unattached",
            "owner_vm_id": None,
            "encryption_type": "EncryptionAtRestWithPlatformKey",
            "os_type": None,
            "tags": {},
        },
    ]


@pytest.mark.asyncio
async def test_disk_list_over_50_rows_goes_to_a_stored_result_with_its_paging_fields(
    connector: AzureConnector, store: MemoryResultStore
) -> None:
    del connector, store
    disks = [
        disk_answer(f"disk-{n:02d}", state="Unattached" if n % 10 == 0 else "Attached")
        for n in range(55)
    ]
    first = {
        "value": disks[:30],
        "nextLink": f"{COMPUTE_URL}/disks?api-version=2026-03-02&$skiptoken=disks2",
    }
    with respx.mock() as mock:
        mock.post(token_url()).respond(200, json=token_answer(login_token()))
        arm = mock.get(f"{COMPUTE_URL}/disks").mock(
            side_effect=_pages(first, {"value": disks[30:]})
        )
        result = await _call("azure.disk.list", {})

    assert result.status == "ok", result.error
    assert arm.call_count == 2
    assert result.handle is not None
    assert result.result["row_count"] == 55
    assert result.result["truncated"] is False
    assert result.result["pages_read"] == 2
    unattached = await _query(
        result, {"filter": [{"field": "disk_state", "op": "=", "value": "Unattached"}]}
    )
    assert sorted(row["name"] for row in unattached["rows"]) == [
        "disk-00",
        "disk-10",
        "disk-20",
        "disk-30",
        "disk-40",
        "disk-50",
    ]


@pytest.mark.asyncio
async def test_disk_list_of_a_resource_group_uses_its_path(connector: AzureConnector) -> None:
    del connector
    with respx.mock() as mock:
        mock.post(token_url()).respond(200, json=token_answer(login_token()))
        arm = mock.get(f"{GROUP_COMPUTE_URL}/disks").respond(200, json={"value": []})
        result = await _call("azure.disk.list", {"resource_group": GROUP})

    assert result.status == "ok", result.error
    assert arm.call_count == 1
    assert result.result["value"] == []


@pytest.mark.asyncio
async def test_image_list_shows_the_image_without_its_blob_url(connector: AzureConnector) -> None:
    del connector
    with respx.mock() as mock:
        mock.post(token_url()).respond(200, json=token_answer(login_token()))
        arm = mock.get(f"{COMPUTE_URL}/images").respond(
            200, json={"value": [image_answer("image-a")]}
        )
        result = await _call("azure.image.list", {})

    assert result.status == "ok", result.error
    assert arm.calls[0].request.url.params["api-version"] == "2026-04-01"
    assert result.result["value"] == [
        {
            "name": "image-a",
            "id": _resource_id("images", "image-a"),
            "resource_group": GROUP,
            "location": "westeurope",
            "os_type": "Linux",
            "os_state": "Generalized",
            "hyperv_generation": "V2",
            "os_disk_size_gb": 30,
            "data_disk_count": 2,
            "source_vm_id": _vm_id("vm-a"),
            "provisioning_state": "Succeeded",
            "tags": {"purpose": "example"},
        }
    ]
    assert "blob.core.windows.net" not in json.dumps(result.result)


@pytest.mark.asyncio
async def test_image_list_over_50_rows_goes_to_a_stored_result(
    connector: AzureConnector, store: MemoryResultStore
) -> None:
    del connector, store
    images = {"value": [image_answer(f"image-{n:02d}") for n in range(52)]}
    with respx.mock() as mock:
        mock.post(token_url()).respond(200, json=token_answer(login_token()))
        mock.get(f"{GROUP_COMPUTE_URL}/images").respond(200, json=images)
        result = await _call("azure.image.list", {"resource_group": GROUP})

    assert result.status == "ok", result.error
    assert result.handle is not None
    assert result.result["row_count"] == 52
    assert result.result["truncated"] is False
    assert result.result["pages_read"] == 1
    found = await _query(result, {"filter": [{"field": "name", "op": "=", "value": "image-51"}]})
    assert found["rows"][0]["os_state"] == "Generalized"


@pytest.mark.asyncio
async def test_vmss_list_shows_size_capacity_and_mode(connector: AzureConnector) -> None:
    del connector
    with respx.mock() as mock:
        mock.post(token_url()).respond(200, json=token_answer(login_token()))
        arm = mock.get(f"{COMPUTE_URL}/virtualMachineScaleSets").respond(
            200, json={"value": [scale_set_answer("vmss-a")]}
        )
        result = await _call("azure.vmss.list", {})

    assert result.status == "ok", result.error
    assert arm.calls[0].request.url.params["api-version"] == "2026-04-01"
    assert result.result["value"] == [
        {
            "name": "vmss-a",
            "id": _resource_id("virtualMachineScaleSets", "vmss-a"),
            "resource_group": GROUP,
            "location": "westeurope",
            "zones": ["1", "2"],
            "size": "Standard_D2s_v5",
            "capacity": 3,
            "orchestration_mode": "Uniform",
            "upgrade_mode": "Manual",
            "os_type": "Windows",
            "provisioning_state": "Succeeded",
            "tags": {},
        }
    ]
    assert "example-admin-user" not in json.dumps(result.result)


@pytest.mark.asyncio
async def test_vmss_list_over_50_rows_goes_to_a_stored_result(
    connector: AzureConnector, store: MemoryResultStore
) -> None:
    del connector, store
    scale_sets = {"value": [scale_set_answer(f"vmss-{n:02d}") for n in range(51)]}
    with respx.mock() as mock:
        mock.post(token_url()).respond(200, json=token_answer(login_token()))
        mock.get(f"{GROUP_COMPUTE_URL}/virtualMachineScaleSets").respond(200, json=scale_sets)
        result = await _call("azure.vmss.list", {"resource_group": GROUP})

    assert result.status == "ok", result.error
    assert result.handle is not None
    assert result.result["row_count"] == 51
    assert result.result["pages_read"] == 1
    assert result.result["truncated"] is False


# ---------------------------------------------------------------------------
# azure.vm.size.list
# ---------------------------------------------------------------------------


def _skus(*sizes: dict[str, Any]) -> dict[str, Any]:
    """A Resource SKUs answer: the VM sizes plus SKUs of other kinds."""
    other = [
        {"resourceType": "disks", "name": "Premium_LRS", "tier": "Premium", "size": "P1"},
        {"resourceType": "availabilitySets", "name": "Aligned", "capabilities": []},
    ]
    return {"value": [*other, *sizes]}


@pytest.mark.asyncio
async def test_vm_size_list_keeps_only_vm_sizes_with_their_numbers(
    connector: AzureConnector,
) -> None:
    del connector
    answer = _skus(
        size_answer("Standard_D2s_v5"),
        size_answer(
            "Standard_D4ps_v5",
            vcpus=4,
            memory="16",
            architecture="Arm64",
            restrictions=(ZONE_RESTRICTION,),
        ),
        size_answer("Standard_E2_v9", memory="0.75", restrictions=(LOCATION_RESTRICTION,)),
    )
    with respx.mock() as mock:
        mock.post(token_url()).respond(200, json=token_answer(login_token()))
        arm = mock.get(f"{COMPUTE_URL}/skus").respond(200, json=answer)
        result = await _call("azure.vm.size.list", {"location": "WestEurope"})

    assert result.status == "ok", result.error
    params = arm.calls[0].request.url.params
    assert params["api-version"] == "2021-07-01"
    assert params["$filter"] == "location eq 'westeurope'"
    assert result.result["location"] == "westeurope"
    rows = result.result["value"]
    assert [row["name"] for row in rows] == [
        "Standard_D2s_v5",
        "Standard_D4ps_v5",
        "Standard_E2_v9",
    ]
    assert rows[0] == {
        "name": "Standard_D2s_v5",
        "family": "standardDSv5Family",
        "vcpus": 2,
        "vcpus_available": 2,
        "memory_gb": 8.0,
        "max_data_disks": 4,
        "premium_io": True,
        "hyperv_generations": ["V1", "V2"],
        "acus": 195,
        "cpu_architecture": "x64",
        "zones": ["1", "2", "3"],
        "restrictions": [],
    }
    assert rows[1]["cpu_architecture"] == "Arm64"
    assert rows[1]["restrictions"] == [
        {"type": "Zone", "reason_code": "NotAvailableForSubscription", "zones": ["2"]}
    ]
    assert rows[2]["memory_gb"] == 0.75
    assert rows[2]["restrictions"] == [
        {"type": "Location", "reason_code": "NotAvailableForSubscription", "zones": []}
    ]


@pytest.mark.asyncio
async def test_vm_size_list_over_50_sizes_goes_to_a_stored_result_with_the_location(
    connector: AzureConnector, store: MemoryResultStore
) -> None:
    del connector, store
    sizes = [size_answer(f"Standard_X{n}", vcpus=n % 8 + 1) for n in range(70)]
    with respx.mock() as mock:
        mock.post(token_url()).respond(200, json=token_answer(login_token()))
        mock.get(f"{COMPUTE_URL}/skus").respond(200, json=_skus(*sizes))
        result = await _call("azure.vm.size.list", {"location": "westeurope"})

    assert result.status == "ok", result.error
    assert result.handle is not None
    assert result.result["row_count"] == 70
    assert result.result["location"] == "westeurope"
    assert result.result["truncated"] is False
    assert result.result["pages_read"] == 1
    big = await _query(result, {"filter": [{"field": "vcpus", "op": ">=", "value": 8}]})
    expected = sorted(f"Standard_X{n}" for n in range(7, 70, 8))
    assert sorted(row["name"] for row in big["rows"]) == expected


@pytest.mark.parametrize(
    "location", ["westeurope' or name eq 'x", "west europe", "", "west-europe"]
)
@pytest.mark.asyncio
async def test_vm_size_list_refuses_a_location_that_is_not_a_region_name(
    connector: AzureConnector, location: str
) -> None:
    del connector
    with respx.mock(assert_all_called=False) as mock:
        arm = mock.get(url__startswith=ARM_BASE).respond(200, json={"value": []})
        result = await _call("azure.vm.size.list", {"location": location})

    assert result.status == "error"
    assert result.extras["error_code"] == "invalid_params"
    assert not arm.called


# ---------------------------------------------------------------------------
# Rows from odd answers
# ---------------------------------------------------------------------------


def test_a_size_row_turns_bad_numbers_into_none() -> None:
    odd = size_answer("Standard_Odd")
    odd["capabilities"] = [
        {"name": "vCPUs", "value": "²"},
        {"name": "MemoryGB", "value": "nan"},
        {"name": "MaxDataDiskCount", "value": "-1"},
        {"name": "PremiumIO", "value": "maybe"},
        {"name": "ACUs", "value": "160-190"},
        {"name": 5, "value": "x"},
        "not-a-pair",
    ]
    odd["locationInfo"] = [{"location": "othereurope", "zones": ["9"]}]
    odd["restrictions"] = ["not-a-restriction"]

    row = size_row(odd, "westeurope")

    assert row is not None
    assert row["vcpus"] is None
    assert row["memory_gb"] is None
    assert row["max_data_disks"] is None
    assert row["premium_io"] is None
    assert row["acus"] is None
    assert row["hyperv_generations"] == []
    assert row["zones"] == []
    assert row["restrictions"] == [{"type": None, "reason_code": None, "zones": []}]
    assert size_row({"resourceType": "disks", "name": "Premium_LRS"}, "westeurope") is None
    assert size_row("not-a-sku", "westeurope") is None


def test_a_vm_detail_from_a_bare_answer_has_every_field() -> None:
    detail = vm_detail({"name": "vm-bare", "id": "/not/a/resource/id"})

    assert detail["resource_group"] is None
    assert detail["power_state"] is None
    assert detail["os_disk"] == {
        "name": None,
        "id": None,
        "size_gb": None,
        "sku": None,
        "caching": None,
    }
    assert detail["data_disks"] == []
    assert detail["network_interfaces"] == []
    assert detail["tags"] == {}


def test_the_last_power_state_code_counts() -> None:
    view = {
        "statuses": [
            {"code": "PowerState/starting"},
            {"code": "ProvisioningState/updating"},
            {"code": "POWERSTATE/Running"},
            {"code": 7},
        ]
    }
    assert vm_detail({"properties": {"instanceView": view}})["power_state"] == "running"


# ---------------------------------------------------------------------------
# Registration and contracts
# ---------------------------------------------------------------------------


def test_the_family_is_in_the_operation_table() -> None:
    by_op = {op.op_id: op for op in AZURE_TYPED_OPS}
    assert [op.op_id for op in compute.OPS] == list(COMPUTE_OP_IDS)
    for op_id in COMPUTE_OP_IDS:
        op = by_op[op_id]
        assert op.group_key == "compute"
        assert op.safety_level == "safe" and op.requires_approval is False
        assert "read-only" in op.tags
        assert write_word_in_op_id(op_id) is None
        assert callable(getattr(AzureConnector, op.handler_attr))
        assert op.llm_instructions is not None
        assert {"when_to_call", "output_shape", "next_step"} <= set(op.llm_instructions)
    assert AZURE_TYPED_WHEN_TO_USE_BY_GROUP["compute"] == compute.WHEN_TO_USE["compute"]
    hint = by_op["azure.vm.list"].llm_instructions
    assert hint is not None
    assert hint["result_objects"] == {
        "objects": {
            "power_state_read": [
                "vms_without_state",
                "pages_read",
                "truncated",
                "omission",
                "omission_kind",
            ]
        }
    }


@pytest.mark.asyncio
async def test_the_compute_operations_are_stored_as_typed_safe_reads(
    connector: AzureConnector, session: AsyncSession
) -> None:
    del connector
    rows = (
        (
            await session.execute(
                select(EndpointDescriptor).where(
                    EndpointDescriptor.product == AZURE_PRODUCT,
                    EndpointDescriptor.op_id.in_(COMPUTE_OP_IDS),
                )
            )
        )
        .scalars()
        .all()
    )
    by_op = {row.op_id: row for row in rows}
    assert set(by_op) == set(COMPUTE_OP_IDS)
    for row in rows:
        assert row.source_kind == "typed"
        assert row.is_enabled is True
        assert row.safety_level == "safe"
        assert row.requires_approval is False
    handler_ref = by_op["azure.vm.list"].handler_ref
    assert handler_ref is not None and handler_ref.endswith("ComputeOps.vm_list")
    group = (
        await session.execute(
            select(OperationGroup).where(
                OperationGroup.product == AZURE_PRODUCT, OperationGroup.group_key == "compute"
            )
        )
    ).scalar_one()
    assert group.when_to_use == compute.WHEN_TO_USE["compute"]
