# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for the VM snapshot writes: ``vm.snapshot.create``, ``vm.snapshot.delete``
and the snapshot-id input of ``vm.snapshot.revert``.

Two layers:

* the handlers and previews against the shared vim fake, extended here with the
  three snapshot tasks: the exact vim bodies, the gated write, the task poll, the
  read of the tree after the task, every refusal before a write, a task fault,
  the poll timeout, a parked gate, and the approval-card blast radius;
* the governed flow through the real dispatcher: create waits for approval and
  runs once a second person approves; delete needs a preview, waits for
  approval, shows the snapshot on the approval card, and is refused for an
  agent.
"""

from __future__ import annotations

import copy
import uuid
from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
from jsonschema import Draft202012Validator
from sqlalchemy import func, select

from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.connectors.registry import clear_registry, register_connector_v2
from meho_backplane.connectors.vmware_rest import VmwareRestConnector
from meho_backplane.connectors.vmware_rest.composites import (
    _snapshot,
    _write,
    register_vmware_composite_operations,
)
from meho_backplane.connectors.vmware_rest.composites._write import vm_snapshot_revert_composite
from meho_backplane.connectors.vmware_rest.composites.schemas import (
    VM_SNAPSHOT_CREATE_PARAMETER_SCHEMA,
    VM_SNAPSHOT_CREATE_RESPONSE_SCHEMA,
    VM_SNAPSHOT_DELETE_PARAMETER_SCHEMA,
    VM_SNAPSHOT_DELETE_RESPONSE_SCHEMA,
    VM_SNAPSHOT_REVERT_PARAMETER_SCHEMA,
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
    moref,
    operator,
    parked,
    preview_ctx,
)

_VM = "vm-42"
_CREATE_OP = "vmware.composite.vm.snapshot.create"
_DELETE_OP = "vmware.composite.vm.snapshot.delete"
_CREATE_SUB_OP = "POST:/VirtualMachine/{moId}/CreateSnapshotEx_Task"
_REMOVE_SUB_OP = "POST:/VirtualMachineSnapshot/{moId}/RemoveSnapshot_Task"
_REVERT_SUB_OP = "POST:/VirtualMachineSnapshot/{moId}/RevertToSnapshot_Task"


class SnapshotVimFake(VimFake):
    """The shared vim fake plus the three snapshot tasks.

    The tasks change the VM's ``snapshot`` property the way vCenter does, so the
    handlers' reads after the task see the result.
    """

    def __init__(self) -> None:
        super().__init__()
        self._next_id = 100

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
        if method == "CreateSnapshotEx_Task":
            self.calls.append((path, copy.deepcopy(json)))
            new_id = f"snapshot-{self._next_id}"
            self._next_id += 1
            return self._task(
                method,
                apply=lambda: self._add(moid, new_id, json),
                result=moref("VirtualMachineSnapshot", new_id),
            )
        if method == "RemoveSnapshot_Task":
            self.calls.append((path, copy.deepcopy(json)))
            return self._task(method, apply=lambda: self._remove(moid, json["removeChildren"]))
        if method == "RevertToSnapshot_Task":
            self.calls.append((path, copy.deepcopy(json)))
            return self._task(method, apply=lambda: self._info_of(moid).update(current(moid)))
        return await super()._post_vmomi_json(
            target,
            path,
            operator=operator,
            json=json,
            promote_managed_object_not_found=promote_managed_object_not_found,
        )

    # -- snapshot tree helpers -------------------------------------------------
    def _props_of(self, snapshot_id: str) -> dict[str, Any]:
        """The properties of the VM that owns *snapshot_id*."""
        for (mo_type, _moid), props in self.objects.items():
            info = props.get("snapshot")
            if (
                mo_type == "VirtualMachine"
                and info
                and _find(info["rootSnapshotList"], snapshot_id)
            ):
                return props
        raise AssertionError(f"no VM has snapshot {snapshot_id}")

    def _info_of(self, snapshot_id: str) -> dict[str, Any]:
        info: dict[str, Any] = self._props_of(snapshot_id)["snapshot"]
        return info

    def _add(self, vm: str, new_id: str, body: dict[str, Any]) -> None:
        props = self.objects[("VirtualMachine", vm)]
        info = props.setdefault("snapshot", {"rootSnapshotList": []})
        node = tree_node(
            new_id, body["name"], state="poweredOn" if body["memory"] else "poweredOff"
        )
        node["quiesced"] = "quiesceSpec" in body
        node["description"] = body.get("description", "")
        parent = _find(info["rootSnapshotList"], _current_id(info)) if _current_id(info) else None
        if parent is None:
            info["rootSnapshotList"].append(node)
        else:
            parent.setdefault("childSnapshotList", []).append(node)
        info.update(current(new_id))

    def _remove(self, snapshot_id: str, remove_children: bool) -> None:
        props = self._props_of(snapshot_id)
        info = props["snapshot"]
        _remove_node(info, info["rootSnapshotList"], None, snapshot_id, remove_children)
        if not info["rootSnapshotList"]:
            # Like vCenter, a VM with no snapshots left has no ``snapshot`` property.
            del props["snapshot"]


def tree_node(
    snapshot_id: str,
    name: str,
    children: list[dict[str, Any]] | None = None,
    *,
    state: str = "poweredOff",
) -> dict[str, Any]:
    """A ``VirtualMachineSnapshotTree`` node as VI-JSON returns it."""
    node: dict[str, Any] = {
        "_typeName": "VirtualMachineSnapshotTree",
        "snapshot": moref("VirtualMachineSnapshot", snapshot_id),
        "vm": moref("VirtualMachine", _VM),
        "name": name,
        "description": "",
        "createTime": "2026-10-01T08:00:00.000Z",
        "state": state,
        "quiesced": False,
    }
    if children:
        node["childSnapshotList"] = children
    return node


def current(snapshot_id: str) -> dict[str, Any]:
    return {"currentSnapshot": moref("VirtualMachineSnapshot", snapshot_id)}


def _current_id(info: dict[str, Any]) -> str | None:
    ref = info.get("currentSnapshot")
    return ref["value"] if ref else None


def _find(nodes: list[dict[str, Any]], snapshot_id: str | None) -> dict[str, Any] | None:
    for node in nodes:
        if node["snapshot"]["value"] == snapshot_id:
            return node
        found = _find(node.get("childSnapshotList", []), snapshot_id)
        if found is not None:
            return found
    return None


def _remove_node(
    info: dict[str, Any],
    nodes: list[dict[str, Any]],
    parent: dict[str, Any] | None,
    snapshot_id: str,
    remove_children: bool,
) -> bool:
    for i, node in enumerate(nodes):
        if node["snapshot"]["value"] == snapshot_id:
            kept = [] if remove_children else node.get("childSnapshotList", [])
            nodes[i : i + 1] = kept
            if _current_id(info) == snapshot_id or (
                remove_children and _find([node], _current_id(info))
            ):
                if parent is None:
                    info.pop("currentSnapshot", None)
                else:
                    info.update(current(parent["snapshot"]["value"]))
            return True
        if _remove_node(
            info, node.get("childSnapshotList", []), node, snapshot_id, remove_children
        ):
            return True
    return False


def _vm(conn: SnapshotVimFake, roots: list[dict[str, Any]] | None, current_id: str | None) -> None:
    """Seed the VM; ``roots=None`` means a VM without snapshots."""
    props: dict[str, Any] = {"name": "app-vm"}
    if roots is not None:
        info: dict[str, Any] = {
            "_typeName": "VirtualMachineSnapshotInfo",
            "rootSnapshotList": roots,
        }
        if current_id is not None:
            info.update(current(current_id))
        props["snapshot"] = info
    conn.add("VirtualMachine", _VM, **props)


def _two_level_tree() -> list[dict[str, Any]]:
    """base (snapshot-1) -> before-patch (snapshot-2) -> after-patch (snapshot-3)."""
    return [
        tree_node(
            "snapshot-1",
            "base",
            [tree_node("snapshot-2", "before-patch", [tree_node("snapshot-3", "after-patch")])],
        )
    ]


def _names(conn: SnapshotVimFake) -> list[str]:
    info = conn.objects[("VirtualMachine", _VM)].get("snapshot")
    out: list[str] = []

    def walk(nodes: list[dict[str, Any]]) -> None:
        for node in nodes:
            out.append(node["name"])
            walk(node.get("childSnapshotList", []))

    walk(info["rootSnapshotList"] if info else [])
    return out


@pytest.fixture
def gate(monkeypatch: pytest.MonkeyPatch) -> GateRecorder:
    recorder = GateRecorder()
    monkeypatch.setattr(_write, "enforce_subop_policy", recorder)
    return recorder


async def _create(conn: SnapshotVimFake, **params: Any) -> Any:
    return await _snapshot.vm_snapshot_create_composite(
        operator=operator(), target=object(), params={"vm": _VM, **params}, connector=conn
    )


async def _delete(conn: SnapshotVimFake, **params: Any) -> Any:
    return await _snapshot.vm_snapshot_delete_composite(
        operator=operator(), target=object(), params={"vm": _VM, **params}, connector=conn
    )


# ===========================================================================
# vm.snapshot.create
# ===========================================================================


async def test_create_happy_path(gate: GateRecorder) -> None:
    conn = SnapshotVimFake()
    _vm(conn, None, None)
    out = await _create(conn, snapshot_name="before-reset")

    assert out["status"] == "created"
    assert out["snapshot_id"] == "snapshot-100"
    assert out["vm_name"] == "app-vm"
    assert out["task_state"] == "success"
    assert out["snapshot"] == {
        "snapshot_id": "snapshot-100",
        "name": "before-reset",
        "description": "",
        "created": "2026-10-01T08:00:00.000Z",
        "power_state": "poweredOff",
        "quiesced": False,
        "current": True,
        "parent_snapshot_id": None,
    }
    Draft202012Validator(VM_SNAPSHOT_CREATE_RESPONSE_SCHEMA).validate(out)
    # One gated write; disks only and no quiescing by default.
    assert [c["op_id"] for c in gate.calls] == [_CREATE_SUB_OP]
    assert gate.calls[0]["params"] == {
        "vm": _VM,
        "snapshot_name": "before-reset",
        "memory": False,
        "quiesce": False,
    }
    [(path, body)] = conn.calls_to("CreateSnapshotEx_Task")
    assert path == f"/VirtualMachine/{_VM}/CreateSnapshotEx_Task"
    assert body == {"name": "before-reset", "memory": False}


async def test_create_with_memory_quiesce_and_description(gate: GateRecorder) -> None:
    conn = SnapshotVimFake()
    _vm(conn, _two_level_tree(), "snapshot-3")
    out = await _create(conn, snapshot_name="full", description="why", memory=True, quiesce=True)

    assert out["status"] == "created"
    [(_path, body)] = conn.calls_to("CreateSnapshotEx_Task")
    assert body == {
        "name": "full",
        "memory": True,
        "description": "why",
        "quiesceSpec": {"_typeName": "VirtualMachineGuestQuiesceSpec"},
    }
    # The new snapshot builds on the current one and becomes current.
    assert out["snapshot"]["parent_snapshot_id"] == "snapshot-3"
    assert out["snapshot"]["power_state"] == "poweredOn"
    assert out["snapshot"]["quiesced"] is True
    assert gate.calls[0]["params"]["memory"] is True
    assert gate.calls[0]["params"]["quiesce"] is True


async def test_create_with_a_name_in_use_changes_nothing(gate: GateRecorder) -> None:
    conn = SnapshotVimFake()
    _vm(conn, _two_level_tree(), "snapshot-3")
    out = await _create(conn, snapshot_name="before-patch")

    assert out["status"] == "unchanged"
    assert out["snapshot_id"] == "snapshot-2"
    assert out["snapshot"]["name"] == "before-patch"
    Draft202012Validator(VM_SNAPSHOT_CREATE_RESPONSE_SCHEMA).validate(out)
    assert gate.calls == []
    assert conn.calls_to("CreateSnapshotEx_Task") == []


async def test_create_on_an_unknown_vm_is_not_found(gate: GateRecorder) -> None:
    conn = SnapshotVimFake()
    out = await _create(conn, snapshot_name="x")
    assert out["status"] == "not_found"
    assert gate.calls == []
    assert conn.calls_to("CreateSnapshotEx_Task") == []


async def test_create_refuses_a_vm_name_as_moid(gate: GateRecorder) -> None:
    conn = SnapshotVimFake()
    out = await _snapshot.vm_snapshot_create_composite(
        operator=operator(),
        target=object(),
        params={"vm": "app-vm", "snapshot_name": "x"},
        connector=conn,
    )
    assert out["status"] == "invalid_request"
    assert conn.calls == []


async def test_create_task_fault_raises_with_the_fault_text(gate: GateRecorder) -> None:
    conn = SnapshotVimFake()
    _vm(conn, None, None)
    conn.task_faults["CreateSnapshotEx_Task"] = (
        "InvalidState",
        "The operation is not allowed in the current state.",
    )
    with pytest.raises(RuntimeError, match="not allowed in the current state"):
        await _create(conn, snapshot_name="x")


async def test_create_timeout_reports_an_error_issue(
    gate: GateRecorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(_snapshot, "_TASK_TIMEOUT_SECONDS", 0.0)
    conn = SnapshotVimFake()
    _vm(conn, None, None)
    conn.running.add("CreateSnapshotEx_Task")
    out = await _create(conn, snapshot_name="x")

    assert out["status"] == "timeout"
    assert out["task"] == "task-1"
    assert [i["severity"] for i in out["issues"]] == ["error"]
    Draft202012Validator(VM_SNAPSHOT_CREATE_RESPONSE_SCHEMA).validate(out)


async def test_create_returns_a_parked_gate_as_is() -> None:
    conn = SnapshotVimFake()
    _vm(conn, None, None)
    verdict = parked(_CREATE_SUB_OP)
    recorder = GateRecorder(verdict)
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(_write, "enforce_subop_policy", recorder)
        out = await _create(conn, snapshot_name="x")
    assert out is verdict
    assert conn.calls_to("CreateSnapshotEx_Task") == []


async def test_create_without_a_read_back_still_names_the_new_id(gate: GateRecorder) -> None:
    conn = SnapshotVimFake()
    _vm(conn, None, None)
    conn.apply_writes = False
    out = await _create(conn, snapshot_name="x")
    assert out["status"] == "created"
    assert out["snapshot_id"] == "snapshot-100"
    assert out["snapshot"] is None
    assert "does not read back" in out["guidance"]


async def test_create_preview_echoes_the_request() -> None:
    effect = await _snapshot.vm_snapshot_create_preview(
        preview_ctx({"vm": _VM, "snapshot_name": "x", "memory": True}, None)
    )
    assert effect == {
        "action": "create_vm_snapshot",
        "vm": _VM,
        "snapshot_name": "x",
        "memory": True,
        "quiesce": False,
    }
    assert await _snapshot.vm_snapshot_create_preview(preview_ctx({"vm": _VM}, None)) is None


# ===========================================================================
# vm.snapshot.delete
# ===========================================================================


async def test_delete_keeps_the_children_by_default(gate: GateRecorder) -> None:
    conn = SnapshotVimFake()
    _vm(conn, _two_level_tree(), "snapshot-3")
    out = await _delete(conn, snapshot_id="snapshot-2")

    assert out["status"] == "deleted"
    assert out["object"] == {
        "kind": "vm_snapshot",
        "vm": _VM,
        "vm_name": "app-vm",
        "snapshot_id": "snapshot-2",
        "remove_children": False,
        "name": "before-patch",
        "description": "",
        "created": "2026-10-01T08:00:00.000Z",
        "power_state": "poweredOff",
        "current": False,
        "child_count": 1,
    }
    Draft202012Validator(VM_SNAPSHOT_DELETE_RESPONSE_SCHEMA).validate(out)
    assert [c["op_id"] for c in gate.calls] == [_REMOVE_SUB_OP]
    assert gate.calls[0]["params"] == {
        "vm": _VM,
        "snapshot_id": "snapshot-2",
        "remove_children": False,
    }
    [(path, body)] = conn.calls_to("RemoveSnapshot_Task")
    assert path == "/VirtualMachineSnapshot/snapshot-2/RemoveSnapshot_Task"
    assert body == {"removeChildren": False}
    assert _names(conn) == ["base", "after-patch"]


async def test_delete_with_remove_children_takes_the_subtree(gate: GateRecorder) -> None:
    conn = SnapshotVimFake()
    _vm(conn, _two_level_tree(), "snapshot-3")
    out = await _delete(conn, snapshot_id="snapshot-2", remove_children=True)

    assert out["status"] == "deleted"
    [(_path, body)] = conn.calls_to("RemoveSnapshot_Task")
    assert body == {"removeChildren": True}
    assert _names(conn) == ["base"]


async def test_delete_of_an_absent_snapshot_changes_nothing(gate: GateRecorder) -> None:
    conn = SnapshotVimFake()
    _vm(conn, _two_level_tree(), "snapshot-3")
    out = await _delete(conn, snapshot_id="snapshot-9")
    assert out["status"] == "unchanged"
    Draft202012Validator(VM_SNAPSHOT_DELETE_RESPONSE_SCHEMA).validate(out)
    assert gate.calls == []
    assert conn.calls_to("RemoveSnapshot_Task") == []


async def test_delete_on_an_unknown_vm_is_refused(gate: GateRecorder) -> None:
    conn = SnapshotVimFake()
    out = await _delete(conn, snapshot_id="snapshot-2")
    assert out["status"] == "invalid_request"
    assert gate.calls == []


@pytest.mark.parametrize(
    "params",
    [
        {"vm": "app-vm", "snapshot_id": "snapshot-2"},
        {"vm": _VM, "snapshot_id": "../Folder/group-v1"},
    ],
)
async def test_delete_checks_both_moids_before_any_call(
    gate: GateRecorder, params: dict[str, Any]
) -> None:
    conn = SnapshotVimFake()
    out = await _snapshot.vm_snapshot_delete_composite(
        operator=operator(), target=object(), params=params, connector=conn
    )
    assert out["status"] == "invalid_request"
    assert conn.calls == []


async def test_delete_task_fault_raises(gate: GateRecorder) -> None:
    conn = SnapshotVimFake()
    _vm(conn, _two_level_tree(), "snapshot-3")
    conn.task_faults["RemoveSnapshot_Task"] = ("TaskInProgress", "The VM is busy.")
    with pytest.raises(RuntimeError, match="The VM is busy"):
        await _delete(conn, snapshot_id="snapshot-2")


async def test_delete_timeout(gate: GateRecorder, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_snapshot, "_TASK_TIMEOUT_SECONDS", 0.0)
    conn = SnapshotVimFake()
    _vm(conn, _two_level_tree(), "snapshot-3")
    conn.running.add("RemoveSnapshot_Task")
    out = await _delete(conn, snapshot_id="snapshot-2")
    assert out["status"] == "timeout"
    assert out["task"] == "task-1"


async def test_delete_that_still_reads_back_is_still_present(gate: GateRecorder) -> None:
    conn = SnapshotVimFake()
    _vm(conn, _two_level_tree(), "snapshot-3")
    conn.apply_writes = False
    out = await _delete(conn, snapshot_id="snapshot-2")
    assert out["status"] == "still_present"


async def test_delete_returns_a_parked_gate_as_is() -> None:
    conn = SnapshotVimFake()
    _vm(conn, _two_level_tree(), "snapshot-3")
    verdict = parked(_REMOVE_SUB_OP)
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(_write, "enforce_subop_policy", GateRecorder(verdict))
        out = await _delete(conn, snapshot_id="snapshot-2")
    assert out is verdict
    assert conn.calls_to("RemoveSnapshot_Task") == []


async def test_delete_preview_shows_the_snapshot_and_what_goes_with_it() -> None:
    conn = SnapshotVimFake()
    _vm(conn, _two_level_tree(), "snapshot-3")
    effect = await _snapshot.vm_snapshot_delete_preview(
        preview_ctx({"vm": _VM, "snapshot_id": "snapshot-1", "remove_children": True}, conn)
    )
    assert effect is not None
    assert blast_radius_missing_reason(effect) is None
    block = effect["blast_radius"]
    assert block["object"]["present"] is True
    assert block["object"]["name"] == "base"
    assert block["object"]["child_count"] == 2
    assert block["children"] == [
        {"kind": "vm_snapshot", "snapshot_id": "snapshot-2", "name": "before-patch"},
        {"kind": "vm_snapshot", "snapshot_id": "snapshot-3", "name": "after-patch"},
    ]
    assert block["irreversibility"] == "permanent"
    # Without remove_children nothing else goes.
    kept = await _snapshot.vm_snapshot_delete_preview(
        preview_ctx({"vm": _VM, "snapshot_id": "snapshot-1"}, conn)
    )
    assert kept is not None
    assert kept["blast_radius"]["children"] == []
    # The preview never writes.
    assert conn.calls_to("RemoveSnapshot_Task") == []


async def test_delete_preview_of_an_absent_snapshot_still_parks() -> None:
    conn = SnapshotVimFake()
    _vm(conn, _two_level_tree(), "snapshot-3")
    effect = await _snapshot.vm_snapshot_delete_preview(
        preview_ctx({"vm": _VM, "snapshot_id": "snapshot-9"}, conn)
    )
    assert effect is not None
    assert blast_radius_missing_reason(effect) is None
    assert effect["blast_radius"]["object"]["present"] is False
    assert effect["blast_radius"]["irreversibility"] == "none-object-already-absent"


async def test_delete_preview_declines_without_connector_or_with_bad_params() -> None:
    assert (
        await _snapshot.vm_snapshot_delete_preview(
            preview_ctx({"vm": _VM, "snapshot_id": "snapshot-1"}, None)
        )
        is None
    )
    assert (
        await _snapshot.vm_snapshot_delete_preview(preview_ctx({"vm": _VM}, SnapshotVimFake()))
        is None
    )


# ===========================================================================
# vm.snapshot.revert -- the snapshot id input
# ===========================================================================


async def _revert(conn: SnapshotVimFake, **params: Any) -> Any:
    return await vm_snapshot_revert_composite(
        operator=operator(), target=object(), params={"vm": _VM, **params}, connector=conn
    )


async def test_revert_by_id(gate: GateRecorder) -> None:
    conn = SnapshotVimFake()
    _vm(conn, _two_level_tree(), "snapshot-3")
    out = await _revert(conn, snapshot_id="snapshot-1")

    assert out["status"] == "reverted"
    assert out["snapshot_id"] == "snapshot-1"
    assert [c["op_id"] for c in gate.calls] == [_REVERT_SUB_OP]
    # The approval record names the snapshot by its name too.
    assert gate.calls[0]["params"] == {"vm": _VM, "snapshot_name": "base", "snapshot": "snapshot-1"}
    [(path, body)] = conn.calls_to("RevertToSnapshot_Task")
    assert path == "/VirtualMachineSnapshot/snapshot-1/RevertToSnapshot_Task"
    assert body == {}


async def test_revert_by_id_picks_one_of_two_snapshots_with_the_same_name(
    gate: GateRecorder,
) -> None:
    conn = SnapshotVimFake()
    _vm(conn, [tree_node("snapshot-1", "x", [tree_node("snapshot-2", "x")])], "snapshot-2")
    by_name = await _revert(conn, snapshot_name="x")
    assert by_name["status"] == "ambiguous"
    by_id = await _revert(conn, snapshot_id="snapshot-1")
    assert by_id["status"] == "reverted"
    assert [p for p, _ in conn.calls_to("RevertToSnapshot_Task")] == [
        "/VirtualMachineSnapshot/snapshot-1/RevertToSnapshot_Task"
    ]


async def test_revert_by_an_id_of_another_vm_is_not_found(gate: GateRecorder) -> None:
    conn = SnapshotVimFake()
    _vm(conn, _two_level_tree(), "snapshot-3")
    out = await _revert(conn, snapshot_id="snapshot-77")
    assert out["status"] == "not_found"
    assert "snapshot-77" in out["guidance"]
    assert gate.calls == []
    assert conn.calls_to("RevertToSnapshot_Task") == []


def test_revert_schema_takes_a_name_or_an_id_not_both() -> None:
    schema = VM_SNAPSHOT_REVERT_PARAMETER_SCHEMA
    assert validate_params(schema, {"vm": _VM, "snapshot_name": "x"}) == []
    assert validate_params(schema, {"vm": _VM, "snapshot_id": "snapshot-1"}) == []
    assert validate_params(schema, {"vm": _VM}) != []
    assert validate_params(schema, {"vm": _VM, "snapshot_name": "x", "snapshot_id": "snapshot-1"})
    assert validate_params(schema, {"vm": _VM, "snapshot_id": "x"}) != []


def test_create_and_delete_schemas_take_moids_only() -> None:
    assert (
        validate_params(VM_SNAPSHOT_CREATE_PARAMETER_SCHEMA, {"vm": _VM, "snapshot_name": "x"})
        == []
    )
    assert validate_params(
        VM_SNAPSHOT_CREATE_PARAMETER_SCHEMA, {"vm": "app-vm", "snapshot_name": "x"}
    )
    assert validate_params(VM_SNAPSHOT_CREATE_PARAMETER_SCHEMA, {"vm": _VM}) != []
    assert (
        validate_params(
            VM_SNAPSHOT_DELETE_PARAMETER_SCHEMA, {"vm": _VM, "snapshot_id": "snapshot-2"}
        )
        == []
    )
    assert validate_params(VM_SNAPSHOT_DELETE_PARAMETER_SCHEMA, {"vm": _VM, "snapshot_id": "base"})


# ===========================================================================
# The governed flow through the dispatcher (approval gate)
# ===========================================================================

_CONNECTOR_ID = "vmware-rest-9.0"
_TENANT_ID = UUID("00000000-0000-0000-0000-000000004844")
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
        name="Snapshot Test",
        email=None,
        raw_jwt="<test-raw-jwt>",
        tenant_id=_TENANT_ID,
        tenant_role=TenantRole.OPERATOR,
        principal_kind=kind,
    )


async def _bootstrap(conn: SnapshotVimFake) -> None:
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
                notes="seeded by test_connectors_vmware_rest_composites_snapshot",
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
    await _bootstrap(SnapshotVimFake())
    async with get_sessionmaker()() as s:
        rows = {
            row.op_id: row
            for row in (
                await s.execute(
                    select(EndpointDescriptor).where(
                        EndpointDescriptor.op_id.in_((_CREATE_OP, _DELETE_OP))
                    )
                )
            )
            .scalars()
            .all()
        }
    assert rows[_CREATE_OP].safety_level == "caution"
    assert rows[_CREATE_OP].requires_approval is True
    assert rows[_DELETE_OP].safety_level == "destructive"
    assert rows[_DELETE_OP].requires_approval is True


@pytest.mark.usefixtures("_dispatch_env")
async def test_create_waits_for_a_second_person_then_runs_once() -> None:
    conn = SnapshotVimFake()
    _vm(conn, None, None)
    await _bootstrap(conn)
    args = {
        "connector_id": _CONNECTOR_ID,
        "op_id": _CREATE_OP,
        "target": _TARGET_NAME,
        "params": {"vm": _VM, "snapshot_name": "before-reset"},
    }
    call = await call_operation(_person("op-requester"), args)
    assert call["status"] == "awaiting_approval", call
    assert conn.calls_to("CreateSnapshotEx_Task") == []

    resume = await _approve_and_resume(UUID(call["extras"]["approval_request_id"]))
    assert resume.status == "ok", resume.error
    assert resume.result["status"] == "created"
    assert len(conn.calls_to("CreateSnapshotEx_Task")) == 1
    assert _names(conn) == ["before-reset"]


@pytest.mark.usefixtures("_dispatch_env")
async def test_an_agent_create_waits_for_approval() -> None:
    conn = SnapshotVimFake()
    _vm(conn, None, None)
    await _bootstrap(conn)
    result = await dispatch(
        operator=_person("agent-1", PrincipalKind.AGENT),
        connector_id=_CONNECTOR_ID,
        op_id=_CREATE_OP,
        target=_FakeTarget(),
        params={"vm": _VM, "snapshot_name": "before-reset"},
    )
    assert result.status == "awaiting_approval", result
    assert conn.calls_to("CreateSnapshotEx_Task") == []


@pytest.mark.usefixtures("_dispatch_env")
async def test_delete_needs_a_preview_then_a_second_person() -> None:
    conn = SnapshotVimFake()
    _vm(conn, _two_level_tree(), "snapshot-3")
    await _bootstrap(conn)
    requester = _person("op-requester")
    args = {
        "connector_id": _CONNECTOR_ID,
        "op_id": _DELETE_OP,
        "target": _TARGET_NAME,
        "params": {"vm": _VM, "snapshot_id": "snapshot-2"},
    }
    # Without a preview hash the call is refused and nothing waits.
    refused = await call_operation(requester, args)
    assert refused["status"] == "denied", refused
    assert refused["extras"]["error_code"] == "preview_binding_required"
    assert await _approval_count() == 0

    preview = await preview_operation(requester, args)
    assert preview["status"] == "ok", preview
    call = await call_operation(requester, {**args, "preview_hash": preview["preview_hash"]})
    assert call["status"] == "awaiting_approval", call
    assert conn.calls_to("RemoveSnapshot_Task") == []
    request_id = UUID(call["extras"]["approval_request_id"])

    # The approval card shows the snapshot.
    async with get_sessionmaker()() as s:
        row = await s.get(ApprovalRequest, request_id)
        assert row is not None
        effect = dict(row.proposed_effect)
    assert effect["safety_level"] == "destructive"
    assert effect["blast_radius"]["object"]["name"] == "before-patch"
    assert effect["blast_radius"]["object"]["vm_name"] == "app-vm"
    assert effect["blast_radius"]["irreversibility"] == "permanent"

    resume = await _approve_and_resume(request_id)
    assert resume.status == "ok", resume.error
    assert resume.result["status"] == "deleted"
    assert [p for p, _ in conn.calls_to("RemoveSnapshot_Task")] == [
        "/VirtualMachineSnapshot/snapshot-2/RemoveSnapshot_Task"
    ]
    assert _names(conn) == ["base", "after-patch"]


@pytest.mark.usefixtures("_dispatch_env")
async def test_an_agent_delete_is_refused() -> None:
    conn = SnapshotVimFake()
    _vm(conn, _two_level_tree(), "snapshot-3")
    await _bootstrap(conn)
    result = await dispatch(
        operator=_person("agent-1", PrincipalKind.AGENT),
        connector_id=_CONNECTOR_ID,
        op_id=_DELETE_OP,
        target=_FakeTarget(),
        params={"vm": _VM, "snapshot_id": "snapshot-2"},
    )
    assert result.status == "denied", result
    assert "destructive" in (result.error or "")
    assert conn.calls_to("RemoveSnapshot_Task") == []
    assert await _approval_count() == 0
