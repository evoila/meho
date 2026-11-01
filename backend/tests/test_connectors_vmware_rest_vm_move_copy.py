# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for the VM move and copy jobs: ``vm.relocate`` and ``vm.clone_from_vm``.

The handlers and previews against a vim fake: the exact VIM bodies, the gated
write, the ``accepted`` answer with the task id and the status read, every
check before a write (nothing is sent), a job that already runs, a parked
gate, a vendor fault on the start call, and a failed job read back through
``vmware.task.info``.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from jsonschema import Draft202012Validator

from meho_backplane.connectors.vmware_rest.composites import (
    _vm_clone_from_vm,
    _vm_relocate,
    _write,
)
from meho_backplane.connectors.vmware_rest.composites._vm_move_copy_schemas import (
    VM_CLONE_FROM_VM_PARAMETER_SCHEMA,
    VM_CLONE_FROM_VM_RESPONSE_SCHEMA,
    VM_RELOCATE_PARAMETER_SCHEMA,
    VM_RELOCATE_RESPONSE_SCHEMA,
)
from meho_backplane.connectors.vmware_rest.typed_ops_task_info import task_info_impl
from meho_backplane.operations._validate import validate_params
from tests._vmware_move_copy_fake import FOLDER, VM, MoveCopyVimFake, seed_vm
from tests._vmware_teardown_fake import (
    GateRecorder,
    http_status_error,
    moref,
    operator,
    parked,
    preview_ctx,
)

_RELOCATE_SUB_OP = "POST:/VirtualMachine/{moId}/RelocateVM_Task"
_CLONE_SUB_OP = "POST:/VirtualMachine/{moId}/CloneVM_Task"
_STATUS_READ = {"op_id": "vmware.task.info", "params": {"task": "task-1"}}


@pytest.fixture
def gate(monkeypatch: pytest.MonkeyPatch) -> GateRecorder:
    recorder = GateRecorder()
    monkeypatch.setattr(_write, "enforce_subop_policy", recorder)
    return recorder


async def _relocate(conn: MoveCopyVimFake, **params: Any) -> Any:
    return await _vm_relocate.vm_relocate_composite(
        operator=operator(), target=object(), params={"vm": VM, **params}, connector=conn
    )


async def _clone(conn: MoveCopyVimFake, **params: Any) -> Any:
    return await _vm_clone_from_vm.vm_clone_from_vm_composite(
        operator=operator(),
        target=object(),
        params={"source_vm": VM, "name": "<new-vm-name>", **params},
        connector=conn,
    )


def _ds(moid: str) -> dict[str, str]:
    return moref("Datastore", moid)


# ===========================================================================
# vm.relocate
# ===========================================================================


async def test_relocate_starts_the_task_and_answers_accepted(gate: GateRecorder) -> None:
    conn = MoveCopyVimFake()
    seed_vm(conn)
    out = await _relocate(
        conn, datastore="datastore-12", resource_pool="resgroup-9", priority="highPriority"
    )

    assert out["status"] == "accepted"
    assert out["task"] == "task-1"
    assert out["status_read"] == _STATUS_READ
    assert out["vm_name"] == "<vm-name>"
    assert out["before"] == {
        "power_state": "poweredOn",
        "host": "host-21",
        "resource_pool": "resgroup-8",
        "folder": FOLDER,
        "datastores": ["datastore-11"],
    }
    assert out["requested"] == {"datastore": "datastore-12", "resource_pool": "resgroup-9"}
    assert "not done" in out["guidance"]
    Draft202012Validator(VM_RELOCATE_RESPONSE_SCHEMA).validate(out)
    [(path, body)] = conn.calls_to("RelocateVM_Task")
    assert path == f"/VirtualMachine/{VM}/RelocateVM_Task"
    assert body == {
        "spec": {
            "_typeName": "VirtualMachineRelocateSpec",
            "datastore": _ds("datastore-12"),
            "pool": moref("ResourcePool", "resgroup-9"),
        },
        "priority": "highPriority",
    }
    assert [c["op_id"] for c in gate.calls] == [_RELOCATE_SUB_OP]
    assert gate.calls[0]["params"] == {
        "vm": VM,
        "datastore": "datastore-12",
        "resource_pool": "resgroup-9",
    }


async def test_relocate_sends_each_disk_to_its_own_datastore(gate: GateRecorder) -> None:
    conn = MoveCopyVimFake()
    seed_vm(conn)
    out = await _relocate(
        conn,
        folder="group-v7",
        host="host-22",
        disks=[{"disk_key": 2001, "datastore": "datastore-13"}],
    )
    assert out["status"] == "accepted"
    [(_path, body)] = conn.calls_to("RelocateVM_Task")
    assert body == {
        "spec": {
            "_typeName": "VirtualMachineRelocateSpec",
            "folder": moref("Folder", "group-v7"),
            "host": moref("HostSystem", "host-22"),
            "disk": [
                {
                    "_typeName": "VirtualMachineRelocateSpecDiskLocator",
                    "diskId": 2001,
                    "datastore": _ds("datastore-13"),
                }
            ],
        }
    }


async def test_relocate_to_where_the_vm_is_sends_nothing(gate: GateRecorder) -> None:
    conn = MoveCopyVimFake()
    seed_vm(conn)
    out = await _relocate(
        conn,
        datastore="datastore-11",
        resource_pool="resgroup-8",
        folder=FOLDER,
        host="host-21",
        disks=[{"disk_key": 2000, "datastore": "datastore-11"}],
    )
    assert out["status"] == "unchanged"
    assert out["task"] is None and out["status_read"] is None
    assert conn.starts() == [] and gate.calls == []


async def test_relocate_of_an_unknown_disk_key_sends_nothing(gate: GateRecorder) -> None:
    conn = MoveCopyVimFake()
    seed_vm(conn)
    out = await _relocate(conn, disks=[{"disk_key": 4000, "datastore": "datastore-12"}])
    assert out["status"] == "invalid_request"
    assert "4000" in out["guidance"]
    assert conn.starts() == []


async def test_relocate_of_an_unknown_vm_is_not_found(gate: GateRecorder) -> None:
    conn = MoveCopyVimFake()
    out = await _relocate(conn, datastore="datastore-12")
    assert out["status"] == "not_found"
    assert conn.starts() == []


@pytest.mark.parametrize(
    "params",
    [
        {"vm": "<vm-name>", "datastore": "datastore-12"},
        {"datastore": "datastore/../x"},
        {"resource_pool": "domain-c7"},
        {"folder": "group-h4"},
    ],
)
async def test_relocate_checks_every_moid_before_any_call(
    gate: GateRecorder, params: dict[str, Any]
) -> None:
    conn = MoveCopyVimFake()
    out = await _relocate(conn, **params)
    assert out["status"] == "invalid_request"
    assert conn.calls == []


async def test_relocate_returns_a_move_that_still_runs(gate: GateRecorder) -> None:
    conn = MoveCopyVimFake()
    seed_vm(conn, recentTask=[moref("Task", "task-7"), moref("Task", "task-8")])
    conn.task("task-7", state="success", descriptionId="VirtualMachine.relocate")
    conn.task("task-8", state="running", descriptionId="VirtualMachine.relocate")
    out = await _relocate(conn, datastore="datastore-12")
    assert out["status"] == "already_running"
    assert out["task"] == "task-8"
    assert out["status_read"] == {"op_id": "vmware.task.info", "params": {"task": "task-8"}}
    assert conn.starts() == [] and gate.calls == []


async def test_relocate_is_not_blocked_by_other_or_ended_tasks(gate: GateRecorder) -> None:
    conn = MoveCopyVimFake()
    seed_vm(conn, recentTask=[moref("Task", "task-7"), moref("Task", "task-8")])
    conn.task("task-7", state="success", descriptionId="VirtualMachine.relocate")
    conn.task("task-8", state="running", descriptionId="VirtualMachine.clone")
    out = await _relocate(conn, datastore="datastore-12")
    assert out["status"] == "accepted"
    assert len(conn.starts()) == 1


async def test_relocate_returns_a_parked_gate_as_is() -> None:
    conn = MoveCopyVimFake()
    seed_vm(conn)
    gate = GateRecorder(parked(_RELOCATE_SUB_OP))
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(_write, "enforce_subop_policy", gate)
        out = await _relocate(conn, datastore="datastore-12")
    assert out.status == "awaiting_approval"
    assert conn.starts() == []


async def test_relocate_start_fault_reaches_the_caller(gate: GateRecorder) -> None:
    conn = MoveCopyVimFake()
    seed_vm(conn)
    conn.start_fault = http_status_error(
        500,
        '{"_typeName": "InvalidArgument", "faultstring": "A parameter was not correct"}',
    )
    with pytest.raises(httpx.HTTPStatusError) as raised:
        await _relocate(conn, datastore="datastore-12")
    assert "InvalidArgument" in raised.value.response.text


async def test_a_failed_move_reads_back_with_the_vendor_error(gate: GateRecorder) -> None:
    conn = MoveCopyVimFake()
    seed_vm(conn)
    out = await _relocate(conn, datastore="datastore-12")
    conn.task(
        out["task"],
        state="error",
        descriptionId="VirtualMachine.relocate",
        error={
            "_typeName": "LocalizedMethodFault",
            "fault": {"_typeName": "InsufficientDisk"},
            "localizedMessage": "Insufficient disk space on datastore.",
        },
    )
    status = await task_info_impl(
        conn,  # type: ignore[arg-type]
        operator(),
        SimpleNamespace(name="vc"),  # type: ignore[arg-type]
        out["status_read"]["params"],
    )
    assert status["state"] == "error"
    assert status["done"] is True
    assert status["error_type"] == "InsufficientDisk"
    assert status["error_message"] == "Insufficient disk space on datastore."


async def test_relocate_preview_shows_the_copy_and_the_targets() -> None:
    conn = MoveCopyVimFake()
    seed_vm(conn, **{"summary.storage": {"committed": 10 * 1024**3, "uncommitted": 0}})
    conn.add("Datastore", "datastore-11", summary={"name": "<ds-a>", "freeSpace": 1})
    conn.add(
        "Datastore",
        "datastore-12",
        summary={"name": "<ds-b>", "capacity": 100 * 1024**3, "freeSpace": 50 * 1024**3},
    )
    out = await _vm_relocate.vm_relocate_preview_builder(
        preview_ctx({"vm": VM, "datastore": "datastore-12", "folder": "group-v7"}, conn)
    )
    assert out is not None
    assert out["action"] == "relocate_vm"
    assert out["requested"] == {"datastore": "datastore-12", "folder": "group-v7"}
    assert out["copies_disks"] is True
    assert out["copied_now_bytes"] == 10 * 1024**3
    assert out["target_datastores"][0]["free_space_after_copy_bytes"] == 40 * 1024**3
    assert conn.starts() == []


async def test_relocate_preview_without_a_datastore_copies_nothing() -> None:
    out = await _vm_relocate.vm_relocate_preview_builder(
        preview_ctx({"vm": VM, "resource_pool": "resgroup-9"}, MoveCopyVimFake())
    )
    assert out is not None
    assert out["copies_disks"] is False
    assert out["requested"] == {"resource_pool": "resgroup-9"}


def test_relocate_schema_needs_a_target_and_takes_the_example() -> None:
    for example in VM_RELOCATE_PARAMETER_SCHEMA["examples"]:
        validate_params(VM_RELOCATE_PARAMETER_SCHEMA, example)
    validator = Draft202012Validator(VM_RELOCATE_PARAMETER_SCHEMA)
    assert not validator.is_valid({"vm": VM})
    assert validator.is_valid({"vm": VM, "disks": [{"disk_key": 2000, "datastore": "datastore-2"}]})
    assert not validator.is_valid({"vm": VM, "datastore": "<datastore-name>"})


# ===========================================================================
# vm.clone_from_vm
# ===========================================================================


async def test_full_copy_starts_the_task_in_the_source_folder(gate: GateRecorder) -> None:
    conn = MoveCopyVimFake()
    seed_vm(conn)
    out = await _clone(conn)

    assert out["status"] == "accepted"
    assert out["task"] == "task-1"
    assert out["status_read"] == _STATUS_READ
    assert out["folder"] == FOLDER
    assert out["clone_type"] == "full"
    assert out["source_vm_name"] == "<vm-name>"
    Draft202012Validator(VM_CLONE_FROM_VM_RESPONSE_SCHEMA).validate(out)
    [(path, body)] = conn.calls_to("CloneVM_Task")
    assert path == f"/VirtualMachine/{VM}/CloneVM_Task"
    assert body == {
        "folder": moref("Folder", FOLDER),
        "name": "<new-vm-name>",
        "spec": {
            "_typeName": "VirtualMachineCloneSpec",
            "location": {"_typeName": "VirtualMachineRelocateSpec"},
            "template": False,
            "powerOn": False,
        },
    }
    assert [c["op_id"] for c in gate.calls] == [_CLONE_SUB_OP]
    assert gate.calls[0]["params"]["folder"] == FOLDER


async def test_linked_copy_builds_on_the_snapshot(gate: GateRecorder) -> None:
    conn = MoveCopyVimFake()
    tree = {
        "rootSnapshotList": [
            {
                "snapshot": moref("VirtualMachineSnapshot", "snapshot-7"),
                "name": "<snapshot-name>",
            }
        ]
    }
    seed_vm(conn, snapshot=tree)
    conn.add("Folder", "group-v9", childEntity=[])
    out = await _clone(
        conn,
        clone_type="linked",
        snapshot="snapshot-7",
        folder="group-v9",
        resource_pool="resgroup-9",
        datastore="datastore-12",
        host="host-22",
        power_on=True,
    )
    assert out["status"] == "accepted"
    [(_path, body)] = conn.calls_to("CloneVM_Task")
    assert body == {
        "folder": moref("Folder", "group-v9"),
        "name": "<new-vm-name>",
        "spec": {
            "_typeName": "VirtualMachineCloneSpec",
            "location": {
                "_typeName": "VirtualMachineRelocateSpec",
                "datastore": _ds("datastore-12"),
                "pool": moref("ResourcePool", "resgroup-9"),
                "host": moref("HostSystem", "host-22"),
                "diskMoveType": "createNewChildDiskBacking",
            },
            "template": False,
            "powerOn": True,
            "snapshot": moref("VirtualMachineSnapshot", "snapshot-7"),
        },
    }


@pytest.mark.parametrize(
    ("params", "seed", "word"),
    [
        ({"clone_type": "linked"}, {}, "snapshot"),
        ({"snapshot": "snapshot-99"}, {}, "snapshot-99"),
        ({}, {"config.template": True}, "clone_from_template"),
        ({"folder": "group-v77"}, {}, "group-v77"),
        ({}, {"parent": moref("VirtualApp", "resgroup-v5")}, "pass folder"),
        ({"source_vm": "<vm-name>"}, {}, "source_vm"),
    ],
)
async def test_copy_refusals_send_nothing(
    gate: GateRecorder, params: dict[str, Any], seed: dict[str, Any], word: str
) -> None:
    conn = MoveCopyVimFake()
    seed_vm(conn, **seed)
    out = await _clone(conn, **params)
    assert out["status"] == "invalid_request"
    assert word in out["guidance"]
    assert conn.starts() == [] and gate.calls == []


async def test_copy_of_an_unknown_vm_is_not_found(gate: GateRecorder) -> None:
    conn = MoveCopyVimFake()
    out = await _clone(conn)
    assert out["status"] == "not_found"
    assert conn.starts() == []


async def test_copy_to_a_name_the_folder_has_sends_nothing(gate: GateRecorder) -> None:
    conn = MoveCopyVimFake()
    seed_vm(conn)
    conn.add("VirtualMachine", "vm-43", name="<new-vm-name>")
    conn.add(
        "Folder",
        FOLDER,
        childEntity=[moref("VirtualMachine", VM), moref("VirtualMachine", "vm-43")],
    )
    out = await _clone(conn)
    assert out["status"] == "already_exists"
    assert out["existing_vm"] == "vm-43"
    assert conn.starts() == []


async def test_copy_returns_a_copy_that_still_runs(gate: GateRecorder) -> None:
    conn = MoveCopyVimFake()
    seed_vm(conn, recentTask=[moref("Task", "task-5")])
    conn.task("task-5", state="queued", descriptionId="VirtualMachine.clone")
    out = await _clone(conn)
    assert out["status"] == "already_running"
    assert out["task"] == "task-5"
    assert conn.starts() == []


async def test_copy_returns_a_parked_gate_as_is() -> None:
    conn = MoveCopyVimFake()
    seed_vm(conn)
    gate = GateRecorder(parked(_CLONE_SUB_OP))
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(_write, "enforce_subop_policy", gate)
        out = await _clone(conn)
    assert out.status == "awaiting_approval"
    assert conn.starts() == []


async def test_copy_start_fault_reaches_the_caller(gate: GateRecorder) -> None:
    conn = MoveCopyVimFake()
    seed_vm(conn)
    conn.start_fault = http_status_error(
        500, '{"_typeName": "DuplicateName", "faultstring": "The name already exists."}'
    )
    with pytest.raises(httpx.HTTPStatusError) as raised:
        await _clone(conn)
    assert "DuplicateName" in raised.value.response.text


async def test_copy_preview_echoes_the_request() -> None:
    out = await _vm_clone_from_vm.vm_clone_from_vm_preview(
        preview_ctx(
            {"source_vm": VM, "name": "<new-vm-name>", "clone_type": "linked"},
            MoveCopyVimFake(),
        )
    )
    assert out == {
        "action": "clone_vm",
        "source_vm": VM,
        "name": "<new-vm-name>",
        "clone_type": "linked",
        "snapshot": None,
        "folder": None,
        "resource_pool": None,
        "datastore": None,
        "host": None,
        "power_on": False,
    }


def test_copy_schema_needs_a_snapshot_for_a_linked_copy() -> None:
    for example in VM_CLONE_FROM_VM_PARAMETER_SCHEMA["examples"]:
        validate_params(VM_CLONE_FROM_VM_PARAMETER_SCHEMA, example)
    validator = Draft202012Validator(VM_CLONE_FROM_VM_PARAMETER_SCHEMA)
    base = {"source_vm": VM, "name": "<new-vm-name>"}
    assert validator.is_valid(base)
    assert not validator.is_valid({**base, "clone_type": "linked"})
    assert validator.is_valid({**base, "clone_type": "linked", "snapshot": "snapshot-7"})
    assert validator.is_valid({**base, "snapshot": "snapshot-7"})
