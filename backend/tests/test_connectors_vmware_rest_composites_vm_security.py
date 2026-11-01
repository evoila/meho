# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for the VM security changes: ``vm.vtpm.add``, ``vm.vtpm.remove`` and
``vm.encryption.set``.

Three layers:

* the handlers and previews against the shared vim fake, extended here with
  ``ReconfigVM_Task``: the exact request bodies, the gated write, the task poll,
  the read after the task, every stop before a write, a task fault, the poll
  timeout, a parked gate, and the approval-card blast radius;
* the transport: the vTPM add lands on ``/sdk/vim25/<release>/...`` for a
  vCenter 8.0 U3 and a vCenter 9.0 target;
* the governed flow through the real dispatcher: each change waits for a
  second person; the vTPM remove needs a preview first and is refused for an
  agent.
"""

from __future__ import annotations

import copy
import json as jsonlib
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import httpx
import pytest
import respx
from jsonschema import Draft202012Validator
from sqlalchemy import func, select

from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.connectors.registry import clear_registry, register_connector_v2
from meho_backplane.connectors.schemas import AuthModel
from meho_backplane.connectors.vmware_rest import VmwareRestConnector, VsphereTargetLike
from meho_backplane.connectors.vmware_rest.composites import (
    _vm_encryption,
    _vm_security,
    _vm_vtpm,
    _write,
    register_vmware_composite_operations,
)
from meho_backplane.connectors.vmware_rest.composites._vm_security_schemas import (
    VM_ENCRYPTION_SET_PARAMETER_SCHEMA,
    VM_ENCRYPTION_SET_RESPONSE_SCHEMA,
    VM_VTPM_ADD_PARAMETER_SCHEMA,
    VM_VTPM_ADD_RESPONSE_SCHEMA,
    VM_VTPM_REMOVE_RESPONSE_SCHEMA,
)
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import ApprovalRequest, EndpointDescriptor
from meho_backplane.db.models import Target as TargetORM
from meho_backplane.operations import dispatch, reset_dispatcher_caches
from meho_backplane.operations._handler_resolve import _CONNECTOR_INSTANCE_CACHE
from meho_backplane.operations._preview import blast_radius_missing_reason
from meho_backplane.operations._validate import validate_params
from meho_backplane.operations.approval_queue import (
    approve_request,
    resume_dispatch_after_approval,
)
from meho_backplane.operations.meta_tools import call_operation, preview_operation
from meho_backplane.settings import get_settings
from tests._vmware_teardown_fake import (
    GateRecorder,
    VimFake,
    operator,
    parked,
    preview_ctx,
)

_VM = "vm-42"
_ADD_OP = "vmware.composite.vm.vtpm.add"
_REMOVE_OP = "vmware.composite.vm.vtpm.remove"
_ENCRYPTION_OP = "vmware.composite.vm.encryption.set"
_RECONFIG_SUB_OP = "POST:/VirtualMachine/{moId}/ReconfigVM_Task"
_RECONFIG_PATH = f"/VirtualMachine/{_VM}/ReconfigVM_Task"
_KEY_ID = {
    "_typeName": "CryptoKeyId",
    "keyId": "<key-id>",
    "providerId": {"_typeName": "KeyProviderId", "id": "<key-provider>"},
}
_TPM = {
    "_typeName": "VirtualTPM",
    "key": 11000,
    "deviceInfo": {"_typeName": "Description", "label": "Virtual TPM", "summary": "TPM 2.0"},
    "endorsementKeyCertificate": ["<certificate>"],
}


def _disk(key: int, *, encrypted: bool = False) -> dict[str, Any]:
    backing: dict[str, Any] = {
        "_typeName": "VirtualDiskFlatVer2BackingInfo",
        "fileName": f"[<datastore-name>] app-vm/app-vm_{key}.vmdk",
        "diskMode": "persistent",
    }
    if encrypted:
        backing["keyId"] = copy.deepcopy(_KEY_ID)
    return {
        "_typeName": "VirtualDisk",
        "key": key,
        "deviceInfo": {"_typeName": "Description", "label": f"Hard disk {key - 1999}"},
        "backing": backing,
        "capacityInKB": 1048576,
    }


class SecurityVimFake(VimFake):
    """The shared vim fake plus ``ReconfigVM_Task``.

    The task changes the VM the way vCenter does: a vTPM device is added or
    removed, and an encrypt or decrypt sets or clears the key ids of the VM
    home and the disks. So the handlers' reads after the task see the result.
    """

    async def _post_vmomi_json(
        self,
        target: Any,
        path: str,
        *,
        operator: Operator,
        json: Any = None,
        promote_managed_object_not_found: bool = False,
    ) -> Any:
        _mo_type, moid, method = path.strip("/").split("/")
        if method == "ReconfigVM_Task":
            self.calls.append((path, copy.deepcopy(json)))
            return self._task(method, apply=lambda: self._reconfigure(moid, json["spec"]))
        return await super()._post_vmomi_json(
            target,
            path,
            operator=operator,
            json=json,
            promote_managed_object_not_found=promote_managed_object_not_found,
        )

    def _reconfigure(self, vm: str, spec: dict[str, Any]) -> None:
        props = self.objects[("VirtualMachine", vm)]
        devices = props.setdefault("config.hardware.device", [])
        for change in spec.get("deviceChange", []):
            device = change["device"]
            if change["operation"] == "add" and device["_typeName"] == "VirtualTPM":
                devices.append({**device, "key": 11000})
            elif change["operation"] == "remove":
                devices[:] = [d for d in devices if d["key"] != device["key"]]
        if "vmProfile" not in spec:
            return
        decrypt = spec.get("crypto", {}).get("_typeName") == "CryptoSpecDecrypt"
        if decrypt:
            props.pop("config.keyId", None)
        else:
            props["config.keyId"] = copy.deepcopy(_KEY_ID)
        for disk in (d for d in devices if d["_typeName"] == "VirtualDisk"):
            if decrypt:
                disk["backing"].pop("keyId", None)
            else:
                disk["backing"]["keyId"] = copy.deepcopy(_KEY_ID)


def _vm(
    conn: SecurityVimFake,
    *,
    power: str = "poweredOff",
    firmware: str = "efi",
    version: str = "vmx-21",
    devices: list[dict[str, Any]] | None = None,
    key_id: dict[str, Any] | None = None,
    snapshot: dict[str, Any] | None = None,
) -> None:
    props: dict[str, Any] = {
        "name": "app-vm",
        "runtime.powerState": power,
        "config.hardware.device": copy.deepcopy(devices if devices is not None else [_disk(2000)]),
        "config.version": version,
        "config.firmware": firmware,
    }
    if key_id is not None:
        props["config.keyId"] = copy.deepcopy(key_id)
    if snapshot is not None:
        props["snapshot"] = snapshot
    conn.add("VirtualMachine", _VM, **props)


@pytest.fixture
def gate(monkeypatch: pytest.MonkeyPatch) -> GateRecorder:
    recorder = GateRecorder()
    monkeypatch.setattr(_write, "enforce_subop_policy", recorder)
    return recorder


async def _add(conn: Any, **params: Any) -> Any:
    return await _vm_vtpm.vm_vtpm_add_composite(
        operator=operator(), target=object(), params={"vm": _VM, **params}, connector=conn
    )


async def _remove(conn: Any, **params: Any) -> Any:
    return await _vm_vtpm.vm_vtpm_remove_composite(
        operator=operator(), target=object(), params={"vm": _VM, **params}, connector=conn
    )


async def _encryption(conn: Any, action: str, policy: str = "<policy-id>", **extra: Any) -> Any:
    params = {"vm": _VM, "action": action, "storage_policy_id": policy, **extra}
    return await _vm_encryption.vm_encryption_set_composite(
        operator=operator(), target=object(), params=params, connector=conn
    )


# ===========================================================================
# vm.vtpm.add
# ===========================================================================


async def test_add_happy_path(gate: GateRecorder) -> None:
    conn = SecurityVimFake()
    _vm(conn)
    out = await _add(conn)

    assert out["status"] == "added"
    assert out["vm_name"] == "app-vm"
    assert out["power_state"] == "poweredOff"
    assert out["vtpm"] == {"key": 11000, "label": None}
    assert out["task"] == "task-1"
    assert [c["op_id"] for c in gate.calls] == [_RECONFIG_SUB_OP]
    assert gate.calls[0]["params"]["vm"] == _VM
    [(path, body)] = conn.calls_to("ReconfigVM_Task")
    assert path == _RECONFIG_PATH
    assert body == {
        "spec": {
            "_typeName": "VirtualMachineConfigSpec",
            "deviceChange": [
                {
                    "_typeName": "VirtualDeviceConfigSpec",
                    "operation": "add",
                    "device": {"_typeName": "VirtualTPM", "key": -1},
                }
            ],
        }
    }
    # The VM is read before the change, the task is polled, the VM is read again.
    assert conn.methods() == [
        "RetrievePropertiesEx",
        "ReconfigVM_Task",
        "RetrievePropertiesEx",
        "RetrievePropertiesEx",
    ]
    Draft202012Validator(VM_VTPM_ADD_RESPONSE_SCHEMA).validate(out)


async def test_add_answer_carries_no_certificate(gate: GateRecorder) -> None:
    conn = SecurityVimFake()
    _vm(conn, devices=[_disk(2000), _TPM])
    out = await _add(conn)
    assert out["status"] == "unchanged"
    assert out["vtpm"] == {"key": 11000, "label": "Virtual TPM"}
    assert "<certificate>" not in jsonlib.dumps(out)
    assert gate.calls == []
    assert conn.calls_to("ReconfigVM_Task") == []


@pytest.mark.parametrize(
    ("vm_kwargs", "fragment"),
    [
        ({"power": "poweredOn"}, "power it off first"),
        ({"firmware": "bios"}, "EFI firmware"),
        ({"version": "vmx-13"}, "hardware version 14"),
    ],
)
async def test_add_stops_before_any_write_when_the_vm_does_not_fit(
    gate: GateRecorder, vm_kwargs: dict[str, Any], fragment: str
) -> None:
    conn = SecurityVimFake()
    _vm(conn, **vm_kwargs)
    out = await _add(conn)
    assert out["status"] == "precondition_failed"
    assert fragment in out["guidance"]
    assert gate.calls == []
    assert conn.calls_to("ReconfigVM_Task") == []
    Draft202012Validator(VM_VTPM_ADD_RESPONSE_SCHEMA).validate(out)


async def test_add_on_an_unknown_vm_is_not_found(gate: GateRecorder) -> None:
    conn = SecurityVimFake()
    out = await _add(conn)
    assert out["status"] == "not_found"
    assert gate.calls == []


async def test_add_refuses_a_vm_name_as_id(gate: GateRecorder) -> None:
    conn = SecurityVimFake()
    out = await _vm_vtpm.vm_vtpm_add_composite(
        operator=operator(), target=object(), params={"vm": "app-vm"}, connector=conn
    )
    assert out["status"] == "invalid_request"
    assert conn.calls == []


async def test_add_task_fault_reaches_the_answer(gate: GateRecorder) -> None:
    conn = SecurityVimFake()
    _vm(conn)
    conn.task_faults["ReconfigVM_Task"] = (
        "InvalidVmConfig",
        "No default key provider is set.",
    )
    with pytest.raises(RuntimeError, match=r"InvalidVmConfig.*No default key provider"):
        await _add(conn)


async def test_add_timeout(gate: GateRecorder, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_vm_security, "_TASK_TIMEOUT_SECONDS", 0.0)
    conn = SecurityVimFake()
    _vm(conn)
    conn.running.add("ReconfigVM_Task")
    out = await _add(conn)
    assert out["status"] == "timeout"
    assert out["task"] == "task-1"
    assert [i["severity"] for i in out["issues"]] == ["error"]


async def test_add_that_does_not_read_back_is_not_confirmed(gate: GateRecorder) -> None:
    conn = SecurityVimFake()
    _vm(conn)
    conn.apply_writes = False
    out = await _add(conn)
    assert out["status"] == "not_confirmed"


async def test_add_returns_a_parked_gate_as_is() -> None:
    conn = SecurityVimFake()
    _vm(conn)
    verdict = parked(_RECONFIG_SUB_OP)
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(_write, "enforce_subop_policy", GateRecorder(verdict))
        out = await _add(conn)
    assert out is verdict
    assert conn.calls_to("ReconfigVM_Task") == []


async def test_add_preview_shows_the_vm_and_a_stop() -> None:
    conn = SecurityVimFake()
    _vm(conn)
    preview = await _vm_vtpm._vtpm_add_preview(preview_ctx({"vm": _VM}, conn))
    assert preview is not None
    assert preview["vm_name"] == "app-vm"
    assert "stops_with" not in preview

    conn = SecurityVimFake()
    _vm(conn, power="poweredOn")
    preview = await _vm_vtpm._vtpm_add_preview(preview_ctx({"vm": _VM}, conn))
    assert preview is not None
    assert preview["stops_with"] == "precondition_failed"
    assert await _vm_vtpm._vtpm_add_preview(preview_ctx({"vm": _VM}, None)) is None


# ===========================================================================
# vm.vtpm.remove
# ===========================================================================


async def test_remove_happy_path(gate: GateRecorder) -> None:
    conn = SecurityVimFake()
    _vm(conn, devices=[_disk(2000), _TPM])
    out = await _remove(conn)

    assert out["status"] == "removed"
    assert out["object"]["vm_name"] == "app-vm"
    assert out["object"]["vtpm"] == {"key": 11000, "label": "Virtual TPM"}
    assert out["task"] == "task-1"
    [(path, body)] = conn.calls_to("ReconfigVM_Task")
    assert path == _RECONFIG_PATH
    assert body == {
        "spec": {
            "_typeName": "VirtualMachineConfigSpec",
            "deviceChange": [
                {"_typeName": "VirtualDeviceConfigSpec", "operation": "remove", "device": _TPM}
            ],
        }
    }
    assert _vm_security.vtpm_device(conn.objects[("VirtualMachine", _VM)]) is None
    assert "<certificate>" not in jsonlib.dumps(out)
    Draft202012Validator(VM_VTPM_REMOVE_RESPONSE_SCHEMA).validate(out)


async def test_remove_without_a_vtpm_changes_nothing(gate: GateRecorder) -> None:
    conn = SecurityVimFake()
    _vm(conn)
    out = await _remove(conn)
    assert out["status"] == "unchanged"
    assert gate.calls == []


async def test_remove_on_an_unknown_vm_is_refused(gate: GateRecorder) -> None:
    conn = SecurityVimFake()
    out = await _remove(conn)
    assert out["status"] == "invalid_request"
    assert "not found" in out["guidance"]
    assert gate.calls == []


async def test_remove_on_a_running_vm_stops(gate: GateRecorder) -> None:
    conn = SecurityVimFake()
    _vm(conn, power="poweredOn", devices=[_TPM])
    out = await _remove(conn)
    assert out["status"] == "precondition_failed"
    assert conn.calls_to("ReconfigVM_Task") == []


async def test_remove_task_fault_reaches_the_answer(gate: GateRecorder) -> None:
    conn = SecurityVimFake()
    _vm(conn, devices=[_TPM])
    conn.task_faults["ReconfigVM_Task"] = ("InvalidState", "The VM is busy.")
    with pytest.raises(RuntimeError, match=r"InvalidState.*The VM is busy"):
        await _remove(conn)


async def test_remove_timeout(gate: GateRecorder, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_vm_security, "_TASK_TIMEOUT_SECONDS", 0.0)
    conn = SecurityVimFake()
    _vm(conn, devices=[_TPM])
    conn.running.add("ReconfigVM_Task")
    out = await _remove(conn)
    assert out["status"] == "timeout"


async def test_remove_that_still_reads_back_is_still_present(gate: GateRecorder) -> None:
    conn = SecurityVimFake()
    _vm(conn, devices=[_TPM])
    conn.apply_writes = False
    out = await _remove(conn)
    assert out["status"] == "still_present"


async def test_remove_returns_a_parked_gate_as_is() -> None:
    conn = SecurityVimFake()
    _vm(conn, devices=[_TPM])
    verdict = parked(_RECONFIG_SUB_OP)
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(_write, "enforce_subop_policy", GateRecorder(verdict))
        out = await _remove(conn)
    assert out is verdict
    assert conn.calls_to("ReconfigVM_Task") == []


async def test_remove_preview_fills_the_blast_radius() -> None:
    conn = SecurityVimFake()
    _vm(conn, devices=[_TPM])
    effect = await _vm_vtpm._vtpm_remove_preview(preview_ctx({"vm": _VM}, conn))
    assert effect is not None
    assert blast_radius_missing_reason(effect) is None
    block = effect["blast_radius"]
    assert block["object"]["present"] is True
    assert block["object"]["vm_name"] == "app-vm"
    assert "BitLocker" in block["object"]["lost_with_it"]
    assert block["children"] == []
    assert block["irreversibility"] == "permanent"


async def test_remove_preview_of_an_unknown_vm_carries_the_refusal() -> None:
    effect = await _vm_vtpm._vtpm_remove_preview(preview_ctx({"vm": _VM}, SecurityVimFake()))
    assert effect is not None
    assert effect["blast_radius"]["object"]["present"] is False
    assert effect["blast_radius"]["refusal"]["status"] == "invalid_request"
    assert await _vm_vtpm._vtpm_remove_preview(preview_ctx({"vm": _VM}, None)) is None


# ===========================================================================
# vm.encryption.set
# ===========================================================================


async def test_encrypt_happy_path(gate: GateRecorder) -> None:
    conn = SecurityVimFake()
    _vm(conn, devices=[_disk(2000), _disk(2001)])
    out = await _encryption(conn, "encrypt")

    assert out["status"] == "encrypted"
    assert out["before"]["vm_home_encrypted"] is False
    assert out["after"]["vm_home_encrypted"] is True
    assert out["after"]["key_provider"] == "<key-provider>"
    assert [d["encrypted"] for d in out["after"]["disks"]] == [True, True]
    [(path, body)] = conn.calls_to("ReconfigVM_Task")
    assert path == _RECONFIG_PATH
    spec = body["spec"]
    profile = [{"_typeName": "VirtualMachineDefinedProfileSpec", "profileId": "<policy-id>"}]
    assert spec["vmProfile"] == profile
    # Encrypt sends no crypto spec: vCenter makes the keys from the default provider.
    assert "crypto" not in spec
    assert [(c["operation"], c["device"]["key"], c["profile"]) for c in spec["deviceChange"]] == [
        ("edit", 2000, profile),
        ("edit", 2001, profile),
    ]
    assert all("backing" not in c for c in spec["deviceChange"])
    # The answer names the key provider, never the key itself.
    assert "<key-id>" not in jsonlib.dumps(out)
    Draft202012Validator(VM_ENCRYPTION_SET_RESPONSE_SCHEMA).validate(out)


async def test_decrypt_happy_path(gate: GateRecorder) -> None:
    conn = SecurityVimFake()
    _vm(conn, devices=[_disk(2000, encrypted=True), _disk(2001)], key_id=_KEY_ID)
    out = await _encryption(conn, "decrypt", "<plain-policy-id>")

    assert out["status"] == "decrypted"
    assert out["after"]["vm_home_encrypted"] is False
    [(_path, body)] = conn.calls_to("ReconfigVM_Task")
    spec = body["spec"]
    assert spec["crypto"] == {"_typeName": "CryptoSpecDecrypt"}
    first, second = spec["deviceChange"]
    assert first["backing"] == {
        "_typeName": "VirtualDeviceConfigSpecBackingSpec",
        "crypto": {"_typeName": "CryptoSpecDecrypt"},
    }
    # A disk that is not encrypted gets the policy only.
    assert "backing" not in second
    assert "<key-id>" not in jsonlib.dumps(out)
    Draft202012Validator(VM_ENCRYPTION_SET_RESPONSE_SCHEMA).validate(out)


async def test_encryption_in_the_asked_state_changes_nothing(gate: GateRecorder) -> None:
    conn = SecurityVimFake()
    _vm(conn)
    out = await _encryption(conn, "decrypt")
    assert out["status"] == "unchanged"
    assert gate.calls == []


async def test_encrypt_refuses_a_vm_with_snapshots(gate: GateRecorder) -> None:
    conn = SecurityVimFake()
    _vm(conn, snapshot={"_typeName": "VirtualMachineSnapshotInfo", "rootSnapshotList": []})
    out = await _encryption(conn, "encrypt")
    assert out["status"] == "precondition_failed"
    assert "snapshots" in out["guidance"]
    assert gate.calls == []


async def test_encryption_on_a_running_vm_stops(gate: GateRecorder) -> None:
    conn = SecurityVimFake()
    _vm(conn, power="poweredOn")
    out = await _encryption(conn, "encrypt")
    assert out["status"] == "precondition_failed"
    assert conn.calls_to("ReconfigVM_Task") == []


async def test_encryption_on_an_unknown_vm_is_not_found(gate: GateRecorder) -> None:
    out = await _encryption(SecurityVimFake(), "encrypt")
    assert out["status"] == "not_found"


async def test_encryption_that_does_not_read_back_is_not_confirmed(gate: GateRecorder) -> None:
    conn = SecurityVimFake()
    _vm(conn)
    conn.apply_writes = False
    out = await _encryption(conn, "encrypt")
    assert out["status"] == "not_confirmed"
    assert out["after"]["vm_home_encrypted"] is False


async def test_encryption_task_fault_reaches_the_answer(gate: GateRecorder) -> None:
    conn = SecurityVimFake()
    _vm(conn)
    conn.task_faults["ReconfigVM_Task"] = ("InvalidArgument", "The policy is not valid.")
    with pytest.raises(RuntimeError, match=r"InvalidArgument.*policy is not valid"):
        await _encryption(conn, "encrypt")


async def test_encryption_preview_shows_the_state_now() -> None:
    conn = SecurityVimFake()
    _vm(conn)
    params = {"vm": _VM, "action": "encrypt", "storage_policy_id": "<policy-id>"}
    preview = await _vm_encryption._encryption_preview(preview_ctx(params, conn))
    assert preview is not None
    assert preview["current"]["vm_home_encrypted"] is False
    assert preview["storage_policy_id"] == "<policy-id>"
    assert "stops_with" not in preview
    bad = {**params, "action": "rotate"}
    assert await _vm_encryption._encryption_preview(preview_ctx(bad, conn)) is None


def test_parameter_schemas() -> None:
    assert validate_params(VM_VTPM_ADD_PARAMETER_SCHEMA, {"vm": _VM}) == []
    assert validate_params(VM_VTPM_ADD_PARAMETER_SCHEMA, {"vm": "app-vm"}) != []
    schema = VM_ENCRYPTION_SET_PARAMETER_SCHEMA
    ok = {"vm": _VM, "action": "encrypt", "storage_policy_id": "<policy-id>"}
    assert validate_params(schema, ok) == []
    assert validate_params(schema, {**ok, "action": "rotate"}) != []
    assert validate_params(schema, {"vm": _VM, "action": "encrypt"}) != []


# ===========================================================================
# Transport: the VI-JSON path on vCenter 8.0 U3 and 9.0
# ===========================================================================


async def _stub_loader(_target: VsphereTargetLike, _operator: Operator) -> dict[str, str]:
    return {"username": "svc-meho", "password": "stub-password"}


def _patch_no_revoke_aclose(connector: VmwareRestConnector) -> None:
    """Skip the session-revoke leg at teardown (like the datastore refresh tests)."""

    async def _aclose() -> None:
        connector._session_tokens.clear()
        for client in connector._clients.values():
            await client.aclose()
        connector._clients.clear()

    connector.aclose = _aclose  # type: ignore[method-assign]


@dataclass
class _StubTarget:
    name: str = "vc"
    host: str = "vc.test.invalid"
    port: int | None = 443
    secret_ref: str = "vsphere/vc"
    auth_model: str | None = AuthModel.SHARED_SERVICE_ACCOUNT.value
    id: UUID = field(default_factory=uuid4)
    tenant_id: UUID = field(default_factory=lambda: UUID(int=0))
    fingerprint: dict[str, Any] | None = None


def _retrieve_answer(request: httpx.Request, vm_props: list[dict[str, Any]]) -> httpx.Response:
    body = jsonlib.loads(request.content)
    mo_type = body["specSet"][0]["propSet"][0]["type"]
    if mo_type == "Task":
        info = {"_typeName": "TaskInfo", "state": "success", "key": "task-7"}
        obj = {"type": "Task", "value": "task-7"}
        return httpx.Response(200, json=[{"obj": obj, "propSet": [{"name": "info", "val": info}]}])
    props = vm_props.pop(0)
    obj = {"type": "VirtualMachine", "value": _VM}
    prop_set = [{"name": k, "val": v} for k, v in props.items()]
    return httpx.Response(200, json=[{"obj": obj, "propSet": prop_set}])


@pytest.mark.parametrize(("about", "release"), [("8.0.3", "8.0.3.0"), ("9.0.0", "9.0.0.0")])
async def test_add_lands_on_the_vi_json_path_of_each_vcenter_version(
    gate: GateRecorder, about: str, release: str
) -> None:
    connector = VmwareRestConnector(session_loader=_stub_loader)
    _patch_no_revoke_aclose(connector)
    before = {
        "name": "app-vm",
        "runtime.powerState": "poweredOff",
        "config.hardware.device": [_disk(2000)],
        "config.version": "vmx-21",
        "config.firmware": "efi",
    }
    after = {**before, "config.hardware.device": [_disk(2000), _TPM]}
    answers = [before, after]
    base = f"/sdk/vim25/{release}"
    try:
        async with respx.mock(base_url="https://vc.test.invalid") as mock:
            mock.post("/api/session").respond(200, json="tok")
            mock.get("/api/about").respond(200, json={"version": about})
            mock.post(f"{base}/PropertyCollector/propertyCollector/RetrievePropertiesEx").mock(
                side_effect=lambda request: _retrieve_answer(request, answers)
            )
            reconfig = mock.post(f"{base}/VirtualMachine/{_VM}/ReconfigVM_Task").respond(
                200, json={"_typeName": "ManagedObjectReference", "type": "Task", "value": "task-7"}
            )
            out = await _vm_vtpm.vm_vtpm_add_composite(
                operator=operator(), target=_StubTarget(), params={"vm": _VM}, connector=connector
            )
        assert reconfig.call_count == 1
        sent = jsonlib.loads(reconfig.calls[0].request.content)
        assert sent["spec"]["deviceChange"][0]["device"]["_typeName"] == "VirtualTPM"
        assert out["status"] == "added"
        assert out["task"] == "task-7"
    finally:
        await connector.aclose()


# ===========================================================================
# The governed flow through the dispatcher (approval gate)
# ===========================================================================

_CONNECTOR_ID = "vmware-rest-9.0"
_TENANT_ID = UUID("00000000-0000-0000-0000-000000004337")
_TARGET_NAME = "example-vcenter"


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
        name="VM Security Test",
        email=None,
        raw_jwt="<test-raw-jwt>",
        tenant_id=_TENANT_ID,
        tenant_role=TenantRole.OPERATOR,
        principal_kind=kind,
    )


async def _bootstrap(conn: SecurityVimFake) -> None:
    register_connector_v2(
        product="vmware", version="9.0", impl_id="vmware-rest", cls=VmwareRestConnector
    )
    _CONNECTOR_INSTANCE_CACHE[VmwareRestConnector] = conn  # type: ignore[assignment]
    await register_vmware_composite_operations(embedding_service=_embedding_stub())
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
                fingerprint={"version": "9.0"},
                preferred_impl_id="vmware-rest",
                notes="seeded by test_connectors_vmware_rest_composites_vm_security",
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


async def _approval_count() -> int:
    async with get_sessionmaker()() as s:
        return int(
            (await s.execute(select(func.count()).select_from(ApprovalRequest))).scalar_one()
        )


async def _approve_and_resume(request_id: UUID) -> Any:
    approver = _person("op-approver")
    async with get_sessionmaker()() as s:
        approved = await approve_request(s, request_id, operator=approver, params=None)
        await s.commit()
    assert approved.status == "approved"
    async with get_sessionmaker()() as s:
        row = await s.get(ApprovalRequest, request_id)
        assert row is not None
        return await resume_dispatch_after_approval(operator=approver, request=row, params=None)


@pytest.mark.usefixtures("_dispatch_env")
async def test_registered_risk_levels() -> None:
    await _bootstrap(SecurityVimFake())
    ops = (_ADD_OP, _REMOVE_OP, _ENCRYPTION_OP)
    async with get_sessionmaker()() as s:
        rows = {
            row.op_id: row
            for row in (
                await s.execute(select(EndpointDescriptor).where(EndpointDescriptor.op_id.in_(ops)))
            )
            .scalars()
            .all()
        }
    assert {op: rows[op].safety_level for op in ops} == {
        _ADD_OP: "dangerous",
        _REMOVE_OP: "destructive",
        _ENCRYPTION_OP: "dangerous",
    }
    assert all(rows[op].requires_approval is True for op in ops)


@pytest.mark.usefixtures("_dispatch_env")
@pytest.mark.parametrize(
    ("op_id", "params", "status"),
    [
        (_ADD_OP, {"vm": _VM}, "added"),
        (
            _ENCRYPTION_OP,
            {"vm": _VM, "action": "encrypt", "storage_policy_id": "<policy-id>"},
            "encrypted",
        ),
    ],
)
async def test_a_change_waits_for_a_second_person_then_runs_once(
    op_id: str, params: dict[str, Any], status: str
) -> None:
    conn = SecurityVimFake()
    _vm(conn)
    await _bootstrap(conn)
    args = {"connector_id": _CONNECTOR_ID, "op_id": op_id, "target": _TARGET_NAME, "params": params}
    call = await call_operation(_person("op-requester"), args)
    assert call["status"] == "awaiting_approval", call
    assert conn.calls_to("ReconfigVM_Task") == []

    resume = await _approve_and_resume(UUID(call["extras"]["approval_request_id"]))
    assert resume.status == "ok", resume.error
    assert resume.result["status"] == status
    assert len(conn.calls_to("ReconfigVM_Task")) == 1


@pytest.mark.usefixtures("_dispatch_env")
async def test_an_agent_without_a_grant_cannot_add_a_vtpm() -> None:
    conn = SecurityVimFake()
    _vm(conn)
    await _bootstrap(conn)
    result = await dispatch(
        operator=_person("agent-1", PrincipalKind.AGENT),
        connector_id=_CONNECTOR_ID,
        op_id=_ADD_OP,
        target=_FakeTarget(),
        params={"vm": _VM},
    )
    assert result.status == "denied", result
    assert "dangerous" in (result.error or "")
    assert conn.calls_to("ReconfigVM_Task") == []


@pytest.mark.usefixtures("_dispatch_env")
async def test_remove_needs_a_preview_then_a_second_person() -> None:
    conn = SecurityVimFake()
    _vm(conn, devices=[_disk(2000), _TPM])
    await _bootstrap(conn)
    requester = _person("op-requester")
    args = {
        "connector_id": _CONNECTOR_ID,
        "op_id": _REMOVE_OP,
        "target": _TARGET_NAME,
        "params": {"vm": _VM},
    }
    refused = await call_operation(requester, args)
    assert refused["status"] == "denied", refused
    assert refused["extras"]["error_code"] == "preview_binding_required"
    assert await _approval_count() == 0

    preview = await preview_operation(requester, args)
    assert preview["status"] == "ok", preview
    call = await call_operation(requester, {**args, "preview_hash": preview["preview_hash"]})
    assert call["status"] == "awaiting_approval", call
    assert conn.calls_to("ReconfigVM_Task") == []
    request_id = UUID(call["extras"]["approval_request_id"])

    async with get_sessionmaker()() as s:
        row = await s.get(ApprovalRequest, request_id)
        assert row is not None
        effect = dict(row.proposed_effect)
    assert effect["safety_level"] == "destructive"
    assert effect["blast_radius"]["object"]["vm_name"] == "app-vm"
    assert effect["blast_radius"]["irreversibility"] == "permanent"

    resume = await _approve_and_resume(request_id)
    assert resume.status == "ok", resume.error
    assert resume.result["status"] == "removed"
    assert len(conn.calls_to("ReconfigVM_Task")) == 1


@pytest.mark.usefixtures("_dispatch_env")
async def test_an_agent_remove_is_refused() -> None:
    conn = SecurityVimFake()
    _vm(conn, devices=[_TPM])
    await _bootstrap(conn)
    result = await dispatch(
        operator=_person("agent-1", PrincipalKind.AGENT),
        connector_id=_CONNECTOR_ID,
        op_id=_REMOVE_OP,
        target=_FakeTarget(),
        params={"vm": _VM},
    )
    assert result.status == "denied", result
    assert "destructive" in (result.error or "")
    assert conn.calls_to("ReconfigVM_Task") == []
    assert await _approval_count() == 0
