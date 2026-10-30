# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Unit tests for ``vmware.composite.folder.teardown`` (#4624).

Driven against the shared teardown fake with a made-up system: one VM folder
with a running and a stopped VM, a nested sub-folder with a running VM and a
template, and an empty sub-sub-folder. Covers the happy path on both destroy
arms, every refusal before any write (and that the step check never sees it),
the ``expected_vms`` fence when the folder changes after the preview, a VM
that fails to delete, a second run, the guest shutdown, a parked step, and the
park-time blast radius (the same plan the handler uses).
"""

from __future__ import annotations

from typing import Any

import pytest
from jsonschema import Draft202012Validator

from meho_backplane.connectors.vmware_rest.composites import (
    _folder_teardown,
    _folder_teardown_plan,
    _write,
)
from meho_backplane.connectors.vmware_rest.composites.schemas import (
    FOLDER_TEARDOWN_PARAMETER_SCHEMA,
    FOLDER_TEARDOWN_RESPONSE_SCHEMA,
)
from meho_backplane.operations._preview import blast_radius_missing_reason
from meho_backplane.operations._validate import validate_params
from tests._vmware_teardown_fake import (
    FolderTeardownFake,
    GateRecorder,
    moref,
    operator,
    parked,
    preview_ctx,
)

_HARD_OFF = "POST:/vcenter/vm/{vm}/power?action=stop"
_GUEST_OFF = "POST:/vcenter/vm/{vm}/guest/power?action=shutdown"
_REST_DELETE = "DELETE:/vcenter/vm/{vm}"
_VM_DESTROY = "POST:/VirtualMachine/{moId}/Destroy_Task"
_FOLDER_DESTROY = "POST:/Folder/{moId}/Destroy_Task"
_ALL_VMS = ["vm-101", "vm-102", "vm-103", "vm-104"]


@pytest.fixture
def gate(monkeypatch: pytest.MonkeyPatch) -> GateRecorder:
    recorder = GateRecorder()
    monkeypatch.setattr(_write, "enforce_subop_policy", recorder)
    return recorder


def _vm(conn: FolderTeardownFake, moid: str, power: str, **extra: Any) -> None:
    conn.add(
        "VirtualMachine",
        moid,
        name=f"name-{moid}",
        **{
            "runtime.powerState": power,
            "config.template": extra.pop("template", False),
            "config.hardware.device": [
                {
                    "_typeName": "VirtualDisk",
                    "capacityInBytes": 1024,
                    "backing": {"fileName": f"[example-ds] {moid}/{moid}.vmdk"},
                },
                {
                    "_typeName": "VirtualVmxnet3",
                    "macAddress": "00:50:56:00:00:01",
                    "deviceInfo": {"label": "Network adapter 1"},
                },
            ],
            **extra,
        },
    )


def _system(about: str = "9.0.0") -> FolderTeardownFake:
    """A made-up disposable system under ``group-v100``."""
    conn = FolderTeardownFake(about=about)
    conn.add("Datacenter", "datacenter-3", name="example-dc")
    conn.add(
        "Folder",
        "group-v4",
        name="vm",
        parent=moref("Datacenter", "datacenter-3"),
        childType=["Folder", "VirtualMachine", "VirtualApp"],
        childEntity=[moref("Folder", "group-v100")],
    )
    conn.add(
        "Folder",
        "group-v100",
        name="example-system",
        parent=moref("Folder", "group-v4"),
        childType=["Folder", "VirtualMachine", "VirtualApp"],
        childEntity=[
            moref("VirtualMachine", "vm-101"),
            moref("VirtualMachine", "vm-102"),
            moref("Folder", "group-v101"),
        ],
    )
    conn.add(
        "Folder",
        "group-v101",
        name="nested",
        childEntity=[
            moref("VirtualMachine", "vm-103"),
            moref("VirtualMachine", "vm-104"),
            moref("Folder", "group-v102"),
        ],
    )
    conn.add("Folder", "group-v102", name="empty", childEntity=[])
    _vm(
        conn,
        "vm-101",
        "poweredOn",
        snapshot={
            "rootSnapshotList": [
                {"name": "base", "snapshot": moref("VirtualMachineSnapshot", "snapshot-1")}
            ]
        },
    )
    _vm(conn, "vm-102", "poweredOff")
    _vm(conn, "vm-103", "poweredOn")
    _vm(conn, "vm-104", "poweredOff", template=True)
    return conn


async def _call(conn: FolderTeardownFake, **params: Any) -> Any:
    params.setdefault("folder", "group-v100")
    params.setdefault("expected_vms", list(_ALL_VMS))
    return await _folder_teardown.folder_teardown_composite(
        operator=operator(), target=object(), params=params, connector=conn
    )


async def _preview(conn: FolderTeardownFake, **params: Any) -> Any:
    params.setdefault("folder", "group-v100")
    params.setdefault("expected_vms", list(_ALL_VMS))
    return await _folder_teardown.folder_teardown_preview(preview_ctx(params, conn))


def _steps(gate: GateRecorder) -> list[tuple[str, str]]:
    return [(c["op_id"], next(iter(c["params"].values()))) for c in gate.calls]


def _validate(out: dict[str, Any]) -> None:
    Draft202012Validator(FOLDER_TEARDOWN_RESPONSE_SCHEMA).validate(out)


def test_parameter_schema_example_and_defaults() -> None:
    Draft202012Validator.check_schema(FOLDER_TEARDOWN_PARAMETER_SCHEMA)
    for example in FOLDER_TEARDOWN_PARAMETER_SCHEMA["examples"]:
        assert validate_params(FOLDER_TEARDOWN_PARAMETER_SCHEMA, example) == []
    assert validate_params(FOLDER_TEARDOWN_PARAMETER_SCHEMA, {"folder": "group-v1"}) != []
    assert validate_params(FOLDER_TEARDOWN_PARAMETER_SCHEMA, {"folder": "group-h1"}) != []
    for prop in FOLDER_TEARDOWN_PARAMETER_SCHEMA["properties"].values():
        assert prop["description"]


async def test_happy_path_rest_arm(gate: GateRecorder) -> None:
    conn = _system()
    out = await _call(conn)
    assert out["status"] == "deleted"
    _validate(out)
    # Power off the two running VMs, delete every VM (the template through
    # vim), then the folders bottom up.
    assert _steps(gate) == [
        (_HARD_OFF, "vm-101"),
        (_HARD_OFF, "vm-103"),
        (_REST_DELETE, "vm-101"),
        (_REST_DELETE, "vm-102"),
        (_REST_DELETE, "vm-103"),
        (_VM_DESTROY, "vm-104"),
        (_FOLDER_DESTROY, "group-v102"),
        (_FOLDER_DESTROY, "group-v101"),
        (_FOLDER_DESTROY, "group-v100"),
    ]
    rows = {r["moid"]: r for r in out["rows"]}
    assert set(rows) == {*_ALL_VMS, "group-v100", "group-v101", "group-v102"}
    assert {r["status"] for r in out["rows"]} == {"deleted"}
    assert rows["vm-101"]["powered_off"] is True
    assert rows["vm-102"]["powered_off"] is False
    assert rows["vm-104"]["kind"] == "template"
    assert out["counts"] == {
        "vms": 4,
        "folders": 2,
        "powered_off": 2,
        "deleted": 7,
        "unchanged": 0,
        "failed": 0,
    }
    assert ("Folder", "group-v100") not in conn.objects
    assert conn.objects[("Folder", "group-v4")]["childEntity"] == []


async def test_pre_9_vcenter_destroys_vms_through_vim(gate: GateRecorder) -> None:
    conn = _system(about="8.0.3")
    out = await _call(conn)
    assert out["status"] == "deleted"
    assert [s for s in _steps(gate) if s[0] == _VM_DESTROY] == [(_VM_DESTROY, v) for v in _ALL_VMS]
    assert [c for c in conn.rest_calls if c[0] == "DELETE"] == []


async def test_delete_folder_false_keeps_the_folder(gate: GateRecorder) -> None:
    conn = _system()
    out = await _call(conn, delete_folder=False)
    assert out["status"] == "deleted"
    assert (_FOLDER_DESTROY, "group-v100") not in _steps(gate)
    assert ("Folder", "group-v100") in conn.objects
    kept = next(r for r in out["rows"] if r["moid"] == "group-v100")
    assert kept["status"] == "unchanged"


async def test_top_folder_is_refused(gate: GateRecorder) -> None:
    conn = _system()
    out = await _call(conn, folder="group-v4")
    assert out["status"] == "precondition_failed"
    assert "top folder" in out["guidance"]
    assert gate.calls == []
    _validate(out)


async def test_vapp_inside_is_refused(gate: GateRecorder) -> None:
    conn = _system()
    conn.objects[("Folder", "group-v101")]["childEntity"].append(moref("VirtualApp", "resgroup-v9"))
    conn.add("VirtualApp", "resgroup-v9", name="example-vapp")
    out = await _call(conn)
    assert out["status"] == "precondition_failed"
    assert out["blockers"] == [
        {
            "kind": "VirtualApp",
            "moid": "resgroup-v9",
            "name": "example-vapp",
            "reason": "a vApp; delete the vApp first",
        }
    ]
    assert gate.calls == []
    assert conn.rest_calls == []
    _validate(out)


async def test_managed_vm_is_refused(gate: GateRecorder) -> None:
    conn = _system()
    conn.objects[("VirtualMachine", "vm-103")]["config.managedBy"] = {
        "extensionKey": "com.example.agent-manager",
        "type": "agent",
    }
    out = await _call(conn)
    assert out["status"] == "precondition_failed"
    assert [b["moid"] for b in out["blockers"]] == ["vm-103"]
    assert "com.example.agent-manager" in out["blockers"][0]["reason"]
    assert gate.calls == []


async def test_not_a_vm_folder_and_bad_input_are_refused(gate: GateRecorder) -> None:
    conn = _system()
    conn.objects[("Folder", "group-v100")]["childType"] = ["Folder", "Datastore"]
    out = await _call(conn)
    assert out["status"] == "invalid_request"
    out = await _call(conn, folder="../Folder/group-v4")
    assert out["status"] == "invalid_request"
    out = await _call(conn, expected_vms=["../vm-1"])
    assert out["status"] == "invalid_request"
    assert gate.calls == []
    assert conn.calls == [c for c in conn.calls if c[0].endswith("RetrievePropertiesEx")]


async def test_incomplete_answer_is_refused(gate: GateRecorder) -> None:
    conn = _system()
    conn.unreadable[("VirtualMachine", "vm-103")] = {"config.managedBy"}
    out = await _call(conn)
    assert out["status"] == "precondition_failed"
    assert "did not fully answer" in out["guidance"]
    assert gate.calls == []


@pytest.mark.parametrize("case", ["top", "vapp", "managed", "unexpected"])
async def test_preview_and_handler_refuse_the_same_blockers(gate: GateRecorder, case: str) -> None:
    conn = _system()
    params: dict[str, Any] = {}
    if case == "top":
        params["folder"] = "group-v4"
    elif case == "vapp":
        conn.objects[("Folder", "group-v100")]["childEntity"].append(
            moref("VirtualApp", "resgroup-v9")
        )
        conn.add("VirtualApp", "resgroup-v9", name="example-vapp")
    elif case == "managed":
        conn.objects[("VirtualMachine", "vm-101")]["config.managedBy"] = {"extensionKey": "x"}
    else:
        params["expected_vms"] = ["vm-101", "vm-102"]
    preview = (await _preview(conn, **params))["blast_radius"]
    out = await _call(conn, **params)
    assert preview["refusal"] == {"status": out["status"], "guidance": out["guidance"]}
    assert preview.get("blockers", []) == out["blockers"]
    assert gate.calls == []


async def test_folder_changed_after_the_preview_is_refused(gate: GateRecorder) -> None:
    """A VM moved into the folder after the approval request is never deleted."""
    conn = _system()
    preview = (await _preview(conn))["blast_radius"]
    assert "refusal" not in preview
    _vm(conn, "vm-105", "poweredOn")
    conn.objects[("Folder", "group-v101")]["childEntity"].append(moref("VirtualMachine", "vm-105"))
    out = await _call(conn)
    assert out["status"] == "precondition_failed"
    assert [b["moid"] for b in out["blockers"]] == ["vm-105"]
    assert "not in expected_vms" in out["blockers"][0]["reason"]
    assert gate.calls == []
    assert ("VirtualMachine", "vm-101") in conn.objects


async def test_one_vm_fails_to_delete(gate: GateRecorder) -> None:
    conn = _system(about="8.0.3")
    conn.destroy_faults["vm-102"] = ("InvalidState", "the VM is busy")
    with pytest.raises(RuntimeError) as err:
        await _call(conn)
    message = str(err.value)
    assert "the VM is busy" in message
    assert "Already deleted: vm-101." in message
    assert ("VirtualMachine", "vm-101") not in conn.objects
    assert ("VirtualMachine", "vm-102") in conn.objects
    assert ("Folder", "group-v100") in conn.objects
    assert all(op != _FOLDER_DESTROY for op, _ in _steps(gate))


async def test_second_run_is_unchanged(gate: GateRecorder) -> None:
    conn = _system()
    await _call(conn)
    gate.calls.clear()
    out = await _call(conn)
    assert out["status"] == "unchanged"
    assert {r["status"] for r in out["rows"]} == {"unchanged"}
    assert {r["moid"] for r in out["rows"]} == {"group-v100", *_ALL_VMS}
    assert gate.calls == []
    _validate(out)


async def test_second_run_after_delete_folder_false(gate: GateRecorder) -> None:
    conn = _system()
    await _call(conn, delete_folder=False)
    gate.calls.clear()
    out = await _call(conn, delete_folder=False)
    assert out["status"] == "unchanged"
    assert gate.calls == []
    assert {r["status"] for r in out["rows"]} == {"unchanged"}


async def test_guest_shutdown_then_hard_power_off(
    gate: GateRecorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(_folder_teardown, "_GUEST_POLL_INTERVAL_SECONDS", 0.0)
    conn = _system()
    conn.no_tools.add("vm-103")
    out = await _call(conn, power_off="guest", guest_timeout_s=0)
    assert out["status"] == "deleted"
    power_steps = [s for s in _steps(gate) if s[0] in (_GUEST_OFF, _HARD_OFF)]
    # vm-101 shuts down cleanly; vm-103 has no Tools, so it is powered off hard.
    assert power_steps == [(_GUEST_OFF, "vm-101"), (_GUEST_OFF, "vm-103"), (_HARD_OFF, "vm-103")]


async def test_guest_shutdown_timeout_falls_back_to_hard(
    gate: GateRecorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(_folder_teardown, "_GUEST_POLL_INTERVAL_SECONDS", 0.0)
    conn = _system()
    conn.ignore_shutdown.add("vm-101")
    out = await _call(conn, power_off="guest", guest_timeout_s=0)
    assert out["status"] == "deleted"
    assert (_HARD_OFF, "vm-101") in _steps(gate)
    assert (_HARD_OFF, "vm-103") not in _steps(gate)


async def test_parked_step_writes_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    result = parked(_HARD_OFF)
    monkeypatch.setattr(_write, "enforce_subop_policy", GateRecorder(result))
    conn = _system()
    out = await _call(conn)
    assert out is result
    assert conn.rest_calls == []
    assert conn.objects[("VirtualMachine", "vm-101")]["runtime.powerState"] == "poweredOn"


async def test_preview_blast_radius(gate: GateRecorder) -> None:
    conn = _system()
    effect = await _preview(conn)
    assert blast_radius_missing_reason(effect) is None
    radius = effect["blast_radius"]
    assert radius["object"]["present"] is True
    assert radius["irreversibility"] == "permanent"
    assert radius["power_off_count"] == 2
    assert radius["summary"].startswith("Powers off 2 running VM(s).")
    assert "3 VM(s)" in radius["summary"] and "1 template(s)" in radius["summary"]
    assert "3 folder(s)" in radius["summary"]
    children = {c["moid"]: c for c in radius["children"]}
    assert children["vm-101"]["disks"] == [
        {"file": "[example-ds] vm-101/vm-101.vmdk", "capacity_bytes": 1024}
    ]
    assert children["vm-101"]["nics"] == [
        {"label": "Network adapter 1", "mac": "00:50:56:00:00:01"}
    ]
    assert children["vm-101"]["snapshots"] == 1
    assert children["group-v102"]["depth"] == 2
    assert gate.calls == []
    assert conn.rest_calls == []


async def test_preview_declines_without_a_connector() -> None:
    ctx = preview_ctx({"folder": "group-v100", "expected_vms": []}, None)
    assert await _folder_teardown_plan.folder_teardown_preview(ctx) is None
