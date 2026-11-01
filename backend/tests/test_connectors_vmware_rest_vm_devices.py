# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Unit tests for the VM device changes: disk remove, network adapter remove, controller add.

Each test drives the real handler through a real ``VmwareRestConnector``. vCenter
is mocked with ``respx`` on the vCenter 8 path (``/sdk/vim25/8.0.3.0``) and the
vCenter 9 path (``/sdk/vim25/9.0.0.0``), so the checks are on the bytes that go
out. The answers are written by hand from the vendor API reference, with
made-up names and ids. The policy check of the write seam is replaced by a
recorder: it lets the write run, or returns a parked result, so a held write
never reaches vCenter.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
import respx

from meho_backplane.auth.operator import Operator
from meho_backplane.connectors.schemas import AuthModel
from meho_backplane.connectors.vmware_rest import VmwareRestConnector
from meho_backplane.connectors.vmware_rest.composites import (
    _vm_devices,
    _vm_disk_remove,
    _vm_nic_remove,
    _write,
)
from meho_backplane.connectors.vmware_rest.composites._register import (
    _COMPOSITES,
    COMPOSITES_NOT_ON_8_0,
)
from meho_backplane.operations._preview import (
    _PREVIEW_BUILDERS,
    PreviewContext,
    approved_preview_var,
)
from meho_backplane.operations._validate import validate_params
from tests._vmware_teardown_fake import GateRecorder, operator, parked

_HOST = "https://vc-devices.test.invalid"
_VM = "vm-42"
_API = f"/api/vcenter/vm/{_VM}/hardware"
_VERSIONS = pytest.mark.parametrize(
    ("about_version", "vijson"),
    [("8.0.3", "/sdk/vim25/8.0.3.0"), ("9.0.0", "/sdk/vim25/9.0.0.0")],
    ids=["vcenter-8", "vcenter-9"],
)
_DISK_OP = "vmware.composite.vm.disk.remove"
_NIC_OP = "vmware.composite.vm.nic.remove"
_CONTROLLER_OP = "vmware.composite.vm.controller.add"
_TEN_GIB = 10 * 1024**3


@dataclass
class _Target:
    name: str = "vc-devices"
    host: str = "vc-devices.test.invalid"
    port: int | None = 443
    secret_ref: str = "vsphere/vc-devices"
    auth_model: str | None = AuthModel.SHARED_SERVICE_ACCOUNT.value
    id: UUID = field(default_factory=uuid4)
    tenant_id: UUID = field(default_factory=lambda: UUID(int=0))


async def _loader(_target: Any, _operator: Operator) -> dict[str, str]:
    return {"username": "svc-meho", "password": "<test-password>"}


@asynccontextmanager
async def _vcenter(about_version: str) -> AsyncIterator[tuple[VmwareRestConnector, Any]]:
    """A real connector over a respx-mocked vCenter of the given version."""
    connector = VmwareRestConnector(session_loader=_loader)

    async def _aclose() -> None:
        connector._session_tokens.clear()
        for client in connector._clients.values():
            await client.aclose()
        connector._clients.clear()

    connector.aclose = _aclose  # type: ignore[method-assign]
    try:
        async with respx.mock(base_url=_HOST, assert_all_called=False) as mock:
            mock.post("/api/session").respond(200, json="tok")
            mock.get("/api/about").respond(200, json={"version": about_version})
            yield connector, mock
    finally:
        await connector.aclose()


@pytest.fixture
def gate(monkeypatch: pytest.MonkeyPatch) -> GateRecorder:
    recorder = GateRecorder()
    monkeypatch.setattr(_write, "enforce_subop_policy", recorder)
    return recorder


def _vm_result(props: dict[str, Any]) -> dict[str, Any]:
    return {
        "objects": [
            {
                "obj": {
                    "_typeName": "ManagedObjectReference",
                    "type": "VirtualMachine",
                    "value": _VM,
                },
                "propSet": [{"name": k, "val": v} for k, v in props.items()],
            }
        ]
    }


def _devices(*devices: dict[str, Any]) -> dict[str, Any]:
    return {"_typeName": "ArrayOfVirtualDevice", "_value": list(devices)}


def _disk(key: int, label: str, *, sharing: str = "sharingNone") -> dict[str, Any]:
    return {
        "_typeName": "VirtualDisk",
        "key": key,
        "deviceInfo": {"_typeName": "Description", "label": label, "summary": "10,485,760 KB"},
        "backing": {
            "_typeName": "VirtualDiskFlatVer2BackingInfo",
            "fileName": f"[<datastore>] app-vm/app-vm_{key}.vmdk",
            "diskMode": "persistent",
            "sharing": sharing,
        },
        "controllerKey": 1000,
        "unitNumber": key - 2000,
        "capacityInBytes": _TEN_GIB,
    }


def _usb_controller() -> dict[str, Any]:
    return {
        "_typeName": "VirtualUSBXHCIController",
        "key": 14000,
        "deviceInfo": {"_typeName": "Description", "label": "USB xHCI controller "},
        "busNumber": 0,
        "autoConnectDevices": True,
    }


def _vm_with(*devices: dict[str, Any]) -> dict[str, Any]:
    return _vm_result(
        {
            "name": "app-vm",
            "runtime.powerState": "poweredOff",
            "config.hardware.device": _devices(*devices),
        }
    )


def _task(state: str, *, fault: str | None = None, message: str | None = None) -> dict[str, Any]:
    info: dict[str, Any] = {"_typeName": "TaskInfo", "state": state}
    if fault is not None:
        info["error"] = {
            "_typeName": "LocalizedMethodFault",
            "fault": {"_typeName": fault},
            "localizedMessage": message,
        }
    return {
        "objects": [
            {
                "obj": {"_typeName": "ManagedObjectReference", "type": "Task", "value": "task-7"},
                "propSet": [{"name": "info", "val": info}],
            }
        ]
    }


_TASK_REF = {"_typeName": "ManagedObjectReference", "type": "Task", "value": "task-7"}


def _retrieve(mock: Any, vijson: str, *answers: dict[str, Any]) -> Any:
    return mock.post(f"{vijson}/PropertyCollector/propertyCollector/RetrievePropertiesEx").mock(
        side_effect=[httpx.Response(200, json=a) for a in answers]
    )


def preview_ctx(params: dict[str, Any], connector: Any) -> PreviewContext:
    return PreviewContext(
        descriptor=object(),  # type: ignore[arg-type]  # builders ignore it
        connector_instance=connector,
        operator=operator(),
        target=_Target(),
        params=params,
    )


async def _run(handler: Any, connector: Any, **params: Any) -> Any:
    return await handler(operator=operator(), target=_Target(), params=params, connector=connector)


# ===========================================================================
# Registration: risk level and approval of each operation
# ===========================================================================


def test_each_operation_has_its_risk_level_and_waits_for_approval() -> None:
    rows = {spec.op_id: spec for spec in _COMPOSITES}
    expected = {_DISK_OP: "destructive", _NIC_OP: "dangerous", _CONTROLLER_OP: "dangerous"}
    for op_id, level in expected.items():
        assert rows[op_id].safety_level == level
        assert rows[op_id].requires_approval is True
        assert rows[op_id].group_key == "vm"
        assert op_id not in COMPOSITES_NOT_ON_8_0
        assert op_id in _PREVIEW_BUILDERS
        for example in rows[op_id].parameter_schema["examples"]:
            validate_params(rows[op_id].parameter_schema, example)


# ===========================================================================
# vm.disk.remove
# ===========================================================================


@_VERSIONS
async def test_disk_remove_keeps_the_file_and_reads_back(
    gate: GateRecorder, about_version: str, vijson: str
) -> None:
    async with _vcenter(about_version) as (conn, mock):
        retrieve = _retrieve(
            mock,
            vijson,
            _vm_with(_disk(2000, "Hard disk 1"), _disk(2001, "Hard disk 2")),
            _task("running"),
            _task("success"),
            _vm_result({"config.hardware.device": _devices(_disk(2000, "Hard disk 1"))}),
        )
        reconfig = mock.post(f"{vijson}/VirtualMachine/{_VM}/ReconfigVM_Task").respond(
            200, json=_TASK_REF
        )
        out = await _run(_vm_disk_remove.vm_disk_remove_composite, conn, vm=_VM, disk="2001")

    assert out["status"] == "removed"
    assert out["object"]["label"] == "Hard disk 2"
    assert out["object"]["vm_name"] == "app-vm"
    assert out["remaining_disks"] == [
        {
            "disk": "2000",
            "label": "Hard disk 1",
            "capacity_bytes": _TEN_GIB,
            "vmdk_file": "[<datastore>] app-vm/app-vm_2000.vmdk",
        }
    ]
    assert retrieve.call_count == 4
    body = json.loads(reconfig.calls[0].request.content)
    change = body["spec"]["deviceChange"][0]
    assert body["spec"]["_typeName"] == "VirtualMachineConfigSpec"
    assert change["_typeName"] == "VirtualDeviceConfigSpec"
    assert change["operation"] == "remove"
    assert "fileOperation" not in change
    assert change["device"]["key"] == 2001
    assert gate.calls[0]["op_id"] == "POST:/VirtualMachine/{moId}/ReconfigVM_Task"
    assert gate.calls[0]["params"] == {"vm": _VM, "disk": "2001", "delete_file": False}


@_VERSIONS
async def test_disk_remove_with_delete_file_destroys_the_file(
    gate: GateRecorder, about_version: str, vijson: str
) -> None:
    async with _vcenter(about_version) as (conn, mock):
        _retrieve(
            mock,
            vijson,
            _vm_with(_disk(2001, "Hard disk 2")),
            _task("success"),
            _vm_result({"config.hardware.device": _devices()}),
        )
        reconfig = mock.post(f"{vijson}/VirtualMachine/{_VM}/ReconfigVM_Task").respond(
            200, json=_TASK_REF
        )
        out = await _run(
            _vm_disk_remove.vm_disk_remove_composite, conn, vm=_VM, disk="2001", delete_file=True
        )

    assert out["status"] == "removed"
    assert out["remaining_disks"] == []
    change = json.loads(reconfig.calls[0].request.content)["spec"]["deviceChange"][0]
    assert change["fileOperation"] == "destroy"


@_VERSIONS
async def test_disk_remove_task_fault_reaches_the_answer(
    gate: GateRecorder, about_version: str, vijson: str
) -> None:
    async with _vcenter(about_version) as (conn, mock):
        _retrieve(
            mock,
            vijson,
            _vm_with(_disk(2001, "Hard disk 2")),
            _task(
                "error", fault="InvalidDeviceSpec", message="Invalid configuration for device '0'."
            ),
        )
        mock.post(f"{vijson}/VirtualMachine/{_VM}/ReconfigVM_Task").respond(200, json=_TASK_REF)
        with pytest.raises(RuntimeError) as err:
            await _run(_vm_disk_remove.vm_disk_remove_composite, conn, vm=_VM, disk="2001")

    assert "InvalidDeviceSpec" in str(err.value)
    assert "Invalid configuration for device" in str(err.value)


async def test_disk_remove_of_an_unknown_disk_sends_nothing(gate: GateRecorder) -> None:
    async with _vcenter("9.0.0") as (conn, mock):
        _retrieve(mock, "/sdk/vim25/9.0.0.0", _vm_with(_disk(2000, "Hard disk 1")))
        reconfig = mock.post(f"/sdk/vim25/9.0.0.0/VirtualMachine/{_VM}/ReconfigVM_Task")
        out = await _run(_vm_disk_remove.vm_disk_remove_composite, conn, vm=_VM, disk="2009")

    assert out["status"] == "not_found"
    assert out["remaining_disks"] is None
    assert not reconfig.called
    assert gate.calls == []


def _approved_disk_preview(disk: dict[str, Any]) -> dict[str, Any]:
    """The park-time preview of a disk removal, as the approval stores it."""
    return {
        "blast_radius": {
            "object": {
                "kind": "vm_disk",
                "vm": _VM,
                "disk": str(disk["key"]),
                "label": disk["deviceInfo"]["label"],
                "capacity_bytes": disk["capacityInBytes"],
                "vmdk_file": disk["backing"]["fileName"],
                "present": True,
            },
            "children": [],
            "irreversibility": "permanent",
        }
    }


@pytest.mark.parametrize(
    "approved",
    [
        pytest.param(_approved_disk_preview(_disk(2001, "Hard disk 2")), id="replaced-disk"),
        pytest.param({}, id="no-preview-stored"),
    ],
)
async def test_an_approved_remove_refuses_a_disk_that_is_not_the_one_approved(
    gate: GateRecorder, approved: dict[str, Any]
) -> None:
    """While the approval waited, another disk took the key the approver saw."""
    live = _disk(2001, "Hard disk 3")
    live["backing"]["fileName"] = "[<datastore>] app-vm/app-vm_2001_new.vmdk"
    token = approved_preview_var.set((_DISK_OP, approved))
    try:
        async with _vcenter("9.0.0") as (conn, mock):
            _retrieve(mock, "/sdk/vim25/9.0.0.0", _vm_with(_disk(2000, "Hard disk 1"), live))
            reconfig = mock.post(f"/sdk/vim25/9.0.0.0/VirtualMachine/{_VM}/ReconfigVM_Task")
            out = await _run(
                _vm_disk_remove.vm_disk_remove_composite,
                conn,
                vm=_VM,
                disk="2001",
                delete_file=True,
            )
    finally:
        approved_preview_var.reset(token)

    assert out["status"] == "precondition_failed"
    assert out["blockers"][0]["kind"] == "disk_changed"
    assert out["blockers"][0]["now"]["vmdk_file"] == "[<datastore>] app-vm/app-vm_2001_new.vmdk"
    assert "not the disk the approver saw" in out["guidance"]
    assert out["remaining_disks"] is None
    assert not reconfig.called
    assert gate.calls == []


async def test_an_approved_remove_runs_on_the_disk_the_approver_saw(gate: GateRecorder) -> None:
    token = approved_preview_var.set((_DISK_OP, _approved_disk_preview(_disk(2001, "Hard disk 2"))))
    try:
        async with _vcenter("9.0.0") as (conn, mock):
            _retrieve(
                mock,
                "/sdk/vim25/9.0.0.0",
                _vm_with(_disk(2000, "Hard disk 1"), _disk(2001, "Hard disk 2")),
                _task("success"),
                _vm_result({"config.hardware.device": _devices(_disk(2000, "Hard disk 1"))}),
            )
            path = f"/sdk/vim25/9.0.0.0/VirtualMachine/{_VM}/ReconfigVM_Task"
            reconfig = mock.post(path).respond(200, json=_TASK_REF)
            out = await _run(_vm_disk_remove.vm_disk_remove_composite, conn, vm=_VM, disk="2001")
    finally:
        approved_preview_var.reset(token)

    assert out["status"] == "removed"
    assert reconfig.call_count == 1


async def test_disk_remove_on_an_unknown_vm_sends_nothing(gate: GateRecorder) -> None:
    fault = {"_typeName": "ManagedObjectNotFound", "faultstring": "The object has been deleted"}
    async with _vcenter("9.0.0") as (conn, mock):
        mock.post(
            "/sdk/vim25/9.0.0.0/PropertyCollector/propertyCollector/RetrievePropertiesEx"
        ).respond(500, json=fault)
        out = await _run(_vm_disk_remove.vm_disk_remove_composite, conn, vm=_VM, disk="2001")

    assert out["status"] == "not_found"
    assert gate.calls == []


@pytest.mark.parametrize(("vm", "disk"), [("app-vm", "2001"), (_VM, "../2001")])
async def test_disk_remove_checks_both_ids_before_any_call(
    gate: GateRecorder, vm: str, disk: str
) -> None:
    async with _vcenter("9.0.0") as (conn, mock):
        retrieve = _retrieve(mock, "/sdk/vim25/9.0.0.0")
        out = await _run(_vm_disk_remove.vm_disk_remove_composite, conn, vm=vm, disk=disk)

    assert out["status"] == "invalid_request"
    assert not retrieve.called


async def test_disk_remove_refuses_to_delete_a_shared_file(gate: GateRecorder) -> None:
    async with _vcenter("9.0.0") as (conn, mock):
        _retrieve(
            mock,
            "/sdk/vim25/9.0.0.0",
            _vm_with(_disk(2001, "Quorum", sharing="sharingMultiWriter")),
        )
        out = await _run(
            _vm_disk_remove.vm_disk_remove_composite, conn, vm=_VM, disk="2001", delete_file=True
        )

    assert out["status"] == "precondition_failed"
    assert out["blockers"] == [{"kind": "shared_disk", "sharing": "sharingMultiWriter"}]
    assert gate.calls == []


async def test_disk_remove_returns_a_parked_gate_as_is(monkeypatch: pytest.MonkeyPatch) -> None:
    held = parked(_DISK_OP)
    monkeypatch.setattr(_write, "enforce_subop_policy", GateRecorder(held))
    async with _vcenter("8.0.3") as (conn, mock):
        _retrieve(mock, "/sdk/vim25/8.0.3.0", _vm_with(_disk(2001, "Hard disk 2")))
        reconfig = mock.post(f"/sdk/vim25/8.0.3.0/VirtualMachine/{_VM}/ReconfigVM_Task")
        out = await _run(_vm_disk_remove.vm_disk_remove_composite, conn, vm=_VM, disk="2001")

    assert out is held
    assert not reconfig.called


@pytest.mark.parametrize(
    ("delete_file", "irreversibility", "children"),
    [(False, "reversible-file-kept", 0), (True, "permanent", 1)],
)
async def test_disk_remove_preview_fills_the_blast_radius(
    delete_file: bool, irreversibility: str, children: int
) -> None:
    async with _vcenter("9.0.0") as (conn, mock):
        _retrieve(mock, "/sdk/vim25/9.0.0.0", _vm_with(_disk(2001, "Hard disk 2")))
        preview = await _vm_disk_remove.vm_disk_remove_preview(
            preview_ctx({"vm": _VM, "disk": "2001", "delete_file": delete_file}, conn)
        )

    assert preview is not None
    block = preview["blast_radius"]
    assert block["object"]["label"] == "Hard disk 2"
    assert block["object"]["vmdk_file"] == "[<datastore>] app-vm/app-vm_2001.vmdk"
    assert block["object"]["present"] is True
    assert block["irreversibility"] == irreversibility
    assert len(block["children"]) == children
    assert "Hard disk 2" in block["summary"]


async def test_disk_remove_preview_refuses_a_missing_disk() -> None:
    async with _vcenter("9.0.0") as (conn, mock):
        _retrieve(mock, "/sdk/vim25/9.0.0.0", _vm_with(_disk(2000, "Hard disk 1")))
        with pytest.raises(ValueError, match="has no disk"):
            await _vm_disk_remove.vm_disk_remove_preview(
                preview_ctx({"vm": _VM, "disk": "2001"}, conn)
            )


async def test_disk_remove_preview_declines_without_a_connector() -> None:
    assert (
        await _vm_disk_remove.vm_disk_remove_preview(preview_ctx({"vm": _VM, "disk": "1"}, None))
        is None
    )


# ===========================================================================
# vm.nic.remove
# ===========================================================================

_NIC_INFO = {
    "label": "Network adapter 2",
    "type": "VMXNET3",
    "mac_address": "00:50:56:00:00:02",
    "backing": {"type": "DISTRIBUTED_PORTGROUP", "network": "dvportgroup-11"},
    "state": "CONNECTED",
    "start_connected": True,
}


@_VERSIONS
async def test_nic_remove_deletes_and_reads_back(
    gate: GateRecorder, about_version: str, vijson: str
) -> None:
    async with _vcenter(about_version) as (conn, mock):
        mock.get(f"{_API}/ethernet/4001").respond(200, json=_NIC_INFO)
        delete = mock.delete(f"{_API}/ethernet/4001").respond(204)
        mock.get(f"{_API}/ethernet").respond(200, json=[{"nic": "4000"}])
        out = await _run(_vm_nic_remove.vm_nic_remove_composite, conn, vm=_VM, nic="4001")

    assert out["status"] == "removed"
    assert out["remaining_nics"] == ["4000"]
    assert out["removed_nic"]["label"] == "Network adapter 2"
    assert out["removed_nic"]["network"] == "dvportgroup-11"
    assert delete.call_count == 1
    assert gate.calls[0]["op_id"] == "DELETE:/vcenter/vm/{vm}/hardware/ethernet/{nic}"
    assert gate.calls[0]["params"] == {"vm": _VM, "nic": "4001"}


async def test_nic_remove_of_an_unknown_adapter_sends_nothing(gate: GateRecorder) -> None:
    async with _vcenter("9.0.0") as (conn, mock):
        mock.get(f"{_API}/ethernet/4009").respond(
            404, json={"error_type": "NOT_FOUND", "messages": []}
        )
        delete = mock.delete(f"{_API}/ethernet/4009")
        out = await _run(_vm_nic_remove.vm_nic_remove_composite, conn, vm=_VM, nic="4009")

    assert out["status"] == "not_found"
    assert out["error_type"] == "NOT_FOUND"
    assert not delete.called
    assert gate.calls == []


async def test_nic_remove_vendor_refusal_reaches_the_answer(gate: GateRecorder) -> None:
    refusal = {
        "error_type": "INVALID_ARGUMENT",
        "messages": [{"id": "x", "default_message": "Hot remove is not supported."}],
    }
    async with _vcenter("8.0.3") as (conn, mock):
        mock.get(f"{_API}/ethernet/4001").respond(200, json=_NIC_INFO)
        mock.delete(f"{_API}/ethernet/4001").respond(400, json=refusal)
        out = await _run(_vm_nic_remove.vm_nic_remove_composite, conn, vm=_VM, nic="4001")

    assert out["status"] == "refused"
    assert out["error_type"] == "INVALID_ARGUMENT"
    assert "Hot remove is not supported." in out["guidance"]


async def test_nic_remove_that_still_reads_back_is_still_present(gate: GateRecorder) -> None:
    async with _vcenter("9.0.0") as (conn, mock):
        mock.get(f"{_API}/ethernet/4001").respond(200, json=_NIC_INFO)
        mock.delete(f"{_API}/ethernet/4001").respond(204)
        mock.get(f"{_API}/ethernet").respond(200, json=[{"nic": "4000"}, {"nic": "4001"}])
        out = await _run(_vm_nic_remove.vm_nic_remove_composite, conn, vm=_VM, nic="4001")

    assert out["status"] == "still_present"


async def test_nic_remove_returns_a_parked_gate_as_is(monkeypatch: pytest.MonkeyPatch) -> None:
    held = parked(_NIC_OP)
    monkeypatch.setattr(_write, "enforce_subop_policy", GateRecorder(held))
    async with _vcenter("9.0.0") as (conn, mock):
        mock.get(f"{_API}/ethernet/4001").respond(200, json=_NIC_INFO)
        delete = mock.delete(f"{_API}/ethernet/4001")
        out = await _run(_vm_nic_remove.vm_nic_remove_composite, conn, vm=_VM, nic="4001")

    assert out is held
    assert not delete.called


async def test_nic_remove_preview_shows_the_adapter() -> None:
    async with _vcenter("9.0.0") as (conn, mock):
        mock.get(f"{_API}/ethernet/4001").respond(200, json=_NIC_INFO)
        preview = await _PREVIEW_BUILDERS[_NIC_OP](preview_ctx({"vm": _VM, "nic": "4001"}, conn))

    assert preview is not None
    assert preview["nic"]["mac_address"] == "00:50:56:00:00:02"
    assert "Network adapter 2" in preview["summary"]


# ===========================================================================
# vm.controller.add
# ===========================================================================


@_VERSIONS
@pytest.mark.parametrize(
    ("params", "kind", "body", "info"),
    [
        (
            {"type": "PVSCSI"},
            "scsi",
            {"type": "PVSCSI", "bus": None},
            {
                "label": "SCSI controller 1",
                "type": "PVSCSI",
                "scsi": {"bus": 1, "unit": 7},
                "sharing": "NONE",
            },
        ),
        (
            {"type": "LSILOGICSAS", "bus": 2, "sharing": "VIRTUAL"},
            "scsi",
            {"type": "LSILOGICSAS", "bus": 2, "sharing": "VIRTUAL"},
            {
                "label": "SCSI controller 2",
                "type": "LSILOGICSAS",
                "scsi": {"bus": 2, "unit": 7},
                "sharing": "VIRTUAL",
            },
        ),
        (
            {"type": "SATA", "bus": 1},
            "sata",
            {"type": "AHCI", "bus": 1},
            {"label": "SATA controller 1", "type": "AHCI", "bus": 1},
        ),
        ({"type": "NVME"}, "nvme", {"bus": None}, {"label": "NVME controller 0", "bus": 0}),
    ],
    ids=["pvscsi", "lsilogicsas", "sata", "nvme"],
)
async def test_controller_add_through_rest_reads_back(
    gate: GateRecorder,
    about_version: str,
    vijson: str,
    params: dict[str, Any],
    kind: str,
    body: dict[str, Any],
    info: dict[str, Any],
) -> None:
    async with _vcenter(about_version) as (conn, mock):
        create = mock.post(f"{_API}/adapter/{kind}").respond(200, json="1001")
        mock.get(f"{_API}/adapter/{kind}/1001").respond(200, json=info)
        out = await _run(_vm_devices.vm_controller_add_composite, conn, vm=_VM, **params)

    assert out["status"] == "added"
    assert out["controller"]["kind"] == kind
    assert out["controller"]["adapter"] == "1001"
    assert out["controller"]["label"] == info["label"]
    assert out["controller"]["bus"] == params.get("bus", info.get("bus", 1))
    assert json.loads(create.calls[0].request.content) == body
    assert gate.calls[0]["op_id"] == f"POST:/vcenter/vm/{{vm}}/hardware/adapter/{kind}"


async def test_controller_add_vendor_refusal_reaches_the_answer(gate: GateRecorder) -> None:
    refusal = {
        "error_type": "UNSUPPORTED",
        "messages": [
            {"id": "x", "default_message": "Hot add of an NVMe controller is not supported."}
        ],
    }
    async with _vcenter("9.0.0") as (conn, mock):
        mock.post(f"{_API}/adapter/nvme").respond(400, json=refusal)
        out = await _run(_vm_devices.vm_controller_add_composite, conn, vm=_VM, type="NVME")

    assert out["status"] == "refused"
    assert out["error_type"] == "UNSUPPORTED"
    assert "Hot add of an NVMe controller" in out["guidance"]


@_VERSIONS
async def test_controller_add_usb_through_vim_reads_back(
    gate: GateRecorder, about_version: str, vijson: str
) -> None:
    async with _vcenter(about_version) as (conn, mock):
        _retrieve(
            mock,
            vijson,
            _vm_result({"config.hardware.device": _devices(_disk(2000, "Hard disk 1"))}),
            _task("success"),
            _vm_result(
                {"config.hardware.device": _devices(_disk(2000, "Hard disk 1"), _usb_controller())}
            ),
        )
        reconfig = mock.post(f"{vijson}/VirtualMachine/{_VM}/ReconfigVM_Task").respond(
            200, json=_TASK_REF
        )
        out = await _run(_vm_devices.vm_controller_add_composite, conn, vm=_VM, type="USB_XHCI")

    assert out["status"] == "added"
    assert out["task"] == "task-7"
    assert out["controller"] == {
        "kind": "usb",
        "adapter": "14000",
        "label": "USB xHCI controller ",
        "type": "USB_XHCI",
        "bus": 0,
        "sharing": None,
        "pci_slot_number": None,
    }
    change = json.loads(reconfig.calls[0].request.content)["spec"]["deviceChange"][0]
    assert change["operation"] == "add"
    assert change["device"] == {
        "_typeName": "VirtualUSBXHCIController",
        "key": -1,
        "busNumber": 0,
        "autoConnectDevices": True,
    }


async def test_controller_add_usb_twice_sends_nothing(gate: GateRecorder) -> None:
    async with _vcenter("9.0.0") as (conn, mock):
        _retrieve(
            mock,
            "/sdk/vim25/9.0.0.0",
            _vm_result({"config.hardware.device": _devices(_usb_controller())}),
        )
        reconfig = mock.post(f"/sdk/vim25/9.0.0.0/VirtualMachine/{_VM}/ReconfigVM_Task")
        out = await _run(_vm_devices.vm_controller_add_composite, conn, vm=_VM, type="USB_XHCI")

    assert out["status"] == "unchanged"
    assert out["controller"]["adapter"] == "14000"
    assert not reconfig.called
    assert gate.calls == []


async def test_controller_add_usb_task_fault_reaches_the_answer(gate: GateRecorder) -> None:
    async with _vcenter("8.0.3") as (conn, mock):
        _retrieve(
            mock,
            "/sdk/vim25/8.0.3.0",
            _vm_result({"config.hardware.device": _devices()}),
            _task(
                "error",
                fault="DeviceUnsupportedForVmVersion",
                message="Unsupported on this VM version.",
            ),
        )
        mock.post(f"/sdk/vim25/8.0.3.0/VirtualMachine/{_VM}/ReconfigVM_Task").respond(
            200, json=_TASK_REF
        )
        with pytest.raises(RuntimeError) as err:
            await _run(_vm_devices.vm_controller_add_composite, conn, vm=_VM, type="USB_XHCI")

    assert "DeviceUnsupportedForVmVersion" in str(err.value)
    assert "Unsupported on this VM version." in str(err.value)


@pytest.mark.parametrize(
    "params",
    [{"type": "NVME", "sharing": "VIRTUAL"}, {"type": "USB_XHCI", "bus": 1}],
    ids=["sharing-on-nvme", "bus-on-usb"],
)
async def test_controller_add_refuses_a_bad_combination_before_any_call(
    gate: GateRecorder, params: dict[str, Any]
) -> None:
    async with _vcenter("9.0.0") as (conn, mock):
        out = await _run(_vm_devices.vm_controller_add_composite, conn, vm=_VM, **params)
        assert not any(call.request.url.path.startswith(_API) for call in mock.calls)

    assert out["status"] == "invalid_request"
    assert gate.calls == []


async def test_controller_add_returns_a_parked_gate_as_is(monkeypatch: pytest.MonkeyPatch) -> None:
    held = parked(_CONTROLLER_OP)
    monkeypatch.setattr(_write, "enforce_subop_policy", GateRecorder(held))
    async with _vcenter("9.0.0") as (conn, mock):
        create = mock.post(f"{_API}/adapter/scsi")
        out = await _run(_vm_devices.vm_controller_add_composite, conn, vm=_VM, type="PVSCSI")

    assert out is held
    assert not create.called


async def test_controller_add_preview_shows_the_current_controllers() -> None:
    async with _vcenter("9.0.0") as (conn, mock):
        mock.get(f"{_API}/adapter/scsi").respond(200, json=[{"adapter": "1000"}])
        preview = await _PREVIEW_BUILDERS[_CONTROLLER_OP](
            preview_ctx({"vm": _VM, "type": "PVSCSI"}, conn)
        )

    assert preview is not None
    assert preview["requested"] == {"type": "PVSCSI", "bus": None}
    assert preview["current_controllers"] == [{"adapter": "1000"}]
