# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for ``vm.snapshot.delete_all`` and ``vm.disks.consolidate``.

Two layers, like the other snapshot write tests:

* the handlers and previews against the shared vim fake, extended here with the
  two tasks: the vim body, the gated write, the task poll, the read after the
  task, every refusal before a write, a task fault, the poll timeout, a parked
  gate, and the approval-card blast radius;
* the governed flow through the real dispatcher: delete_all needs a preview,
  waits for a second person, shows the snapshots on the approval card, and is
  refused for an agent; consolidate waits for approval.

All names and ids are made up.
"""

from __future__ import annotations

import copy
from typing import Any
from uuid import UUID

import pytest
from jsonschema import Draft202012Validator
from sqlalchemy import select

from meho_backplane.auth.operator import Operator, PrincipalKind
from meho_backplane.connectors.vmware_rest.composites import _snapshot_cleanup, _write
from meho_backplane.connectors.vmware_rest.composites.schemas import (
    VM_DISKS_CONSOLIDATE_PARAMETER_SCHEMA,
    VM_DISKS_CONSOLIDATE_RESPONSE_SCHEMA,
    VM_SNAPSHOT_DELETE_ALL_PARAMETER_SCHEMA,
    VM_SNAPSHOT_DELETE_ALL_RESPONSE_SCHEMA,
)
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import ApprovalRequest, EndpointDescriptor
from meho_backplane.operations import dispatch
from meho_backplane.operations._preview import blast_radius_missing_reason
from meho_backplane.operations._validate import validate_params
from meho_backplane.operations.meta_tools import call_operation, preview_operation
from tests._vmware_teardown_fake import GateRecorder, operator, parked, preview_ctx
from tests.test_connectors_vmware_rest_composites_snapshot import (
    _VM,
    SnapshotVimFake,
    _approval_count,
    _approve_and_resume,
    _bootstrap,
    _dispatch_env,  # noqa: F401  (fixture)
    _FakeTarget,
    _person,
    _two_level_tree,
    _vm,
)

_DELETE_ALL_OP = "vmware.composite.vm.snapshot.delete_all"
_CONSOLIDATE_OP = "vmware.composite.vm.disks.consolidate"
_REMOVE_ALL_SUB_OP = "POST:/VirtualMachine/{moId}/RemoveAllSnapshots_Task"
_CONSOLIDATE_SUB_OP = "POST:/VirtualMachine/{moId}/ConsolidateVMDisks_Task"
_CONNECTOR_ID = "vmware-rest-9.0"
_TARGET_NAME = "example-vcenter"


class CleanupVimFake(SnapshotVimFake):
    """The snapshot fake plus the two clean-up tasks."""

    #: What ``runtime.consolidationNeeded`` reads after the consolidate task;
    #: ``None`` drops the property, like a read vCenter did not answer.
    flag_after_consolidate: bool | None = False

    def _after_consolidate(self, props: dict[str, Any]) -> None:
        if self.flag_after_consolidate is None:
            props.pop("runtime.consolidationNeeded", None)
        else:
            props["runtime.consolidationNeeded"] = self.flag_after_consolidate

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
        if method == "RemoveAllSnapshots_Task":
            self.calls.append((path, copy.deepcopy(json)))
            return self._task(
                method, apply=lambda: self.objects[("VirtualMachine", moid)].pop("snapshot", None)
            )
        if method == "ConsolidateVMDisks_Task":
            self.calls.append((path, copy.deepcopy(json)))
            props = self.objects[("VirtualMachine", moid)]
            return self._task(method, apply=lambda: self._after_consolidate(props))
        return await super()._post_vmomi_json(
            target,
            path,
            operator=operator,
            json=json,
            promote_managed_object_not_found=promote_managed_object_not_found,
        )


@pytest.fixture
def gate(monkeypatch: pytest.MonkeyPatch) -> GateRecorder:
    recorder = GateRecorder()
    monkeypatch.setattr(_write, "enforce_subop_policy", recorder)
    return recorder


def _with_tree() -> CleanupVimFake:
    conn = CleanupVimFake()
    _vm(conn, _two_level_tree(), "snapshot-3")
    return conn


def _needing_consolidation(needed: bool = True) -> CleanupVimFake:
    conn = CleanupVimFake()
    conn.add("VirtualMachine", _VM, name="app-vm", **{"runtime.consolidationNeeded": needed})
    return conn


async def _delete_all(conn: CleanupVimFake, vm: str = _VM) -> Any:
    return await _snapshot_cleanup.vm_snapshot_delete_all_composite(
        operator=operator(), target=object(), params={"vm": vm}, connector=conn
    )


async def _consolidate(conn: CleanupVimFake, vm: str = _VM) -> Any:
    return await _snapshot_cleanup.vm_disks_consolidate_composite(
        operator=operator(), target=object(), params={"vm": vm}, connector=conn
    )


# ===========================================================================
# vm.snapshot.delete_all
# ===========================================================================


async def test_delete_all_removes_every_snapshot(gate: GateRecorder) -> None:
    conn = _with_tree()
    out = await _delete_all(conn)

    assert out["status"] == "deleted"
    assert out["object"] == {
        "kind": "vm_snapshots",
        "vm": _VM,
        "vm_name": "app-vm",
        "snapshot_count": 3,
        "snapshots_left": 0,
    }
    Draft202012Validator(VM_SNAPSHOT_DELETE_ALL_RESPONSE_SCHEMA).validate(out)
    assert [c["op_id"] for c in gate.calls] == [_REMOVE_ALL_SUB_OP]
    assert gate.calls[0]["params"] == {"vm": _VM, "snapshot_count": 3}
    [(path, body)] = conn.calls_to("RemoveAllSnapshots_Task")
    assert path == "/VirtualMachine/vm-42/RemoveAllSnapshots_Task"
    assert body == {"consolidate": True}
    assert "snapshot" not in conn.objects[("VirtualMachine", _VM)]


async def test_delete_all_without_snapshots_changes_nothing(gate: GateRecorder) -> None:
    conn = CleanupVimFake()
    _vm(conn, None, None)
    out = await _delete_all(conn)
    assert out["status"] == "unchanged"
    assert gate.calls == []
    assert conn.calls_to("RemoveAllSnapshots_Task") == []


async def test_delete_all_on_an_unknown_vm_is_refused(gate: GateRecorder) -> None:
    out = await _delete_all(CleanupVimFake())
    assert out["status"] == "invalid_request"
    assert gate.calls == []


async def test_delete_all_checks_the_moid_before_any_call(gate: GateRecorder) -> None:
    conn = CleanupVimFake()
    out = await _delete_all(conn, vm="../Folder/group-v1")
    assert out["status"] == "invalid_request"
    assert conn.calls == []


async def test_delete_all_task_fault_raises_with_the_fault_text(gate: GateRecorder) -> None:
    conn = _with_tree()
    conn.task_faults["RemoveAllSnapshots_Task"] = ("SnapshotFault", "The disk is locked.")
    with pytest.raises(RuntimeError, match="The disk is locked"):
        await _delete_all(conn)


async def test_delete_all_timeout(gate: GateRecorder, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_snapshot_cleanup, "_TASK_TIMEOUT_SECONDS", 0.0)
    conn = _with_tree()
    conn.running.add("RemoveAllSnapshots_Task")
    out = await _delete_all(conn)
    assert out["status"] == "timeout"
    assert out["task"] == "task-1"


async def test_delete_all_that_still_reads_back_is_still_present(gate: GateRecorder) -> None:
    conn = _with_tree()
    conn.apply_writes = False
    out = await _delete_all(conn)
    assert out["status"] == "still_present"
    assert out["object"]["snapshots_left"] == 3


async def test_delete_all_returns_a_parked_gate_as_is() -> None:
    conn = _with_tree()
    verdict = parked(_REMOVE_ALL_SUB_OP)
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(_write, "enforce_subop_policy", GateRecorder(verdict))
        out = await _delete_all(conn)
    assert out is verdict
    assert conn.calls_to("RemoveAllSnapshots_Task") == []


async def test_delete_all_preview_lists_every_snapshot() -> None:
    conn = _with_tree()
    effect = await _snapshot_cleanup.vm_snapshot_delete_all_preview(preview_ctx({"vm": _VM}, conn))
    assert effect is not None
    assert blast_radius_missing_reason(effect) is None
    block = effect["blast_radius"]
    assert block["object"]["present"] is True
    assert block["object"]["snapshot_count"] == 3
    assert [c["name"] for c in block["children"]] == ["base", "before-patch", "after-patch"]
    assert block["irreversibility"] == "permanent"
    assert conn.calls_to("RemoveAllSnapshots_Task") == []


async def test_delete_all_preview_of_an_unknown_vm_shows_the_refusal() -> None:
    effect = await _snapshot_cleanup.vm_snapshot_delete_all_preview(
        preview_ctx({"vm": _VM}, CleanupVimFake())
    )
    assert effect is not None
    assert effect["blast_radius"]["object"]["present"] is False
    assert effect["blast_radius"]["refusal"]["status"] == "invalid_request"


async def test_delete_all_preview_declines_without_connector() -> None:
    assert (
        await _snapshot_cleanup.vm_snapshot_delete_all_preview(preview_ctx({"vm": _VM}, None))
        is None
    )


# ===========================================================================
# vm.disks.consolidate
# ===========================================================================


async def test_consolidate_runs_and_reads_back(gate: GateRecorder) -> None:
    conn = _needing_consolidation()
    out = await _consolidate(conn)
    assert out["status"] == "consolidated"
    assert out["consolidation_needed"] is False
    assert out["vm_name"] == "app-vm"
    Draft202012Validator(VM_DISKS_CONSOLIDATE_RESPONSE_SCHEMA).validate(out)
    assert [c["op_id"] for c in gate.calls] == [_CONSOLIDATE_SUB_OP]
    [(path, body)] = conn.calls_to("ConsolidateVMDisks_Task")
    assert path == "/VirtualMachine/vm-42/ConsolidateVMDisks_Task"
    assert body == {}


async def test_consolidate_when_not_needed_changes_nothing(gate: GateRecorder) -> None:
    conn = _needing_consolidation(needed=False)
    out = await _consolidate(conn)
    assert out["status"] == "unchanged"
    assert gate.calls == []
    assert conn.calls_to("ConsolidateVMDisks_Task") == []


async def test_consolidate_without_the_flag_writes_nothing(gate: GateRecorder) -> None:
    conn = CleanupVimFake()
    conn.add("VirtualMachine", _VM, name="app-vm")
    out = await _consolidate(conn)
    assert out["status"] == "unknown"
    assert out["consolidation_needed"] is None
    assert out["vm_name"] == "app-vm"
    assert "nothing was written" in out["guidance"]
    Draft202012Validator(VM_DISKS_CONSOLIDATE_RESPONSE_SCHEMA).validate(out)
    assert gate.calls == []
    assert conn.calls_to("ConsolidateVMDisks_Task") == []


async def test_consolidate_flag_missing_after_the_task_is_unverified(gate: GateRecorder) -> None:
    conn = _needing_consolidation()
    conn.flag_after_consolidate = None
    out = await _consolidate(conn)
    assert out["status"] == "unverified"
    assert out["consolidation_needed"] is None
    assert out["task_state"] == "success"
    assert [i["severity"] for i in out["issues"]] == ["warning"]
    Draft202012Validator(VM_DISKS_CONSOLIDATE_RESPONSE_SCHEMA).validate(out)
    assert [c["op_id"] for c in gate.calls] == [_CONSOLIDATE_SUB_OP]


async def test_consolidate_unknown_vm_and_bad_moid(gate: GateRecorder) -> None:
    assert (await _consolidate(CleanupVimFake()))["status"] == "not_found"
    conn = CleanupVimFake()
    assert (await _consolidate(conn, vm="app-vm"))["status"] == "invalid_request"
    assert conn.calls == []
    assert gate.calls == []


async def test_consolidate_task_fault_raises(gate: GateRecorder) -> None:
    conn = _needing_consolidation()
    conn.task_faults["ConsolidateVMDisks_Task"] = ("NoDiskSpace", "The datastore is full.")
    with pytest.raises(RuntimeError, match="The datastore is full"):
        await _consolidate(conn)


async def test_consolidate_timeout(gate: GateRecorder, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_snapshot_cleanup, "_TASK_TIMEOUT_SECONDS", 0.0)
    conn = _needing_consolidation()
    conn.running.add("ConsolidateVMDisks_Task")
    out = await _consolidate(conn)
    assert out["status"] == "timeout"
    assert out["issues"]


async def test_consolidate_still_needed(gate: GateRecorder) -> None:
    conn = _needing_consolidation()
    conn.apply_writes = False
    assert (await _consolidate(conn))["status"] == "still_needed"


async def test_consolidate_returns_a_parked_gate_as_is() -> None:
    conn = _needing_consolidation()
    verdict = parked(_CONSOLIDATE_SUB_OP)
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(_write, "enforce_subop_policy", GateRecorder(verdict))
        out = await _consolidate(conn)
    assert out is verdict


async def test_consolidate_preview_echoes_the_vm() -> None:
    effect = await _snapshot_cleanup.vm_disks_consolidate_preview(preview_ctx({"vm": _VM}, None))
    assert effect == {"action": "consolidate_vm_disks", "vm": _VM}


def test_schemas_take_one_vm_and_show_a_placeholder_example() -> None:
    """The schema needs ``vm``; the handler checks the moid shape (tests above)."""
    for schema in (VM_SNAPSHOT_DELETE_ALL_PARAMETER_SCHEMA, VM_DISKS_CONSOLIDATE_PARAMETER_SCHEMA):
        assert schema["examples"] == [{"vm": "<vm-id>"}]
        assert validate_params(schema, schema["examples"][0]) == []
        assert validate_params(schema, {"vm": "vm-42"}) == []
        assert validate_params(schema, {"vm": ""}) != []
        assert validate_params(schema, {}) != []
        assert validate_params(schema, {"vm": "vm-42", "force": True}) != []


# ===========================================================================
# The governed flow through the dispatcher
# ===========================================================================


@pytest.mark.usefixtures("_dispatch_env")
async def test_registered_risk_levels() -> None:
    await _bootstrap(CleanupVimFake())
    async with get_sessionmaker()() as s:
        rows = {
            row.op_id: row
            for row in (
                await s.execute(
                    select(EndpointDescriptor).where(
                        EndpointDescriptor.op_id.in_((_DELETE_ALL_OP, _CONSOLIDATE_OP))
                    )
                )
            )
            .scalars()
            .all()
        }
    assert (rows[_DELETE_ALL_OP].safety_level, rows[_DELETE_ALL_OP].requires_approval) == (
        "destructive",
        True,
    )
    assert (rows[_CONSOLIDATE_OP].safety_level, rows[_CONSOLIDATE_OP].requires_approval) == (
        "caution",
        True,
    )


@pytest.mark.usefixtures("_dispatch_env")
async def test_delete_all_needs_a_preview_then_a_second_person() -> None:
    conn = _with_tree()
    await _bootstrap(conn)
    requester = _person("op-requester")
    args = {
        "connector_id": _CONNECTOR_ID,
        "op_id": _DELETE_ALL_OP,
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
    assert conn.calls_to("RemoveAllSnapshots_Task") == []
    request_id = UUID(call["extras"]["approval_request_id"])

    async with get_sessionmaker()() as s:
        row = await s.get(ApprovalRequest, request_id)
        assert row is not None
        effect = dict(row.proposed_effect)
    assert effect["safety_level"] == "destructive"
    assert effect["blast_radius"]["object"]["snapshot_count"] == 3
    assert len(effect["blast_radius"]["children"]) == 3
    assert effect["blast_radius"]["irreversibility"] == "permanent"

    resume = await _approve_and_resume(request_id)
    assert resume.status == "ok", resume.error
    assert resume.result["status"] == "deleted"
    assert len(conn.calls_to("RemoveAllSnapshots_Task")) == 1


@pytest.mark.usefixtures("_dispatch_env")
async def test_an_agent_delete_all_is_refused() -> None:
    conn = _with_tree()
    await _bootstrap(conn)
    result = await dispatch(
        operator=_person("agent-1", PrincipalKind.AGENT),
        connector_id=_CONNECTOR_ID,
        op_id=_DELETE_ALL_OP,
        target=_FakeTarget(),
        params={"vm": _VM},
    )
    assert result.status == "denied", result
    assert conn.calls_to("RemoveAllSnapshots_Task") == []
    assert await _approval_count() == 0


@pytest.mark.usefixtures("_dispatch_env")
async def test_consolidate_waits_for_a_second_person_then_runs_once() -> None:
    conn = _needing_consolidation()
    await _bootstrap(conn)
    call = await call_operation(
        _person("op-requester"),
        {
            "connector_id": _CONNECTOR_ID,
            "op_id": _CONSOLIDATE_OP,
            "target": _TARGET_NAME,
            "params": {"vm": _VM},
        },
    )
    assert call["status"] == "awaiting_approval", call
    assert conn.calls_to("ConsolidateVMDisks_Task") == []

    resume = await _approve_and_resume(UUID(call["extras"]["approval_request_id"]))
    assert resume.status == "ok", resume.error
    assert resume.result["status"] == "consolidated"
    assert len(conn.calls_to("ConsolidateVMDisks_Task")) == 1
