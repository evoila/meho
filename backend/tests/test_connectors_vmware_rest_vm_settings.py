# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for the VM boot, upgrade and advanced-settings operations.

Six operations: the reads ``vmware.vm.boot.show`` and
``vmware.vm.advanced_settings.show``, and the changes
``vmware.composite.vm.boot.set``, ``vmware.composite.vm.hardware.upgrade``,
``vmware.composite.vm.tools.upgrade`` and
``vmware.composite.vm.advanced_settings.set``.

A fake connector answers with saved vSphere REST and VI-JSON responses and
changes its state the way vCenter does, so the read after each change sees
the result. The policy check of the write seam is replaced by a recorder, so
the tests show that a held write never reaches vCenter. The last tests run
the real connector over mocked HTTP on the vCenter 8 and 9 VI-JSON paths, and
check that a flight-recorder trace of these calls holds no secret setting value.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
import respx
from jsonschema import Draft202012Validator

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors import OperationResult
from meho_backplane.connectors.base import ConnectorResourceNotFoundError
from meho_backplane.connectors.schemas import AuthModel
from meho_backplane.connectors.vmware_rest import VmwareRestConnector, VsphereTargetLike
from meho_backplane.connectors.vmware_rest import trace as vmware_trace
from meho_backplane.connectors.vmware_rest import typed_ops_vm_advanced_settings as adv
from meho_backplane.connectors.vmware_rest import typed_ops_vm_boot as boot
from meho_backplane.connectors.vmware_rest.composites import _vm_advanced_settings as adv_set
from meho_backplane.connectors.vmware_rest.composites import _vm_boot as boot_set
from meho_backplane.connectors.vmware_rest.composites import _vm_change_common as common
from meho_backplane.connectors.vmware_rest.composites import _vm_upgrade as upgrade
from meho_backplane.connectors.vmware_rest.composites import _write
from meho_backplane.connectors.vmware_rest.composites._register import (
    _COMPOSITES,
    COMPOSITES_NOT_ON_8_0,
)
from meho_backplane.connectors.vmware_rest.typed_ops import VMWARE_TYPED_OPS
from meho_backplane.flight_recorder import capture
from meho_backplane.operations._preview import _PREVIEW_BUILDERS, PreviewContext
from meho_backplane.operations._validate import validate_params
from meho_backplane.operations.jsonflux_reducer import JsonFluxReducer
from meho_backplane.settings import get_settings

_VM = "vm-42"
_BASE = f"/api/vcenter/vm/{_VM}"
_RETRIEVE = "/PropertyCollector/propertyCollector/RetrievePropertiesEx"
_TASK = "task-901"


def _operator() -> Operator:
    return Operator(
        sub="op-vm-settings",
        name="VM Settings Test",
        email=None,
        raw_jwt="<test-raw-jwt>",
        tenant_id=UUID("00000000-0000-0000-0000-00000000a0c3"),
        tenant_role=TenantRole.OPERATOR,
    )


class _Target:
    name = "vc-test"


def _status_error(method: str, path: str, status: int, body: dict[str, Any]) -> httpx.HTTPError:
    request = httpx.Request(method, f"https://vc.test.invalid{path}")
    response = httpx.Response(status, json=body, request=request)
    return httpx.HTTPStatusError(f"HTTP {status}", request=request, response=response)


def _box(value: str) -> dict[str, Any]:
    return {"_typeName": "string", "_value": value}


class _Fake:
    """Saved vSphere answers for one VM; writes change the state like vCenter does."""

    impl_id = "vmware-rest"
    version = "9.0"

    def __init__(self) -> None:
        self.boot: dict[str, Any] = {
            "type": "BIOS",
            "delay": 0,
            "retry": False,
            "retry_delay": 10000,
            "enter_setup_mode": False,
        }
        self.boot_devices: list[dict[str, Any]] = [
            {"type": "DISK", "disks": ["2000"]},
            {"type": "ETHERNET", "nic": "4000"},
        ]
        self.hardware: dict[str, Any] = {
            "version": "VMX_19",
            "upgrade_policy": "NEVER",
            "upgrade_status": "NONE",
        }
        self.tools: dict[str, Any] = {
            "version": "12.1.0",
            "version_number": 12352,
            "version_status": "TOO_OLD_SUPPORTED",
            "run_state": "RUNNING",
            "install_type": "MSI",
            "upgrade_policy": "MANUAL",
            "auto_update_supported": True,
        }
        self.installer: dict[str, Any] = {"is_connected": False}
        self.secure_boot = False
        self.extra_config: dict[str, str] = {
            "disk.EnableUUID": "FALSE",
            "guestinfo.userdata": "<base64-user-data>",
            "isolation.tools.copy.disable": "TRUE",
            "svga.present": "TRUE",
        }
        self.vm_exists = True
        self.extra_config_unreadable = False
        self.task_state = "success"
        self.task_error: dict[str, Any] | None = None
        self.ignore_keys: set[str] = set()
        self.write_errors: dict[str, httpx.HTTPError] = {}
        self.get_errors: dict[str, int] = {}
        self.writes: list[tuple[str, str, Any]] = []
        self.reconfigs: list[dict[str, Any]] = []

    # -- REST ---------------------------------------------------------------
    async def mount_op_path(self, target: Any, path: str, operator: Operator) -> str:
        return f"/api{path}"

    async def adapt_op_query(self, target: Any, query: Any, operator: Operator) -> Any:
        return query

    async def _get_json(
        self, target: Any, path: str, *, operator: Operator, params: Any = None
    ) -> Any:
        if path in self.get_errors:
            raise _status_error("GET", path, self.get_errors[path], {"error_type": "NOT_FOUND"})
        answers = {
            "/api/vcenter/vm": [{"vm": _VM, "name": "app-01"}],
            f"{_BASE}/hardware/boot": self.boot,
            f"{_BASE}/hardware/boot/device": self.boot_devices,
            f"{_BASE}/hardware": self.hardware,
            f"{_BASE}/tools": self.tools,
            f"{_BASE}/tools/installer": self.installer,
        }
        if path not in answers:
            raise AssertionError(f"unexpected GET {path!r}")
        return copy.deepcopy(answers[path])

    async def _post_json(
        self,
        target: Any,
        path: str,
        *,
        operator: Operator,
        verb: str = "POST",
        json: Any = None,
        timeout: Any = None,
    ) -> Any:
        self.writes.append((verb, path, copy.deepcopy(json)))
        if path in self.write_errors:
            raise self.write_errors[path]
        body = json or {}
        if path == f"{_BASE}/hardware/boot":
            self.boot.update(body)
        elif path == f"{_BASE}/hardware/boot/device":
            self.boot_devices = body["devices"]
        elif path == f"{_BASE}/hardware?action=upgrade":
            self.hardware["version"] = body.get("version", "VMX_21")
        elif path == f"{_BASE}/tools?action=upgrade":
            self.tools["version_status"] = "CURRENT"
        elif path == f"{_BASE}/tools/installer?action=connect":
            self.installer["is_connected"] = True
        return None

    # -- VI-JSON ------------------------------------------------------------
    async def _post_vmomi_json(
        self,
        target: Any,
        path: str,
        *,
        operator: Operator,
        json: Any = None,
        promote_managed_object_not_found: bool = False,
    ) -> Any:
        if path.endswith("/ReconfigVM_Task"):
            self.reconfigs.append(copy.deepcopy(json))
            if self.task_state == "success":
                self._apply(json["spec"])
            return {"_typeName": "ManagedObjectReference", "type": "Task", "value": _TASK}
        assert path == _RETRIEVE, path
        prop_spec = json["specSet"][0]["propSet"][0]
        if prop_spec["type"] == "Task":
            info: dict[str, Any] = {"_typeName": "TaskInfo", "state": self.task_state}
            if self.task_error is not None:
                info["error"] = self.task_error
            return {
                "objects": [
                    {
                        "obj": {"type": "Task", "value": _TASK},
                        "propSet": [{"name": "info", "val": info}],
                    }
                ]
            }
        if not self.vm_exists:
            raise ConnectorResourceNotFoundError([_VM], "ManagedObjectNotFound")
        return {"objects": [self._vm_object(prop_spec["pathSet"])]}

    def _vm_object(self, paths: list[str]) -> dict[str, Any]:
        values = {
            "name": _box("app-01"),
            "config.bootOptions": {
                "_typeName": "VirtualMachineBootOptions",
                "bootDelay": 0,
                "efiSecureBootEnabled": self.secure_boot,
            },
            "config.extraConfig": {
                "_typeName": "ArrayOfOptionValue",
                "_value": [
                    {"_typeName": "OptionValue", "key": k, "value": _box(v)}
                    for k, v in self.extra_config.items()
                ],
            },
        }
        obj: dict[str, Any] = {
            "obj": {"type": "VirtualMachine", "value": _VM},
            "propSet": [{"name": p, "val": values[p]} for p in paths],
        }
        if self.extra_config_unreadable and "config.extraConfig" in paths:
            obj["propSet"] = [e for e in obj["propSet"] if e["name"] != "config.extraConfig"]
            obj["missingSet"] = [
                {"path": "config.extraConfig", "fault": {"_typeName": "NoPermission"}}
            ]
        return obj

    def _apply(self, spec: dict[str, Any]) -> None:
        options = spec.get("bootOptions")
        if options is not None:
            self.secure_boot = options["efiSecureBootEnabled"]
        for option in spec.get("extraConfig", []):
            key, value = option["key"], option["value"]["_value"]
            if key in self.ignore_keys:
                continue
            if value == "":
                self.extra_config.pop(key, None)
            else:
                self.extra_config[key] = value


class _Gate:
    def __init__(self, verdict: OperationResult | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self._verdict = verdict

    async def __call__(self, **kwargs: Any) -> OperationResult | None:
        self.calls.append(kwargs)
        return self._verdict


@pytest.fixture
def gate(monkeypatch: pytest.MonkeyPatch) -> _Gate:
    recorder = _Gate()
    monkeypatch.setattr(_write, "enforce_subop_policy", recorder)
    return recorder


@pytest.fixture
def held(monkeypatch: pytest.MonkeyPatch) -> OperationResult:
    verdict = OperationResult(
        status="awaiting_approval", op_id="held-write", result=None, duration_ms=1.0
    )
    monkeypatch.setattr(_write, "enforce_subop_policy", _Gate(verdict))
    return verdict


def _spec(op_id: str) -> Any:
    return next(s for s in _COMPOSITES if s.op_id == op_id)


async def _run(op_id: str, conn: _Fake, **params: Any) -> Any:
    handler = _spec(op_id).handler
    return await handler(operator=_operator(), target=_Target(), params=params, connector=conn)


def _check(schema: dict[str, Any] | None, out: dict[str, Any]) -> None:
    Draft202012Validator(schema or {}).validate(out)


def _preview_ctx(conn: _Fake | None, **params: Any) -> PreviewContext:
    return PreviewContext(
        descriptor=None,  # type: ignore[arg-type]
        connector_instance=conn,  # type: ignore[arg-type]
        operator=_operator(),
        target=_Target(),
        params=params,
    )


_BOOT_SET = "vmware.composite.vm.boot.set"
_HW_UPGRADE = "vmware.composite.vm.hardware.upgrade"
_TOOLS_UPGRADE = "vmware.composite.vm.tools.upgrade"
_ADV_SET = "vmware.composite.vm.advanced_settings.set"
_CHANGES = (_BOOT_SET, _HW_UPGRADE, _TOOLS_UPGRADE, _ADV_SET)


# ---------------------------------------------------------------------------
# Metadata: risk level and approval of every operation in the table
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "op", [boot.VMWARE_VM_BOOT_SHOW_OP, adv.VMWARE_VM_ADVANCED_SETTINGS_SHOW_OP]
)
def test_reads_are_safe_and_run_at_once(op: Any) -> None:
    assert op in VMWARE_TYPED_OPS
    assert op.safety_level == "safe"
    assert op.requires_approval is False
    assert callable(getattr(VmwareRestConnector, op.handler_attr))


@pytest.mark.parametrize("op_id", _CHANGES)
def test_changes_are_dangerous_wait_for_approval_and_are_on_both_catalogs(op_id: str) -> None:
    spec = _spec(op_id)
    assert spec.safety_level == "dangerous"
    assert spec.requires_approval is True
    assert spec.group_key == "vm"
    assert op_id not in COMPOSITES_NOT_ON_8_0
    assert op_id in _PREVIEW_BUILDERS


@pytest.mark.parametrize(
    ("schema", "params", "valid"),
    [
        (boot_set.VM_BOOT_SET_PARAMETER_SCHEMA, {"vm": _VM, "delay_ms": 0}, True),
        (boot_set.VM_BOOT_SET_PARAMETER_SCHEMA, {"vm": _VM}, False),
        (boot_set.VM_BOOT_SET_PARAMETER_SCHEMA, {"name": "app-01", "delay_ms": 1}, False),
        (boot_set.VM_BOOT_SET_PARAMETER_SCHEMA, {"vm": _VM, "firmware": "UEFI"}, False),
        (
            boot_set.VM_BOOT_SET_PARAMETER_SCHEMA,
            {"vm": _VM, "boot_order": [{"type": "USB"}]},
            False,
        ),
        (upgrade.VM_HARDWARE_UPGRADE_PARAMETER_SCHEMA, {"vm": _VM, "version": "21"}, False),
        (upgrade.VM_TOOLS_UPGRADE_PARAMETER_SCHEMA, {"vm": _VM, "mode": "install"}, False),
        (adv_set.VM_ADVANCED_SETTINGS_SET_PARAMETER_SCHEMA, {"vm": _VM}, False),
        (
            adv_set.VM_ADVANCED_SETTINGS_SET_PARAMETER_SCHEMA,
            {"vm": _VM, "settings": [{"key": "a b", "value": "x"}]},
            False,
        ),
        (
            adv_set.VM_ADVANCED_SETTINGS_SET_PARAMETER_SCHEMA,
            {"vm": _VM, "remove": ["svga.present"]},
            True,
        ),
    ],
)
def test_parameter_schemas(schema: dict[str, Any], params: dict[str, Any], valid: bool) -> None:
    errors = validate_params(schema, params)
    assert (errors == []) is valid, errors


# ---------------------------------------------------------------------------
# vmware.vm.boot.show
# ---------------------------------------------------------------------------


async def test_boot_show_reads_rest_and_secure_boot() -> None:
    out = await boot.vm_boot_show_impl(_Fake(), _operator(), _Target(), {"name": "app-01"})  # type: ignore[arg-type]
    _check(boot.VMWARE_VM_BOOT_SHOW_OP.response_schema, out)
    assert out["vm"] == _VM
    assert out["vm_name"] == "app-01"
    assert out["firmware"] == "BIOS"
    assert out["efi_secure_boot"] is False
    assert out["delay_ms"] == 0
    assert out["retry_delay_ms"] == 10000
    assert out["boot_order"] == [
        {"type": "DISK", "nic": None, "disks": ["2000"]},
        {"type": "ETHERNET", "nic": "4000", "disks": None},
    ]
    assert out["read_notes"] == []


async def test_boot_show_unknown_vm_is_not_found() -> None:
    conn = _Fake()
    conn.get_errors[f"{_BASE}/hardware/boot"] = 404
    with pytest.raises(ConnectorResourceNotFoundError):
        await boot.vm_boot_show_impl(conn, _operator(), _Target(), {"vm": _VM})  # type: ignore[arg-type]


async def test_boot_show_failed_boot_order_is_null_with_a_note() -> None:
    conn = _Fake()
    conn.get_errors[f"{_BASE}/hardware/boot/device"] = 503
    out = await boot.vm_boot_show_impl(conn, _operator(), _Target(), {"vm": _VM})  # type: ignore[arg-type]
    assert out["boot_order"] is None
    assert out["firmware"] == "BIOS"
    assert len(out["read_notes"]) == 1


# ---------------------------------------------------------------------------
# vmware.composite.vm.boot.set
# ---------------------------------------------------------------------------


async def test_boot_set_sends_only_what_differs_and_reads_back(gate: _Gate) -> None:
    conn = _Fake()
    out = await _run(
        _BOOT_SET,
        conn,
        vm=_VM,
        delay_ms=5000,
        retry=False,
        boot_order=[{"type": "CDROM"}, {"type": "DISK", "disks": ["2000"]}],
    )
    _check(_spec(_BOOT_SET).response_schema, out)
    assert out["status"] == "set"
    assert out["applied"] == ["boot_settings", "boot_order"]
    assert conn.writes == [
        ("PATCH", f"{_BASE}/hardware/boot", {"delay": 5000}),
        (
            "PUT",
            f"{_BASE}/hardware/boot/device",
            {"devices": [{"type": "CDROM"}, {"type": "DISK", "disks": ["2000"]}]},
        ),
    ]
    assert conn.reconfigs == []
    assert out["before"]["delay_ms"] == 0
    assert out["after"]["delay_ms"] == 5000
    assert [g["op_id"] for g in gate.calls] == [
        "PATCH:/vcenter/vm/{vm}/hardware/boot",
        "PUT:/vcenter/vm/{vm}/hardware/boot/device",
    ]


async def test_boot_set_switches_to_efi_then_turns_secure_boot_on(gate: _Gate) -> None:
    conn = _Fake()
    out = await _run(_BOOT_SET, conn, vm=_VM, firmware="EFI", efi_secure_boot=True)
    assert out["status"] == "set"
    assert out["applied"] == ["boot_settings", "efi_secure_boot"]
    assert out["task"] == _TASK
    assert conn.reconfigs == [
        {
            "spec": {
                "_typeName": "VirtualMachineConfigSpec",
                "bootOptions": {
                    "_typeName": "VirtualMachineBootOptions",
                    "efiSecureBootEnabled": True,
                },
            }
        }
    ]
    assert out["after"]["firmware"] == "EFI"
    assert out["after"]["efi_secure_boot"] is True


async def test_boot_set_turns_secure_boot_off_before_the_switch_to_bios(gate: _Gate) -> None:
    conn = _Fake()
    conn.boot["type"] = "EFI"
    conn.secure_boot = True
    out = await _run(_BOOT_SET, conn, vm=_VM, firmware="BIOS", efi_secure_boot=False)
    assert out["status"] == "set"
    assert out["applied"] == ["efi_secure_boot", "boot_settings"]


async def test_boot_set_bios_with_secure_boot_still_on_sends_nothing(gate: _Gate) -> None:
    conn = _Fake()
    conn.boot["type"] = "EFI"
    conn.secure_boot = True
    out = await _run(_BOOT_SET, conn, vm=_VM, firmware="BIOS")
    assert out["status"] == "invalid_request"
    assert "efi_secure_boot false" in out["guidance"]
    assert conn.writes == [] and conn.reconfigs == []


async def test_boot_set_disk_entry_without_disks_matches_any_disks(gate: _Gate) -> None:
    conn = _Fake()
    first = await _run(_BOOT_SET, conn, vm=_VM, boot_order=[{"type": "CDROM"}, {"type": "DISK"}])
    assert first["status"] == "set"
    conn.boot_devices = [{"type": "CDROM"}, {"type": "DISK", "disks": ["2000"]}]
    conn.writes.clear()
    again = await _run(_BOOT_SET, conn, vm=_VM, boot_order=[{"type": "CDROM"}, {"type": "DISK"}])
    assert again["status"] == "unchanged"
    assert conn.writes == []


async def test_boot_set_secure_boot_on_bios_sends_nothing(gate: _Gate) -> None:
    conn = _Fake()
    out = await _run(_BOOT_SET, conn, vm=_VM, efi_secure_boot=True)
    assert out["status"] == "invalid_request"
    assert conn.writes == [] and conn.reconfigs == []


async def test_boot_set_already_matching_sends_nothing(gate: _Gate) -> None:
    conn = _Fake()
    out = await _run(_BOOT_SET, conn, vm=_VM, delay_ms=0, firmware="BIOS")
    assert out["status"] == "unchanged"
    assert conn.writes == [] and gate.calls == []


async def test_boot_set_waits_for_approval_and_sends_nothing(held: OperationResult) -> None:
    conn = _Fake()
    assert await _run(_BOOT_SET, conn, vm=_VM, delay_ms=3000) is held
    assert conn.writes == []


async def test_boot_set_refused_by_vcenter_names_the_error(gate: _Gate) -> None:
    conn = _Fake()
    conn.write_errors[f"{_BASE}/hardware/boot"] = _status_error(
        "PATCH",
        f"{_BASE}/hardware/boot",
        400,
        {
            "error_type": "NOT_ALLOWED_IN_CURRENT_STATE",
            "messages": [{"default_message": "The VM is powered on."}],
        },
    )
    out = await _run(_BOOT_SET, conn, vm=_VM, firmware="EFI")
    assert out["status"] == "refused"
    assert out["error_type"] == "NOT_ALLOWED_IN_CURRENT_STATE"
    assert "The VM is powered on." in out["guidance"]
    assert out["applied"] == []


async def test_boot_set_task_fault_raises_with_the_fault_type_and_text(gate: _Gate) -> None:
    conn = _Fake()
    conn.boot["type"] = "EFI"
    conn.task_state = "error"
    conn.task_error = {
        "_typeName": "LocalizedMethodFault",
        "fault": {"_typeName": "InvalidPowerState"},
        "localizedMessage": "The attempted operation cannot be performed in the current state.",
    }
    with pytest.raises(RuntimeError) as excinfo:
        await _run(_BOOT_SET, conn, vm=_VM, efi_secure_boot=True)
    assert "InvalidPowerState" in str(excinfo.value)
    assert "cannot be performed in the current state" in str(excinfo.value)


async def test_boot_set_task_timeout(gate: _Gate, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(common, "TASK_TIMEOUT_SECONDS", 0.0)
    conn = _Fake()
    conn.boot["type"] = "EFI"
    conn.task_state = "running"
    out = await _run(_BOOT_SET, conn, vm=_VM, efi_secure_boot=True)
    assert out["status"] == "timeout"
    assert out["issues"][0]["severity"] == "error"


async def test_boot_set_rejects_a_vm_that_is_not_an_id(gate: _Gate) -> None:
    with pytest.raises(ValueError):
        await _run(_BOOT_SET, _Fake(), vm="../vm-42", delay_ms=1)


async def test_boot_set_preview_shows_the_planned_steps() -> None:
    builder = _PREVIEW_BUILDERS[_BOOT_SET]
    preview = await builder(_preview_ctx(_Fake(), vm=_VM, delay_ms=5000))
    assert preview is not None
    assert preview["current"]["delay_ms"] == 0
    assert preview["changes"]["patch"] == {"delay": 5000}
    assert "boot settings" in preview["summary"]


# ---------------------------------------------------------------------------
# vmware.composite.vm.hardware.upgrade
# ---------------------------------------------------------------------------


async def test_hardware_upgrade_to_the_newest_version(gate: _Gate) -> None:
    conn = _Fake()
    out = await _run(_HW_UPGRADE, conn, vm=_VM)
    _check(_spec(_HW_UPGRADE).response_schema, out)
    assert out["status"] == "upgraded"
    assert conn.writes == [("POST", f"{_BASE}/hardware?action=upgrade", None)]
    assert out["before"]["version"] == "VMX_19"
    assert out["after"]["version"] == "VMX_21"


async def test_hardware_upgrade_to_a_given_version(gate: _Gate) -> None:
    conn = _Fake()
    out = await _run(_HW_UPGRADE, conn, vm=_VM, version="VMX_20")
    assert out["status"] == "upgraded"
    assert conn.writes == [("POST", f"{_BASE}/hardware?action=upgrade", {"version": "VMX_20"})]


@pytest.mark.parametrize(
    ("version", "status"), [("VMX_19", "unchanged"), ("VMX_15", "invalid_request")]
)
async def test_hardware_upgrade_same_or_older_sends_nothing(
    gate: _Gate, version: str, status: str
) -> None:
    conn = _Fake()
    out = await _run(_HW_UPGRADE, conn, vm=_VM, version=version)
    assert out["status"] == status
    assert conn.writes == []


async def test_hardware_upgrade_waits_for_approval(held: OperationResult) -> None:
    conn = _Fake()
    assert await _run(_HW_UPGRADE, conn, vm=_VM) is held
    assert conn.writes == []


async def test_hardware_upgrade_refused_for_a_running_vm(gate: _Gate) -> None:
    conn = _Fake()
    conn.write_errors[f"{_BASE}/hardware?action=upgrade"] = _status_error(
        "POST",
        f"{_BASE}/hardware?action=upgrade",
        400,
        {"error_type": "NOT_ALLOWED_IN_CURRENT_STATE", "messages": [{"default_message": "on"}]},
    )
    out = await _run(_HW_UPGRADE, conn, vm=_VM)
    assert out["status"] == "refused"
    assert out["error_type"] == "NOT_ALLOWED_IN_CURRENT_STATE"


async def test_hardware_upgrade_server_fault_raises(gate: _Gate) -> None:
    conn = _Fake()
    conn.write_errors[f"{_BASE}/hardware?action=upgrade"] = _status_error(
        "POST", f"{_BASE}/hardware?action=upgrade", 500, {"error_type": "ERROR"}
    )
    with pytest.raises(httpx.HTTPStatusError):
        await _run(_HW_UPGRADE, conn, vm=_VM)


async def test_hardware_upgrade_preview_says_it_cannot_be_undone() -> None:
    preview = await _PREVIEW_BUILDERS[_HW_UPGRADE](_preview_ctx(_Fake(), vm=_VM))
    assert preview is not None
    assert preview["current"]["version"] == "VMX_19"
    assert "cannot be undone" in preview["summary"]


# ---------------------------------------------------------------------------
# vmware.composite.vm.tools.upgrade
# ---------------------------------------------------------------------------


async def test_tools_upgrade_sends_the_upgrade_and_reads_back(gate: _Gate) -> None:
    conn = _Fake()
    out = await _run(_TOOLS_UPGRADE, conn, vm=_VM, command_line_options="/S")
    _check(_spec(_TOOLS_UPGRADE).response_schema, out)
    assert out["status"] == "upgraded"
    assert conn.writes == [
        ("POST", f"{_BASE}/tools?action=upgrade", {"command_line_options": "/S"})
    ]
    assert out["before"]["version_status"] == "TOO_OLD_SUPPORTED"
    assert out["after"]["version_status"] == "CURRENT"


async def test_tools_upgrade_still_running_is_upgrade_started(gate: _Gate) -> None:
    conn = _Fake()

    async def no_change(*args: Any, **kwargs: Any) -> None:
        conn.writes.append(("POST", args[1], kwargs.get("json")))

    conn._post_json = no_change  # type: ignore[method-assign]
    out = await _run(_TOOLS_UPGRADE, conn, vm=_VM)
    assert out["status"] == "upgrade_started"


async def test_tools_upgrade_current_sends_nothing(gate: _Gate) -> None:
    conn = _Fake()
    conn.tools["version_status"] = "CURRENT"
    out = await _run(_TOOLS_UPGRADE, conn, vm=_VM)
    assert out["status"] == "unchanged"
    assert conn.writes == []


async def test_tools_mount_installer(gate: _Gate) -> None:
    conn = _Fake()
    out = await _run(_TOOLS_UPGRADE, conn, vm=_VM, mode="mount_installer")
    _check(_spec(_TOOLS_UPGRADE).response_schema, out)
    assert out["status"] == "installer_mounted"
    assert conn.writes == [("POST", f"{_BASE}/tools/installer?action=connect", None)]
    assert out["after"] == {"installer_mounted": True}


async def test_tools_mount_installer_with_command_line_options_is_refused(gate: _Gate) -> None:
    conn = _Fake()
    out = await _run(
        _TOOLS_UPGRADE, conn, vm=_VM, mode="mount_installer", command_line_options="/S"
    )
    _check(_spec(_TOOLS_UPGRADE).response_schema, out)
    assert out["status"] == "invalid_request"
    assert conn.writes == [] and gate.calls == []


async def test_tools_mount_installer_already_mounted_sends_nothing(gate: _Gate) -> None:
    conn = _Fake()
    conn.installer["is_connected"] = True
    out = await _run(_TOOLS_UPGRADE, conn, vm=_VM, mode="mount_installer")
    assert out["status"] == "unchanged"
    assert conn.writes == []


async def test_tools_upgrade_waits_for_approval(held: OperationResult) -> None:
    conn = _Fake()
    assert await _run(_TOOLS_UPGRADE, conn, vm=_VM) is held
    assert conn.writes == []


async def test_tools_upgrade_refused_for_a_vm_that_is_off(gate: _Gate) -> None:
    conn = _Fake()
    conn.write_errors[f"{_BASE}/tools?action=upgrade"] = _status_error(
        "POST",
        f"{_BASE}/tools?action=upgrade",
        400,
        {
            "error_type": "NOT_ALLOWED_IN_CURRENT_STATE",
            "messages": [{"default_message": "The VM is not powered on."}],
        },
    )
    out = await _run(_TOOLS_UPGRADE, conn, vm=_VM)
    assert out["status"] == "refused"
    assert "not powered on" in out["guidance"]


async def test_tools_upgrade_preview_shows_the_tools_state() -> None:
    preview = await _PREVIEW_BUILDERS[_TOOLS_UPGRADE](_preview_ctx(_Fake(), vm=_VM))
    assert preview is not None
    assert preview["current"]["version_status"] == "TOO_OLD_SUPPORTED"


# ---------------------------------------------------------------------------
# vmware.vm.advanced_settings.show
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("secret_key", ["guestinfo.userdata", "guestinfo.ignition.config.data"])
async def test_advanced_settings_show_masks_secret_values(secret_key: str) -> None:
    conn = _Fake()
    conn.extra_config[secret_key] = conn.extra_config.pop("guestinfo.userdata")
    out = await adv.vm_advanced_settings_show_impl(conn, _operator(), _Target(), {"vm": _VM})  # type: ignore[arg-type]
    _check(adv.VMWARE_VM_ADVANCED_SETTINGS_SHOW_OP.response_schema, out)
    assert out["vm_name"] == "app-01"
    assert out["setting_count"] == 4
    assert out["masked_count"] == 1
    assert out["settings"][1] == {"key": secret_key, "value": None, "masked": True}
    assert "<base64-user-data>" not in json.dumps(out)
    assert [row["key"] for row in out["settings"]] == sorted(row["key"] for row in out["settings"])


async def test_advanced_settings_show_filters_by_prefix() -> None:
    out = await adv.vm_advanced_settings_show_impl(  # type: ignore[arg-type]
        _Fake(), _operator(), _Target(), {"vm": _VM, "key_prefix": "isolation."}
    )
    assert out["settings"] == [
        {"key": "isolation.tools.copy.disable", "value": "TRUE", "masked": False}
    ]


async def test_advanced_settings_show_unreadable_property_fails() -> None:
    conn = _Fake()
    conn.extra_config_unreadable = True
    with pytest.raises(RuntimeError):
        await adv.vm_advanced_settings_show_impl(conn, _operator(), _Target(), {"vm": _VM})  # type: ignore[arg-type]


async def test_advanced_settings_show_unknown_vm_is_not_found() -> None:
    conn = _Fake()
    conn.vm_exists = False
    with pytest.raises(ConnectorResourceNotFoundError):
        await adv.vm_advanced_settings_show_impl(conn, _operator(), _Target(), {"vm": _VM})  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("key", "secret"),
    [
        ("guestinfo.userdata", True),
        ("guestinfo.ovfEnv", True),
        ("guestinfo.admin_password", True),
        ("guestinfo.api-token", True),
        ("guestinfo.ssh.privateKey", True),
        ("guestinfo.ignition.config.data", True),
        ("guestinfo.Ignition.Config.Data", True),
        ("guestinfo.ignition.config.data.encoding", True),
        ("guestinfo.ignition.config.url", True),
        ("guestinfo.talos.config", True),
        ("GUESTINFO.TALOS.CONFIG", True),
        ("guestinfo.talos.platform", True),
        ("guestinfo.ignitionfoo", False),
        ("disk.EnableUUID", False),
        ("guestinfo.metadata.encoding", False),
        ("keyboard.typematicMinDelay", False),
    ],
)
def test_is_secret_key(key: str, secret: bool) -> None:
    assert adv.is_secret_key(key) is secret


@pytest.fixture
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.mark.usefixtures("_settings_env")
async def test_long_advanced_settings_list_goes_behind_a_result_handle() -> None:
    conn = _Fake()
    conn.extra_config = {f"guestinfo.app.key{i:03d}": f"value-{i}" for i in range(120)}
    out = await adv.vm_advanced_settings_show_impl(conn, _operator(), _Target(), {"vm": _VM})  # type: ignore[arg-type]
    reduced, handle = await JsonFluxReducer(sample_size=5).reduce(
        out, None, {"op_id": adv.VMWARE_VM_ADVANCED_SETTINGS_SHOW_OP.op_id}
    )
    assert handle is not None
    assert reduced["row_count"] == 120


# ---------------------------------------------------------------------------
# vmware.composite.vm.advanced_settings.set
# ---------------------------------------------------------------------------


async def test_advanced_settings_set_sends_only_changed_keys_and_reads_back(gate: _Gate) -> None:
    conn = _Fake()
    out = await _run(
        _ADV_SET,
        conn,
        vm=_VM,
        settings=[
            {"key": "disk.EnableUUID", "value": "TRUE"},
            {"key": "svga.present", "value": "TRUE"},
        ],
        remove=["guestinfo.userdata", "not.there"],
    )
    _check(_spec(_ADV_SET).response_schema, out)
    assert out["status"] == "set"
    assert conn.reconfigs == [
        {
            "spec": {
                "_typeName": "VirtualMachineConfigSpec",
                "extraConfig": [
                    {"_typeName": "OptionValue", "key": "disk.EnableUUID", "value": _box("TRUE")},
                    {"_typeName": "OptionValue", "key": "guestinfo.userdata", "value": _box("")},
                ],
            }
        }
    ]
    assert out["changes"] == [
        {
            "key": "disk.EnableUUID",
            "action": "set",
            "before": "FALSE",
            "after": "TRUE",
            "masked": False,
        },
        {
            "key": "guestinfo.userdata",
            "action": "remove",
            "before": None,
            "after": None,
            "masked": True,
        },
    ]
    assert "<base64-user-data>" not in json.dumps(out)
    assert gate.calls[0]["op_id"] == "POST:/VirtualMachine/{moId}/ReconfigVM_Task"
    assert "guestinfo.userdata" not in conn.extra_config


@pytest.mark.parametrize(
    "secret_key", ["guestinfo.root_password", "guestinfo.ignition.config.data"]
)
async def test_advanced_settings_set_refuses_to_set_a_secret_key(
    gate: _Gate, secret_key: str
) -> None:
    conn = _Fake()
    out = await _run(_ADV_SET, conn, vm=_VM, settings=[{"key": secret_key, "value": "x"}])
    assert out["status"] == "invalid_request"
    assert conn.reconfigs == [] and gate.calls == []


async def test_advanced_settings_set_matching_sends_nothing(gate: _Gate) -> None:
    conn = _Fake()
    out = await _run(
        _ADV_SET, conn, vm=_VM, settings=[{"key": "svga.present", "value": "TRUE"}], remove=["x.y"]
    )
    assert out["status"] == "unchanged"
    assert conn.reconfigs == []


async def test_advanced_settings_set_same_key_twice_is_refused(gate: _Gate) -> None:
    out = await _run(
        _ADV_SET, _Fake(), vm=_VM, settings=[{"key": "a.b", "value": "1"}], remove=["a.b"]
    )
    assert out["status"] == "invalid_request"


async def test_advanced_settings_set_ignored_key_is_partial(gate: _Gate) -> None:
    conn = _Fake()
    conn.ignore_keys = {"numvcpus"}
    out = await _run(_ADV_SET, conn, vm=_VM, settings=[{"key": "numvcpus", "value": "8"}])
    assert out["status"] == "partial"


async def test_advanced_settings_set_waits_for_approval(held: OperationResult) -> None:
    conn = _Fake()
    out = await _run(_ADV_SET, conn, vm=_VM, settings=[{"key": "disk.EnableUUID", "value": "TRUE"}])
    assert out is held
    assert conn.reconfigs == []


async def test_advanced_settings_set_task_fault_raises(gate: _Gate) -> None:
    conn = _Fake()
    conn.task_state = "error"
    conn.task_error = {
        "_typeName": "LocalizedMethodFault",
        "fault": {"_typeName": "InvalidVmConfig"},
        "localizedMessage": "Invalid virtual machine configuration.",
    }
    with pytest.raises(RuntimeError) as excinfo:
        await _run(_ADV_SET, conn, vm=_VM, settings=[{"key": "disk.EnableUUID", "value": "TRUE"}])
    assert "InvalidVmConfig" in str(excinfo.value)
    assert "Invalid virtual machine configuration." in str(excinfo.value)


async def test_advanced_settings_set_preview_masks_secret_values() -> None:
    preview = await _PREVIEW_BUILDERS[_ADV_SET](
        _preview_ctx(
            _Fake(),
            vm=_VM,
            settings=[{"key": "disk.EnableUUID", "value": "TRUE"}],
            remove=["guestinfo.userdata"],
        )
    )
    assert preview is not None
    assert [row["key"] for row in preview["changes"]] == ["disk.EnableUUID", "guestinfo.userdata"]
    assert preview["changes"][0]["after"] == "TRUE"
    assert "<base64-user-data>" not in json.dumps(preview)


async def test_previews_without_a_connector_decline() -> None:
    for op_id in _CHANGES:
        params: dict[str, Any] = {"vm": _VM, "remove": ["a.b"], "delay_ms": 1}
        assert await _PREVIEW_BUILDERS[op_id](_preview_ctx(None, **params)) is None


# ---------------------------------------------------------------------------
# The real connector over mocked HTTP: vCenter 8 and 9 VI-JSON paths
# ---------------------------------------------------------------------------


async def _stub_loader(_target: VsphereTargetLike, _operator: Operator) -> dict[str, str]:
    return {"username": "svc-meho", "password": "stub-password"}


@dataclass
class _StubTarget:
    name: str = "vc-example"
    host: str = "vc.example.test"
    port: int | None = 443
    secret_ref: str = "vsphere/vc-example"
    auth_model: str | None = AuthModel.SHARED_SERVICE_ACCOUNT.value
    id: UUID = field(default_factory=uuid4)
    tenant_id: UUID = field(default_factory=lambda: UUID(int=0))
    fingerprint: dict[str, Any] | None = None


def _vim_route(fake: _Fake, release: str) -> Any:
    """Answer the VI-JSON calls of the change from *fake*, on the path of *release*."""

    def vim(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        path = request.url.path.removeprefix(f"/sdk/vim25/{release}")
        if path.endswith("/ReconfigVM_Task"):
            fake._apply(body["spec"])
            return httpx.Response(
                200, json={"_typeName": "ManagedObjectReference", "type": "Task", "value": _TASK}
            )
        prop_spec = body["specSet"][0]["propSet"][0]
        if prop_spec["type"] == "Task":
            info = {"_typeName": "TaskInfo", "state": "success"}
            return httpx.Response(
                200,
                json={
                    "objects": [
                        {
                            "obj": {"type": "Task", "value": _TASK},
                            "propSet": [{"name": "info", "val": info}],
                        }
                    ]
                },
            )
        return httpx.Response(200, json={"objects": [fake._vm_object(prop_spec["pathSet"])]})

    return vim


@pytest.mark.parametrize(("about", "release"), [("8.0.3", "8.0.3.0"), ("9.0.0.0", "9.0.0.0")])
async def test_real_connector_advanced_settings_set_on_vcenter_8_and_9(
    gate: _Gate, about: str, release: str
) -> None:
    fake = _Fake()
    connector = VmwareRestConnector(session_loader=_stub_loader)
    try:
        async with respx.mock(base_url="https://vc.example.test") as mock:
            mock.post("/api/session").respond(200, json="tok")
            mock.get("/api/about").respond(200, json={"version": about})
            route = mock.post(
                url__regex=rf"^https://vc\.example\.test/sdk/vim25/{release}/.*"
            ).mock(side_effect=_vim_route(fake, release))
            out = await adv_set.vm_advanced_settings_set_composite(
                operator=_operator(),
                target=_StubTarget(),
                params={"vm": _VM, "settings": [{"key": "disk.EnableUUID", "value": "TRUE"}]},
                connector=connector,
            )
        assert out["status"] == "set"
        paths = [call.request.url.path for call in route.calls]
        assert f"/sdk/vim25/{release}/VirtualMachine/{_VM}/ReconfigVM_Task" in paths
        assert f"/sdk/vim25/{release}{_RETRIEVE}" in paths
    finally:
        connector._session_tokens.clear()
        for client in connector._clients.values():
            await client.aclose()
        connector._clients.clear()


# ---------------------------------------------------------------------------
# The flight recorder: a stored trace holds no secret setting value
# ---------------------------------------------------------------------------

_SECRET_VALUE = "<base64-user-data>"
_SHOW_OP = adv.VMWARE_VM_ADVANCED_SETTINGS_SHOW_OP


@contextmanager
def _capture_scope(op_id: str, tags: tuple[str, ...]) -> Iterator[Any]:
    """Bind a flight-recorder scope the way the dispatcher does, without a DB."""
    scope = capture._CaptureScope(audit_id=uuid4(), tenant_id=uuid4(), target_id=None)
    op = capture._OpContext(
        op_id=op_id,
        connector_id="vmware-rest-9.0",
        impl_id="vmware-rest",
        product="vmware",
        tags=tags,
        body_paths=(),
        delete_shaped_patterns=(),
    )
    cap_token = capture._active_capture_var.set(scope)
    op_token = capture._op_context_var.set(op)
    try:
        yield scope
    finally:
        capture._op_context_var.reset(op_token)
        capture._active_capture_var.reset(cap_token)


async def _vim_spans_of_show_and_set() -> list[Any]:
    """Run the read and the change on the real connector; return the VI-JSON spans."""
    fake = _Fake()
    connector = VmwareRestConnector(session_loader=_stub_loader)
    try:
        async with respx.mock(base_url="https://vc.example.test") as mock:
            mock.post("/api/session").respond(200, json="tok")
            mock.get("/api/about").respond(200, json={"version": "9.0.0.0"})
            mock.post(url__regex=r"^https://vc\.example\.test/sdk/vim25/9\.0\.0\.0/.*").mock(
                side_effect=_vim_route(fake, "9.0.0.0")
            )
            with _capture_scope(_SHOW_OP.op_id, _SHOW_OP.tags) as scope:
                shown = await connector.vm_advanced_settings_show(
                    _operator(), _StubTarget(), {"vm": _VM}
                )
                changed = await adv_set.vm_advanced_settings_set_composite(
                    operator=_operator(),
                    target=_StubTarget(),
                    params={"vm": _VM, "settings": [{"key": "disk.EnableUUID", "value": "TRUE"}]},
                    connector=connector,
                )
    finally:
        connector._session_tokens.clear()
        for client in connector._clients.values():
            await client.aclose()
        connector._clients.clear()
    assert shown["masked_count"] == 1
    assert changed["status"] == "set"
    return [
        span
        for span in scope.spans
        if span.span_kind == "vendor_call" and "/sdk/vim25/" in span.attributes["url"]
    ]


@pytest.mark.usefixtures("gate")
async def test_flight_recorder_spans_hold_no_secret_setting_value() -> None:
    spans = await _vim_spans_of_show_and_set()
    reads = [
        span
        for span in spans
        if span.attributes["url"].endswith(_RETRIEVE)
        and "config.extraConfig" in json.dumps(span.attributes["response_body"])
    ]
    assert len(reads) == 3  # the show, and the read before and after the change
    for span in reads:
        assert span.attributes["body_recorded"] is True
        rows = json.dumps(span.attributes["response_body"])
        assert "guestinfo.userdata" in rows
        assert vmware_trace.SECRET_SETTING_MARKER in rows
        assert "disk.EnableUUID" in rows
    assert _SECRET_VALUE not in repr(spans)


@pytest.mark.usefixtures("gate")
async def test_without_the_mask_the_flight_recorder_would_keep_the_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Control: the connector's mask is what keeps the value out, so the test above is not empty."""
    monkeypatch.setattr(VmwareRestConnector, "trace_response_body", lambda self, response: None)
    spans = await _vim_spans_of_show_and_set()
    assert _SECRET_VALUE in repr(spans)


def test_trace_payload_masks_only_secret_named_values() -> None:
    payload: dict[str, Any] = {
        "objects": [
            {
                "propSet": [
                    {
                        "name": "config.extraConfig",
                        "val": {
                            "_typeName": "ArrayOfOptionValue",
                            "_value": [
                                {
                                    "_typeName": "OptionValue",
                                    "key": "svga.present",
                                    "value": _box("TRUE"),
                                },
                                {
                                    "_typeName": "OptionValue",
                                    "key": "guestinfo.userdata",
                                    "value": _box(_SECRET_VALUE),
                                },
                                {"key": "ovf.Password", "value": "hunter2"},
                            ],
                        },
                    }
                ]
            }
        ]
    }
    masked = vmware_trace.trace_payload(payload)
    rows = masked["objects"][0]["propSet"][0]["val"]["_value"]
    assert rows[0] == {"_typeName": "OptionValue", "key": "svga.present", "value": _box("TRUE")}
    assert rows[1]["key"] == "guestinfo.userdata"
    assert rows[1]["value"] == vmware_trace.SECRET_SETTING_MARKER
    assert rows[2] == {"key": "ovf.Password", "value": vmware_trace.SECRET_SETTING_MARKER}
    assert _SECRET_VALUE not in json.dumps(masked)
    assert payload["objects"][0]["propSet"][0]["val"]["_value"][1]["value"] == _box(_SECRET_VALUE)


def test_trace_response_body_leaves_non_json_to_the_shared_engine_and_never_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connector = VmwareRestConnector(session_loader=_stub_loader)
    assert connector.trace_response_body(httpx.Response(200, text="<xml/>")) is None
    assert connector.trace_response_body(httpx.Response(200, json={"a": 1})) == {"a": 1}

    def boom(payload: Any) -> Any:
        raise RecursionError

    monkeypatch.setattr(vmware_trace, "trace_payload", boom)
    out = connector.trace_response_body(httpx.Response(200, json={"a": 1}))
    assert out == vmware_trace.SECRET_SETTING_MARKER
