# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for four VM changes: PCI passthrough add and remove, console
screenshot and key send.

Two layers:

* the handlers and previews against the shared vim fake, extended here with
  ``EnvironmentBrowser.QueryConfigTarget``, ``ReconfigVM_Task``,
  ``CreateScreenshot_Task`` and ``PutUsbScanCodes``, plus a fake datastore file
  access for the screenshot file: the exact vim bodies, the gated write, the
  task poll, the read after the task, every refusal before a write, a vendor
  fault, a timeout and a parked gate;
* the governed flow through the real dispatcher: the four operations are
  registered on both vCenter connectors with their risk level, a person's call
  waits for approval, an agent's dangerous call is refused, and a key that is
  not on the list never reaches vCenter.
"""

from __future__ import annotations

import base64
import copy
import json
import uuid
from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID

import httpx
import pytest
from jsonschema import Draft202012Validator
from sqlalchemy import select

from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.connectors.registry import clear_registry, register_connector_v2
from meho_backplane.connectors.vmware_rest import VmwareRestConnector
from meho_backplane.connectors.vmware_rest.composites import (
    _vm_console,
    _vm_pci_device,
    _write,
    register_vmware_composite_operations,
    register_vmware_composite_operations_8_0,
)
from meho_backplane.connectors.vmware_rest.composites._register import _COMPOSITES
from meho_backplane.connectors.vmware_rest.composites._vm_console import (
    vm_keys_send_composite,
    vm_screenshot_composite,
)
from meho_backplane.connectors.vmware_rest.composites._vm_console_pci_schemas import (
    VM_KEYS_SEND_PARAMETER_SCHEMA,
    VM_KEYS_SEND_RESPONSE_SCHEMA,
    VM_PCI_DEVICE_ADD_PARAMETER_SCHEMA,
    VM_PCI_DEVICE_ADD_RESPONSE_SCHEMA,
    VM_PCI_DEVICE_REMOVE_RESPONSE_SCHEMA,
    VM_SCREENSHOT_RESPONSE_SCHEMA,
)
from meho_backplane.connectors.vmware_rest.composites._vm_pci_device import (
    vm_pci_device_add_composite,
    vm_pci_device_remove_composite,
)
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import EndpointDescriptor
from meho_backplane.db.models import Target as TargetORM
from meho_backplane.operations import dispatch, reset_dispatcher_caches
from meho_backplane.operations._handler_resolve import _CONNECTOR_INSTANCE_CACHE
from meho_backplane.operations._preview import _PREVIEW_BUILDERS
from meho_backplane.operations._validate import validate_params
from meho_backplane.operations.meta_tools import call_operation
from meho_backplane.settings import get_settings
from tests._vmware_teardown_fake import (
    GateRecorder,
    VimFake,
    http_status_error,
    moref,
    operator,
    parked,
    preview_ctx,
)

_VM = "vm-42"
_ADD_OP = "vmware.composite.vm.pci_device.add"
_REMOVE_OP = "vmware.composite.vm.pci_device.remove"
_SCREENSHOT_OP = "vmware.composite.vm.screenshot"
_KEYS_OP = "vmware.composite.vm.keys.send"
_RECONFIG_SUB_OP = "POST:/VirtualMachine/{moId}/ReconfigVM_Task"
_SCREENSHOT_SUB_OP = "POST:/VirtualMachine/{moId}/CreateScreenshot_Task"
_KEYS_SUB_OP = "POST:/VirtualMachine/{moId}/PutUsbScanCodes"
_PCI_ID = "0000:3b:00.0"
_PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
_SCREENSHOT_PATH = "[ds-a] app-vm/app-vm-1.png"


def _direct_device(key: int, pci_id: str) -> dict[str, Any]:
    """A DirectPath I/O ``VirtualPCIPassthrough`` as VI-JSON returns it."""
    return {
        "_typeName": "VirtualPCIPassthrough",
        "key": key,
        "deviceInfo": {"_typeName": "Description", "label": f"PCI device {key - 13000}"},
        "backing": {
            "_typeName": "VirtualPCIPassthroughDeviceBackingInfo",
            "deviceName": "Example GPU",
            "id": pci_id,
            "deviceId": "1eb8",
            "systemId": "<system-id>",
            "vendorId": 4318,
        },
    }


def _disk() -> dict[str, Any]:
    return {"_typeName": "VirtualDisk", "key": 2000, "deviceInfo": {"label": "Hard disk 1"}}


class PciConsoleFake(VimFake):
    """The shared vim fake plus the four VM methods and the datastore file access."""

    def __init__(self) -> None:
        super().__init__()
        self.config_target: dict[str, Any] = {
            "_typeName": "ConfigTarget",
            "pciPassthrough": [
                {
                    "_typeName": "VirtualMachinePciPassthroughInfo",
                    "name": "Example GPU",
                    "systemId": "<system-id>",
                    "pciDevice": {
                        "_typeName": "HostPciDevice",
                        "id": _PCI_ID,
                        "deviceId": -26904,  # 0x96e8 as a signed 16-bit value
                        "vendorId": 4318,
                        "deviceName": "Example GPU",
                    },
                }
            ],
            "dynamicPassthrough": [
                {
                    "_typeName": "VirtualMachineDynamicPassthroughInfo",
                    "name": "Example NIC",
                    "vendorId": 5555,
                    "deviceId": 4660,
                    "deviceName": "Example NIC",
                    "customLabel": "fast",
                }
            ],
        }
        self.keys_fault: Exception | None = None
        self.file_requests: list[httpx.Request] = []
        self.file_body = _PNG
        self.file_status = 200
        self.file_headers: dict[str, str] = {}
        self.soap_logins = 0

    def vm(self, power: str = "poweredOff", devices: list[dict[str, Any]] | None = None) -> None:
        self.add(
            "VirtualMachine",
            _VM,
            name="app-vm",
            **{
                "runtime.powerState": power,
                "config.hardware.device": [_disk(), *(devices or [])],
                "environmentBrowser": moref("EnvironmentBrowser", "envbrowser-7"),
                "parent": moref("Folder", "group-v3"),
            },
        )
        self.add("Folder", "group-v3", parent=moref("Datacenter", "datacenter-2"))
        self.add("Datacenter", "datacenter-2", name="dc-a", parent=moref("Folder", "group-f9"))
        self.add("Folder", "group-f9", name="region", parent=moref("Folder", "group-d1"))
        self.add("Folder", "group-d1", name="Datacenters")

    def devices(self) -> list[dict[str, Any]]:
        found: list[dict[str, Any]] = self.objects[("VirtualMachine", _VM)][
            "config.hardware.device"
        ]
        return found

    async def _post_vmomi_json(
        self,
        target: Any,
        path: str,
        *,
        operator: Operator,
        json: Any = None,
        promote_managed_object_not_found: bool = False,
    ) -> Any:
        _mo_type, _moid, method = path.strip("/").split("/")
        if method == "QueryConfigTarget":
            self.calls.append((path, copy.deepcopy(json)))
            return copy.deepcopy(self.config_target)
        if method == "ReconfigVM_Task":
            self.calls.append((path, copy.deepcopy(json)))
            return self._task(method, apply=lambda: self._reconfigure(json["spec"]))
        if method == "CreateScreenshot_Task":
            self.calls.append((path, copy.deepcopy(json)))
            return self._task(method, apply=lambda: None, result=_SCREENSHOT_PATH)
        if method == "PutUsbScanCodes":
            self.calls.append((path, copy.deepcopy(json)))
            if self.keys_fault is not None:
                raise self.keys_fault
            return len(json["spec"]["keyEvents"])
        return await super()._post_vmomi_json(
            target,
            path,
            operator=operator,
            json=json,
            promote_managed_object_not_found=promote_managed_object_not_found,
        )

    def _reconfigure(self, spec: dict[str, Any]) -> None:
        devices = self.devices()
        for change in spec["deviceChange"]:
            device = change["device"]
            if change["operation"] == "add":
                devices.append({**device, "key": 13000 + len(devices)})
            else:
                devices[:] = [d for d in devices if d["key"] != device["key"]]

    # -- the screenshot file read ----------------------------------------------
    async def _vcenter_soap_login(self, target: Any, operator: Operator, *, service: str) -> str:
        self.soap_logins += 1
        return '"<soap-session>"'

    async def _http_client(self, target: Any) -> httpx.AsyncClient:
        def handler(request: httpx.Request) -> httpx.Response:
            self.file_requests.append(request)
            return httpx.Response(
                self.file_status,
                stream=httpx.ByteStream(self.file_body),
                headers=self.file_headers,
            )

        return httpx.AsyncClient(
            base_url="https://vc.example.test", transport=httpx.MockTransport(handler)
        )

    def _request_extensions(self, target: Any) -> dict[str, Any]:
        return {}


@pytest.fixture
def gate(monkeypatch: pytest.MonkeyPatch) -> GateRecorder:
    recorder = GateRecorder()
    monkeypatch.setattr(_write, "enforce_subop_policy", recorder)
    return recorder


async def _run(handler: Any, conn: PciConsoleFake, **params: Any) -> Any:
    return await handler(
        operator=operator(),
        target=object(),
        params={"vm": _VM, **params},
        connector=conn,
    )


def _no_writes(conn: PciConsoleFake) -> None:
    for method in ("ReconfigVM_Task", "CreateScreenshot_Task", "PutUsbScanCodes"):
        assert conn.calls_to(method) == [], method


# ===========================================================================
# vm.pci_device.add
# ===========================================================================


async def test_add_directpath_sends_the_device_backing_and_reads_it_back(
    gate: GateRecorder,
) -> None:
    conn = PciConsoleFake()
    conn.vm()
    out = await _run(vm_pci_device_add_composite, conn, pci_id=_PCI_ID)

    assert out["status"] == "added", out
    [(path, body)] = conn.calls_to("ReconfigVM_Task")
    assert path == f"/VirtualMachine/{_VM}/ReconfigVM_Task"
    spec = body["spec"]
    assert spec["memoryReservationLockedToMax"] is True
    [change] = spec["deviceChange"]
    assert change["operation"] == "add"
    assert change["device"]["_typeName"] == "VirtualPCIPassthrough"
    assert change["device"]["backing"] == {
        "_typeName": "VirtualPCIPassthroughDeviceBackingInfo",
        "deviceName": "Example GPU",
        "id": _PCI_ID,
        "deviceId": "96e8",
        "systemId": "<system-id>",
        "vendorId": 4318,
    }
    assert [c["op_id"] for c in gate.calls] == [_RECONFIG_SUB_OP]
    assert gate.calls[0]["params"]["pci_id"] == _PCI_ID
    assert (
        conn.calls_to("QueryConfigTarget")[0][0]
        == "/EnvironmentBrowser/envbrowser-7/QueryConfigTarget"
    )
    assert out["device"]["pci_id"] == _PCI_ID
    assert out["device"]["kind"] == "directpath"
    assert out["device"]["vendor_id"] == 4318
    assert [d["device_key"] for d in out["pci_devices"]] == [out["device"]["device_key"]]
    assert out["task_state"] == "success"
    Draft202012Validator(VM_PCI_DEVICE_ADD_RESPONSE_SCHEMA).validate(out)


async def test_add_dynamic_sends_the_allowed_device_and_can_skip_the_reservation(
    gate: GateRecorder,
) -> None:
    conn = PciConsoleFake()
    conn.vm()
    out = await _run(
        vm_pci_device_add_composite,
        conn,
        dynamic_device={"vendor_id": 5555, "device_id": 4660, "custom_label": "fast"},
        reserve_all_memory=False,
    )

    assert out["status"] == "added", out
    [(_, body)] = conn.calls_to("ReconfigVM_Task")
    assert "memoryReservationLockedToMax" not in body["spec"]
    backing = body["spec"]["deviceChange"][0]["device"]["backing"]
    assert backing == {
        "_typeName": "VirtualPCIPassthroughDynamicBackingInfo",
        "deviceName": "",
        "allowedDevice": [
            {
                "_typeName": "VirtualPCIPassthroughAllowedDevice",
                "vendorId": 5555,
                "deviceId": 4660,
            }
        ],
        "customLabel": "fast",
    }
    assert out["device"]["kind"] == "dynamic"
    assert out["device"]["vendor_id"] == 5555
    assert out["device"]["device_id"] == 4660
    Draft202012Validator(VM_PCI_DEVICE_ADD_RESPONSE_SCHEMA).validate(out)


async def test_add_refuses_a_running_vm_before_any_write(gate: GateRecorder) -> None:
    conn = PciConsoleFake()
    conn.vm(power="poweredOn")
    out = await _run(vm_pci_device_add_composite, conn, pci_id=_PCI_ID)
    assert out["status"] == "refused"
    assert "powered off" in out["guidance"]
    assert gate.calls == []
    _no_writes(conn)
    Draft202012Validator(VM_PCI_DEVICE_ADD_RESPONSE_SCHEMA).validate(out)


async def test_add_is_unchanged_when_the_vm_has_the_device(gate: GateRecorder) -> None:
    conn = PciConsoleFake()
    conn.vm(devices=[_direct_device(13000, _PCI_ID)])
    out = await _run(vm_pci_device_add_composite, conn, pci_id=_PCI_ID.upper())
    assert out["status"] == "unchanged"
    assert out["device"]["device_key"] == 13000
    assert gate.calls == []
    _no_writes(conn)


async def test_add_names_the_offered_devices_when_the_host_lacks_one(gate: GateRecorder) -> None:
    conn = PciConsoleFake()
    conn.vm()
    out = await _run(vm_pci_device_add_composite, conn, pci_id="0000:af:00.1")
    assert out["status"] == "invalid_request"
    assert _PCI_ID in out["guidance"]
    assert gate.calls == []
    _no_writes(conn)

    dynamic = await _run(
        vm_pci_device_add_composite, conn, dynamic_device={"vendor_id": 1, "device_id": 2}
    )
    assert dynamic["status"] == "invalid_request"
    assert "5555:4660" in dynamic["guidance"]
    _no_writes(conn)


async def test_add_unknown_vm_is_not_found(gate: GateRecorder) -> None:
    conn = PciConsoleFake()
    out = await _run(vm_pci_device_add_composite, conn, pci_id=_PCI_ID)
    assert out["status"] == "not_found"
    _no_writes(conn)


async def test_add_task_fault_reaches_the_answer(gate: GateRecorder) -> None:
    conn = PciConsoleFake()
    conn.vm()
    conn.task_faults["ReconfigVM_Task"] = ("InvalidDeviceSpec", "Device is in use")
    with pytest.raises(RuntimeError, match=r"InvalidDeviceSpec.*Device is in use"):
        await _run(vm_pci_device_add_composite, conn, pci_id=_PCI_ID)


async def test_add_parked_gate_sends_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_write, "enforce_subop_policy", GateRecorder(parked(_RECONFIG_SUB_OP)))
    conn = PciConsoleFake()
    conn.vm()
    out = await _run(vm_pci_device_add_composite, conn, pci_id=_PCI_ID)
    assert out.status == "awaiting_approval"
    _no_writes(conn)


async def test_add_timeout_is_not_confirmed(
    gate: GateRecorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(_vm_pci_device, "_TASK_TIMEOUT_SECONDS", 0.0)
    conn = PciConsoleFake()
    conn.vm()
    conn.running.add("ReconfigVM_Task")
    out = await _run(vm_pci_device_add_composite, conn, pci_id=_PCI_ID)
    assert out["status"] == "timeout"
    assert out["issues"][0]["severity"] == "error"
    Draft202012Validator(VM_PCI_DEVICE_ADD_RESPONSE_SCHEMA).validate(out)


def test_add_parameters_need_exactly_one_device_kind() -> None:
    schema = VM_PCI_DEVICE_ADD_PARAMETER_SCHEMA
    assert validate_params(schema, {"vm": _VM, "pci_id": _PCI_ID}) == []
    assert validate_params(schema, {"vm": _VM}) != []
    both = {"vm": _VM, "pci_id": _PCI_ID, "dynamic_device": {"vendor_id": 1, "device_id": 2}}
    assert validate_params(schema, both) != []
    assert validate_params(schema, {"vm": _VM, "pci_id": "3b:00.0"}) != []


# ===========================================================================
# vm.pci_device.remove
# ===========================================================================


async def test_remove_sends_the_device_as_read_and_confirms_it_is_gone(
    gate: GateRecorder,
) -> None:
    conn = PciConsoleFake()
    device = _direct_device(13000, _PCI_ID)
    conn.vm(devices=[device])
    out = await _run(vm_pci_device_remove_composite, conn, device_key=13000)

    assert out["status"] == "removed", out
    [(_, body)] = conn.calls_to("ReconfigVM_Task")
    assert body["spec"]["deviceChange"] == [
        {"_typeName": "VirtualDeviceConfigSpec", "operation": "remove", "device": device}
    ]
    assert gate.calls[0]["op_id"] == _RECONFIG_SUB_OP
    assert gate.calls[0]["params"]["device_key"] == 13000
    assert out["device"]["pci_id"] == _PCI_ID
    assert out["pci_devices"] == []
    Draft202012Validator(VM_PCI_DEVICE_REMOVE_RESPONSE_SCHEMA).validate(out)


async def test_remove_unknown_key_sends_nothing(gate: GateRecorder) -> None:
    conn = PciConsoleFake()
    conn.vm(devices=[_direct_device(13000, _PCI_ID)])
    out = await _run(vm_pci_device_remove_composite, conn, device_key=2000)
    assert out["status"] == "unchanged"
    assert gate.calls == []
    _no_writes(conn)


async def test_remove_refuses_a_running_vm(gate: GateRecorder) -> None:
    conn = PciConsoleFake()
    conn.vm(power="poweredOn", devices=[_direct_device(13000, _PCI_ID)])
    out = await _run(vm_pci_device_remove_composite, conn, device_key=13000)
    assert out["status"] == "refused"
    _no_writes(conn)


async def test_remove_task_fault_reaches_the_answer(gate: GateRecorder) -> None:
    conn = PciConsoleFake()
    conn.vm(devices=[_direct_device(13000, _PCI_ID)])
    conn.task_faults["ReconfigVM_Task"] = ("InvalidPowerState", "The VM is running")
    with pytest.raises(RuntimeError, match=r"InvalidPowerState.*The VM is running"):
        await _run(vm_pci_device_remove_composite, conn, device_key=13000)


async def test_pci_previews_show_the_vm_and_the_device() -> None:
    conn = PciConsoleFake()
    conn.vm(devices=[_direct_device(13000, _PCI_ID)])
    add = await _PREVIEW_BUILDERS[_ADD_OP](preview_ctx({"vm": _VM, "pci_id": _PCI_ID}, conn))
    assert add is not None
    assert add["power_state"] == "poweredOff"
    assert add["current_pci_devices"][0]["device_key"] == 13000
    remove = await _PREVIEW_BUILDERS[_REMOVE_OP](
        preview_ctx({"vm": _VM, "device_key": 13000}, conn)
    )
    assert remove is not None
    assert remove["device"]["pci_id"] == _PCI_ID
    _no_writes(conn)


# ===========================================================================
# vm.screenshot
# ===========================================================================


async def test_screenshot_reads_the_png_through_the_datastore_file_access(
    gate: GateRecorder,
) -> None:
    conn = PciConsoleFake()
    conn.vm(power="poweredOn")
    out = await _run(vm_screenshot_composite, conn)

    assert out["status"] == "taken", out
    assert [c["op_id"] for c in gate.calls] == [_SCREENSHOT_SUB_OP]
    [(path, _)] = conn.calls_to("CreateScreenshot_Task")
    assert path == f"/VirtualMachine/{_VM}/CreateScreenshot_Task"
    [request] = conn.file_requests
    assert request.method == "GET"
    assert request.url.path == "/folder/app-vm/app-vm-1.png"
    assert request.url.params["dcPath"] == "region/dc-a"
    assert request.url.params["dsName"] == "ds-a"
    assert request.headers["cookie"] == 'vmware_soap_session="<soap-session>"'
    assert conn.soap_logins == 1
    assert out["file"] == _SCREENSHOT_PATH
    assert out["size_bytes"] == len(_PNG)
    assert out["media_type"] == "image/png"
    assert out["encoding"] == "base64"
    assert base64.b64decode("".join(out["content_lines"])) == _PNG
    Draft202012Validator(VM_SCREENSHOT_RESPONSE_SCHEMA).validate(out)


async def test_screenshot_refuses_a_vm_that_is_not_running(gate: GateRecorder) -> None:
    conn = PciConsoleFake()
    conn.vm(power="poweredOff")
    out = await _run(vm_screenshot_composite, conn)
    assert out["status"] == "refused"
    assert gate.calls == []
    _no_writes(conn)
    assert conn.file_requests == []


async def test_screenshot_over_the_cap_is_refused_not_cut(gate: GateRecorder) -> None:
    conn = PciConsoleFake()
    conn.vm(power="poweredOn")
    conn.file_body = b"\x00" * 200
    with pytest.raises(RuntimeError, match="over the limit of 100 bytes"):
        await _run(vm_screenshot_composite, conn, max_bytes=100)


async def test_screenshot_refuses_an_encoded_file(gate: GateRecorder) -> None:
    conn = PciConsoleFake()
    conn.vm(power="poweredOn")
    conn.file_headers = {"Content-Encoding": "gzip"}
    with pytest.raises(RuntimeError, match="Content-Encoding"):
        await _run(vm_screenshot_composite, conn)


async def test_screenshot_file_read_error_names_the_status(gate: GateRecorder) -> None:
    conn = PciConsoleFake()
    conn.vm(power="poweredOn")
    conn.file_status = 404
    with pytest.raises(RuntimeError, match="HTTP 404"):
        await _run(vm_screenshot_composite, conn)


async def test_screenshot_task_fault_reaches_the_answer(gate: GateRecorder) -> None:
    conn = PciConsoleFake()
    conn.vm(power="poweredOn")
    conn.task_faults["CreateScreenshot_Task"] = ("InvalidState", "No console")
    with pytest.raises(RuntimeError, match=r"InvalidState.*No console"):
        await _run(vm_screenshot_composite, conn)
    assert conn.file_requests == []


async def test_screenshot_parked_gate_sends_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_write, "enforce_subop_policy", GateRecorder(parked(_SCREENSHOT_SUB_OP)))
    conn = PciConsoleFake()
    conn.vm(power="poweredOn")
    out = await _run(vm_screenshot_composite, conn)
    assert out.status == "awaiting_approval"
    _no_writes(conn)


async def test_screenshot_timeout_is_not_confirmed(
    gate: GateRecorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(_vm_console, "_TASK_TIMEOUT_SECONDS", 0.0)
    conn = PciConsoleFake()
    conn.vm(power="poweredOn")
    conn.running.add("CreateScreenshot_Task")
    out = await _run(vm_screenshot_composite, conn)
    assert out["status"] == "timeout"
    assert out["issues"][0]["severity"] == "error"
    assert conn.file_requests == []


# ===========================================================================
# vm.keys.send
# ===========================================================================


async def test_keys_send_sends_the_hid_codes_of_the_listed_keys(gate: GateRecorder) -> None:
    conn = PciConsoleFake()
    conn.vm(power="poweredOn")
    out = await _run(vm_keys_send_composite, conn, keys=["ctrl_alt_del", "f12", "enter"])

    assert out["status"] == "sent", out
    [(path, body)] = conn.calls_to("PutUsbScanCodes")
    assert path == f"/VirtualMachine/{_VM}/PutUsbScanCodes"
    assert body["spec"]["_typeName"] == "UsbScanCodeSpec"
    assert body["spec"]["keyEvents"] == [
        {
            "_typeName": "UsbScanCodeSpecKeyEvent",
            "usbHidCode": (0x4C << 16) | 7,
            "modifiers": {
                "_typeName": "UsbScanCodeSpecModifierType",
                "leftControl": True,
                "leftAlt": True,
            },
        },
        {"_typeName": "UsbScanCodeSpecKeyEvent", "usbHidCode": (0x45 << 16) | 7},
        {"_typeName": "UsbScanCodeSpecKeyEvent", "usbHidCode": (0x28 << 16) | 7},
    ]
    assert gate.calls[0]["op_id"] == _KEYS_SUB_OP
    assert out["keys_sent"] == 3
    assert out["power_state"] == "poweredOn"
    Draft202012Validator(VM_KEYS_SEND_RESPONSE_SCHEMA).validate(out)


@pytest.mark.parametrize("keys", [["a"], ["enter", "ls -la\n"], [], "enter", [1]])
async def test_keys_send_refuses_a_key_off_the_list_with_no_vendor_call(
    gate: GateRecorder, keys: Any
) -> None:
    conn = PciConsoleFake()
    conn.vm(power="poweredOn")
    out = await _run(vm_keys_send_composite, conn, keys=keys)
    assert out["status"] == "invalid_request"
    assert conn.calls == []
    assert gate.calls == []


def test_keys_parameter_schema_allows_only_special_keys() -> None:
    enum = VM_KEYS_SEND_PARAMETER_SCHEMA["properties"]["keys"]["items"]["enum"]
    assert not [k for k in enum if len(k) == 1]
    assert validate_params(VM_KEYS_SEND_PARAMETER_SCHEMA, {"vm": _VM, "keys": ["esc"]}) == []
    assert validate_params(VM_KEYS_SEND_PARAMETER_SCHEMA, {"vm": _VM, "keys": ["x"]}) != []


async def test_keys_send_refuses_a_vm_that_is_not_running(gate: GateRecorder) -> None:
    conn = PciConsoleFake()
    conn.vm(power="suspended")
    out = await _run(vm_keys_send_composite, conn, keys=["enter"])
    assert out["status"] == "refused"
    _no_writes(conn)


async def test_keys_send_vendor_fault_reaches_the_answer(gate: GateRecorder) -> None:
    conn = PciConsoleFake()
    conn.vm(power="poweredOn")
    conn.keys_fault = http_status_error(
        500, json.dumps({"_typeName": "InvalidState", "faultstring": "No keyboard"})
    )
    with pytest.raises(RuntimeError, match=r"InvalidState.*No keyboard"):
        await _run(vm_keys_send_composite, conn, keys=["enter"])


async def test_keys_send_parked_gate_sends_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_write, "enforce_subop_policy", GateRecorder(parked(_KEYS_SUB_OP)))
    conn = PciConsoleFake()
    conn.vm(power="poweredOn")
    out = await _run(vm_keys_send_composite, conn, keys=["enter"])
    assert out.status == "awaiting_approval"
    _no_writes(conn)


async def test_console_previews_name_the_vm_and_keys() -> None:
    conn = PciConsoleFake()
    shot = await _PREVIEW_BUILDERS[_SCREENSHOT_OP](preview_ctx({"vm": _VM}, conn))
    keys = await _PREVIEW_BUILDERS[_KEYS_OP](preview_ctx({"vm": _VM, "keys": ["esc"]}, conn))
    assert shot is not None and shot["vm"] == _VM
    assert keys is not None and keys["keys"] == ["esc"]
    assert conn.calls == []


# ===========================================================================
# Registration and the governed flow through the dispatcher
# ===========================================================================

_RISK = {
    _ADD_OP: "dangerous",
    _REMOVE_OP: "dangerous",
    _SCREENSHOT_OP: "caution",
    _KEYS_OP: "dangerous",
}
_TENANT_ID = UUID("00000000-0000-0000-0000-000000004338")
_TARGET_NAME = "example-vcenter"


def test_every_operation_is_a_change_that_waits_for_approval() -> None:
    specs = {spec.op_id: spec for spec in _COMPOSITES if spec.op_id in _RISK}
    assert set(specs) == set(_RISK)
    for op_id, level in _RISK.items():
        assert specs[op_id].safety_level == level, op_id
        assert specs[op_id].requires_approval is True, op_id
        assert specs[op_id].group_key == "vm", op_id


@pytest.fixture
def _dispatch_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    reset_dispatcher_caches()
    clear_registry()
    yield
    reset_dispatcher_caches()
    clear_registry()
    get_settings.cache_clear()


def _embedding_stub() -> AsyncMock:
    service = AsyncMock()
    service.encode_one.return_value = [0.1] * 384
    service.encode.return_value = [[0.1] * 384]
    service.dimension = 384
    return service


def _person(sub: str, kind: PrincipalKind = PrincipalKind.USER) -> Operator:
    return Operator(
        sub=sub,
        name="Console Test",
        email=None,
        raw_jwt="<test-raw-jwt>",
        tenant_id=_TENANT_ID,
        tenant_role=TenantRole.OPERATOR,
        principal_kind=kind,
    )


async def _bootstrap(conn: PciConsoleFake, version: str = "9.0") -> None:
    register_connector_v2(
        product="vmware", version=version, impl_id="vmware-rest", cls=VmwareRestConnector
    )
    _CONNECTOR_INSTANCE_CACHE[VmwareRestConnector] = conn  # type: ignore[assignment]
    register = (
        register_vmware_composite_operations_8_0
        if version == "8.0"
        else register_vmware_composite_operations
    )
    await register(embedding_service=_embedding_stub())
    async with get_sessionmaker()() as s:
        s.add(
            TargetORM(
                id=uuid.uuid4(),
                tenant_id=_TENANT_ID,
                name=_TARGET_NAME,
                aliases=[],
                product="vmware",
                host="vcenter.example.test",
                port=443,
                fqdn=None,
                secret_ref=None,
                auth_model="shared_service_account",
                vpn_required=False,
                extras={},
                fingerprint={"version": version},
                preferred_impl_id="vmware-rest",
                notes="seeded by test_connectors_vmware_rest_composites_vm_pci_console",
            )
        )
        await s.commit()


class _FakeTarget:
    """The resolved-target shape ``dispatch`` reads (vCenter 9.0, vmware-rest)."""

    def __init__(self) -> None:
        self.product = "vmware"
        self.fingerprint = type("_Fingerprint", (), {"version": "9.0"})()
        self.preferred_impl_id: str | None = "vmware-rest"
        self.id: UUID = uuid.uuid4()
        self.tenant_id: UUID = _TENANT_ID
        self.name = _TARGET_NAME
        self.host = "vcenter.example.test"
        self.port = 443
        self.auth_model = "shared_service_account"


@pytest.mark.usefixtures("_dispatch_env")
@pytest.mark.parametrize("version", ["8.0", "9.0"])
async def test_registered_on_both_vcenter_connectors_with_their_risk_level(version: str) -> None:
    await _bootstrap(PciConsoleFake(), version)
    async with get_sessionmaker()() as s:
        rows = (
            (
                await s.execute(
                    select(EndpointDescriptor).where(EndpointDescriptor.op_id.in_(tuple(_RISK)))
                )
            )
            .scalars()
            .all()
        )
    by_op = {row.op_id: row for row in rows if row.version == version}
    assert set(by_op) == set(_RISK)
    for op_id, level in _RISK.items():
        assert by_op[op_id].safety_level == level, op_id
        assert by_op[op_id].requires_approval is True, op_id


@pytest.mark.usefixtures("_dispatch_env")
@pytest.mark.parametrize("version", ["8.0", "9.0"])
async def test_a_persons_key_send_waits_for_approval(version: str) -> None:
    conn = PciConsoleFake()
    conn.vm(power="poweredOn")
    await _bootstrap(conn, version)
    call = await call_operation(
        _person("op-requester"),
        {
            "connector_id": f"vmware-rest-{version}",
            "op_id": _KEYS_OP,
            "target": _TARGET_NAME,
            "params": {"vm": _VM, "keys": ["enter"]},
        },
    )
    assert call["status"] == "awaiting_approval", call
    _no_writes(conn)


@pytest.mark.usefixtures("_dispatch_env")
async def test_a_key_off_the_list_never_reaches_vcenter() -> None:
    conn = PciConsoleFake()
    conn.vm(power="poweredOn")
    await _bootstrap(conn)
    call = await call_operation(
        _person("op-requester"),
        {
            "connector_id": "vmware-rest-9.0",
            "op_id": _KEYS_OP,
            "target": _TARGET_NAME,
            "params": {"vm": _VM, "keys": ["r"]},
        },
    )
    assert call["status"] not in ("ok", "awaiting_approval"), call
    assert conn.calls == []


@pytest.mark.usefixtures("_dispatch_env")
async def test_an_agent_pci_add_is_refused_without_a_grant() -> None:
    conn = PciConsoleFake()
    conn.vm()
    await _bootstrap(conn)
    result = await dispatch(
        operator=_person("agent-1", PrincipalKind.AGENT),
        connector_id="vmware-rest-9.0",
        op_id=_ADD_OP,
        target=_FakeTarget(),
        params={"vm": _VM, "pci_id": _PCI_ID},
    )
    assert result.status == "denied", result
    _no_writes(conn)
