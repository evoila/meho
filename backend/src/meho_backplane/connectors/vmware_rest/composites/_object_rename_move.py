# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Rename one inventory object, or move objects into a folder (VIM API).

* ``vmware.composite.object.rename`` (``caution``) -- vim
  ``ManagedEntity.Rename_Task`` on a VM, folder, datacenter, cluster,
  datastore, datastore cluster, network, distributed switch, distributed port
  group or resource pool. MEHO reads the name first: when it already is the new
  name, nothing is sent (``unchanged``). After the task MEHO reads the name
  again.
* ``vmware.composite.object.move`` (``dangerous``) -- vim
  ``Folder.MoveIntoFolder_Task``. Moves up to 50 objects into one folder. The
  folder's permissions then apply to the moved objects, which is why the move
  is ``dangerous``. MEHO reads the folder and every object first: it refuses an
  unknown folder or object, and an object the folder cannot hold (a VM into a
  host folder, for example). Objects already in the folder are skipped. After
  the task MEHO reads each object's parent again, also after a failed task:
  vCenter moves the objects one by one and stops at the first fault, so the
  answer names the objects that moved before it.

Each object is named by its kind and its moid, never by name: after approval
the change must run on the object the approver saw. Both wait for approval and
run the same way on vCenter 8.0 U1 and newer and on vCenter 9.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any, Final, NamedTuple

import httpx

from meho_backplane.connectors import OperationResult
from meho_backplane.connectors.vmware_rest.composite_issues import issue
from meho_backplane.connectors.vmware_rest.composites._teardown_reads import read_object
from meho_backplane.connectors.vmware_rest.composites._write import (
    _OP_RETRIEVE_PROPERTIES,
    _moref_value,
    _task_timeout_issue,
    _unwrap_value,
    _vsphere_fault_detail,
    _write_vmomi_sub_op,
)
from meho_backplane.connectors.vmware_rest.vim_body import vim_moref
from meho_backplane.connectors.vmware_rest.vim_task import TASK_STATE_ERROR, poll_vim_task
from meho_backplane.operations._preview import preview_failure_reason

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors.vmware_rest.connector import VmwareRestConnector
    from meho_backplane.connectors.vmware_rest.vim_task import VimTaskResult
    from meho_backplane.operations._preview import PreviewContext

__all__ = [
    "MOVE_FOLDER_MOID_PATTERN",
    "MOVE_KINDS",
    "MOVE_MAX_OBJECTS",
    "OBJECT_KINDS",
    "OP_ID_OBJECT_MOVE",
    "OP_ID_OBJECT_RENAME",
    "RENAME_KINDS",
    "object_move_composite",
    "object_rename_composite",
]

OP_ID_OBJECT_RENAME: Final = "vmware.composite.object.rename"
OP_ID_OBJECT_MOVE: Final = "vmware.composite.object.move"


class ObjectKind(NamedTuple):
    """One kind of inventory object: its vim type, moid shape and folder child type."""

    mo_type: str
    moid_pattern: str
    #: The ``Folder.childType`` entry a folder needs to hold this kind, without
    #: the ``vim.`` prefix. A datastore cluster is a folder in the vim model, so
    #: it needs ``Folder``; a distributed switch and a distributed port group go
    #: into a network folder, whose ``childType`` names ``Network``.
    folder_child_type: str


#: Every kind the two changes take, by the name the agent passes.
OBJECT_KINDS: Final[dict[str, ObjectKind]] = {
    "vm": ObjectKind("VirtualMachine", r"^vm-[0-9]+$", "VirtualMachine"),
    "folder": ObjectKind("Folder", r"^group-[vhsnd][0-9]+$", "Folder"),
    "datacenter": ObjectKind("Datacenter", r"^datacenter-[0-9]+$", "Datacenter"),
    "cluster": ObjectKind("ClusterComputeResource", r"^domain-c[0-9]+$", "ComputeResource"),
    "standalone_host": ObjectKind("ComputeResource", r"^domain-s[0-9]+$", "ComputeResource"),
    "datastore": ObjectKind("Datastore", r"^datastore-[0-9]+$", "Datastore"),
    "datastore_cluster": ObjectKind("StoragePod", r"^group-p[0-9]+$", "Folder"),
    "network": ObjectKind("Network", r"^network-[0-9]+$", "Network"),
    "distributed_switch": ObjectKind("DistributedVirtualSwitch", r"^dvs-[0-9]+$", "Network"),
    "distributed_portgroup": ObjectKind(
        "DistributedVirtualPortgroup", r"^dvportgroup-[0-9]+$", "Network"
    ),
    "resource_pool": ObjectKind("ResourcePool", r"^resgroup-[0-9]+$", "ResourcePool"),
}

#: The ``Rename_Task`` call of each kind: one vim path per managed-object type.
#: Written out in full, so the reconcile lanes find each path in the pinned
#: vi-json files of vCenter 8.0 U1 to U3 and 9.0. A standalone host takes its
#: name from the host itself, so it has no rename.
_RENAME_OPS: Final[dict[str, str]] = {
    "vm": "POST:/VirtualMachine/{moId}/Rename_Task",
    "folder": "POST:/Folder/{moId}/Rename_Task",
    "datacenter": "POST:/Datacenter/{moId}/Rename_Task",
    "cluster": "POST:/ClusterComputeResource/{moId}/Rename_Task",
    "datastore": "POST:/Datastore/{moId}/Rename_Task",
    "datastore_cluster": "POST:/StoragePod/{moId}/Rename_Task",
    "network": "POST:/Network/{moId}/Rename_Task",
    "distributed_switch": "POST:/DistributedVirtualSwitch/{moId}/Rename_Task",
    "distributed_portgroup": "POST:/DistributedVirtualPortgroup/{moId}/Rename_Task",
    "resource_pool": "POST:/ResourcePool/{moId}/Rename_Task",
}
RENAME_KINDS: Final[tuple[str, ...]] = tuple(_RENAME_OPS)
#: A resource pool moves with ``MoveIntoResourcePool``, so ``MoveIntoFolder``
#: does not take it. A distributed port group moves between network folders
#: and stays on its switch.
MOVE_KINDS: Final[tuple[str, ...]] = tuple(k for k in OBJECT_KINDS if k != "resource_pool")

_OP_MOVE_INTO_FOLDER_TASK: Final = "POST:/Folder/{moId}/MoveIntoFolder_Task"

_VIM_SUB_OPS_OBJECT_RENAME: Final[tuple[str, ...]] = (
    _OP_RETRIEVE_PROPERTIES,
    *_RENAME_OPS.values(),
)
_VIM_SUB_OPS_OBJECT_MOVE: Final[tuple[str, ...]] = (
    _OP_RETRIEVE_PROPERTIES,
    _OP_MOVE_INTO_FOLDER_TASK,
)

#: Folders an object can move into: VM, host, datastore, network or datacenter folders.
MOVE_FOLDER_MOID_PATTERN: Final = OBJECT_KINDS["folder"].moid_pattern

#: The letter after ``group-`` of the folders that hold a kind whose
#: ``childType`` entry is ``Folder``, which does not tell the folder's kind.
_FOLDER_LETTER_OF_KIND: Final[dict[str, str]] = {"datastore_cluster": "s"}

#: Objects per move; the parameter schema refuses more.
MOVE_MAX_OBJECTS: Final = 50

#: Wall-clock bound for the task polls; module-global so tests can zero it.
_TASK_TIMEOUT_SECONDS = 600.0


def _bad_reference(kind: Any, moid: Any, allowed: tuple[str, ...]) -> str | None:
    """Why ``(kind, moid)`` is not a usable reference, or ``None``."""
    if kind not in allowed:
        return f"object kind {kind!r} is not one of {', '.join(allowed)}"
    if not isinstance(moid, str) or not re.fullmatch(OBJECT_KINDS[kind].moid_pattern, moid):
        return f"{moid!r} is not a {kind} moid (expected {OBJECT_KINDS[kind].moid_pattern})"
    return None


def _failed(envelope: dict[str, Any], status: str, message: str, guidance: str) -> dict[str, Any]:
    """A refusal before any write, with an ``error`` issue so the call counts as failed."""
    return {
        **envelope,
        "status": status,
        "issues": [issue("input", "error", message)],
        "guidance": guidance,
    }


async def _run_task(
    connector: VmwareRestConnector,
    target: Any,
    operator: Operator,
    *,
    op_id: str,
    vmomi_path: str,
    body: dict[str, Any],
    params: dict[str, Any],
    label: str,
) -> OperationResult | VimTaskResult:
    """Gate + send one ``*_Task`` and poll it to its end.

    A fault can come at once (vCenter refuses the call, HTTP 500 with the vim
    fault): that raises, so the call is recorded as failed and the answer
    carries the fault. A fault at the end of the task comes back in the
    outcome (``state == "error"``): the caller decides what to read back
    before it reports the fault.
    """
    try:
        gate, payload = await _write_vmomi_sub_op(
            connector,
            target,
            operator,
            op_id=op_id,
            vmomi_path=vmomi_path,
            body=body,
            params=params,
        )
    except httpx.HTTPStatusError as exc:
        raise RuntimeError(
            f"{label}: vCenter refused the call: {_vsphere_fault_detail(exc)}"
        ) from exc
    if gate is not None:
        return gate
    outcome = await poll_vim_task(
        connector,
        target,
        operator,
        task=_unwrap_value(payload),
        timeout_seconds=_TASK_TIMEOUT_SECONDS,
    )
    return outcome


def _task_fault(outcome: VimTaskResult, label: str) -> str:
    """The fault of a task that ended in error, as one line."""
    return (
        f"{label}: vCenter task {outcome.task} failed "
        f"({outcome.fault_type or 'fault'}): {outcome.error_message or '<no message>'}"
    )


# ===========================================================================
# object.rename -- ManagedEntity.Rename_Task
# ===========================================================================


async def object_rename_composite(
    *,
    operator: Operator,
    target: Any,
    params: dict[str, Any],
    connector: VmwareRestConnector,
) -> dict[str, Any] | OperationResult:
    """Rename one inventory object. Op-id: ``vmware.composite.object.rename``."""
    kind, moid, new_name = params["object_type"], params["moid"], params["new_name"]
    envelope: dict[str, Any] = {
        "object_type": kind,
        "moid": moid,
        "old_name": None,
        "new_name": new_name,
        "name_after": None,
        "task": None,
        "issues": [],
        "guidance": None,
    }
    problem = _bad_reference(kind, moid, RENAME_KINDS)
    if problem is not None:
        return _failed(envelope, "invalid_request", problem, "Fix the kind or the moid.")
    mo_type = OBJECT_KINDS[kind].mo_type
    before = await read_object(
        connector, target, operator, mo_type=mo_type, moid=moid, props=["name"]
    )
    if before is None:
        return _failed(
            envelope,
            "not_found",
            f"{kind} {moid} does not exist",
            "Nothing was renamed. Check the kind and the moid.",
        )
    envelope["old_name"] = envelope["name_after"] = before.get("name")
    if before.get("name") == new_name:
        return {**envelope, "status": "unchanged", "guidance": "It has that name; nothing sent."}
    outcome = await _run_task(
        connector,
        target,
        operator,
        op_id=_RENAME_OPS[kind],
        vmomi_path=f"/{mo_type}/{moid}/Rename_Task",
        body={"newName": new_name},
        params={"object_type": kind, "moid": moid, "new_name": new_name},
        label="object.rename",
    )
    if isinstance(outcome, OperationResult):
        return outcome
    if outcome.state == TASK_STATE_ERROR:
        raise RuntimeError(_task_fault(outcome, "object.rename"))
    envelope["task"] = outcome.task
    if outcome.timed_out:
        return {
            **envelope,
            "status": "timeout",
            "issues": [_task_timeout_issue("Rename_Task", outcome.task)],
            "guidance": "The rename may still finish. Read the name again before a retry.",
        }
    after = await read_object(
        connector, target, operator, mo_type=mo_type, moid=moid, props=["name"]
    )
    envelope["name_after"] = None if after is None else after.get("name")
    return {**envelope, "status": "renamed"}


async def _object_rename_preview(ctx: PreviewContext) -> dict[str, Any] | None:
    """Show the approver the object and its current name."""
    kind, moid, new_name = (ctx.params.get(k) for k in ("object_type", "moid", "new_name"))
    if ctx.connector_instance is None or _bad_reference(kind, moid, RENAME_KINDS) is not None:
        return None
    connector: VmwareRestConnector = ctx.connector_instance  # type: ignore[assignment]
    effect: dict[str, Any] = {"object_type": kind, "moid": moid, "new_name": new_name}
    try:
        props = await read_object(
            connector,
            ctx.target,
            ctx.operator,
            mo_type=OBJECT_KINDS[str(kind)].mo_type,
            moid=str(moid),
            props=["name"],
        )
    except Exception as exc:
        reason = preview_failure_reason(exc)
        return {**effect, "summary": "MEHO could not read the object.", "read_error": reason}
    if props is None:
        return {**effect, "summary": f"{kind} {moid} does not exist; the call will fail."}
    current = props.get("name")
    return {
        **effect,
        "current_name": current,
        "summary": f"Rename {kind} {moid} from {current!r} to {new_name!r}.",
    }


# ===========================================================================
# object.move -- Folder.MoveIntoFolder_Task
# ===========================================================================


async def _read_objects(
    connector: VmwareRestConnector,
    target: Any,
    operator: Operator,
    objects: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """``[{object_type, moid, name, present, parent}]``, one read per object.

    One read each, not one for all: vCenter fails a property read as a whole
    when one object in it does not exist, and MEHO must name the missing one.
    A read vCenter answers only in part raises (the call fails before any
    write).
    """
    rows = []
    for obj in objects:
        props = await read_object(
            connector,
            target,
            operator,
            mo_type=OBJECT_KINDS[obj["object_type"]].mo_type,
            moid=obj["moid"],
            props=["name", "parent"],
        )
        rows.append(
            {
                "object_type": obj["object_type"],
                "moid": obj["moid"],
                "name": None if props is None else props.get("name"),
                "present": props is not None,
                "parent": None if props is None else _moref_value(props.get("parent")),
            }
        )
    return rows


def _child_type_name(raw: str) -> str:
    """``vim.Datastore`` and ``Datastore`` are the same ``childType`` entry."""
    return raw.removeprefix("vim.")


def _fits(row: dict[str, Any], folder: str, child_types: list[str]) -> bool:
    """Whether the folder can hold the object.

    The folder's ``childType`` must name the object's kind. It says only
    ``Folder`` for a folder and for a datastore cluster (a folder in the vim
    model), so for those the letter after ``group-`` decides: a folder moves
    into a folder of its own kind (a VM folder into a VM folder), a datastore
    cluster into a datastore folder.
    """
    kind = row["object_type"]
    if OBJECT_KINDS[kind].folder_child_type not in child_types:
        return False
    if kind == "folder":
        return str(row["moid"])[:7] == folder[:7]
    letter = _FOLDER_LETTER_OF_KIND.get(kind)
    return letter is None or folder[:7] == f"group-{letter}"


class _MovePlan(NamedTuple):
    """What the move would do, or why it must not run (``refusal``)."""

    envelope: dict[str, Any]
    refusal: dict[str, Any] | None
    to_move: list[dict[str, Any]]


async def _plan_move(
    connector: VmwareRestConnector, target: Any, operator: Operator, params: dict[str, Any]
) -> _MovePlan:
    """Read the folder and the objects; decide what to move (no write)."""
    folder, objects = params["folder"], params["objects"]
    envelope: dict[str, Any] = {
        "folder": folder,
        "folder_name": None,
        "objects": [],
        "task": None,
        "issues": [],
        "guidance": None,
    }
    checks = [_bad_reference(o.get("object_type"), o.get("moid"), MOVE_KINDS) for o in objects]
    if not (isinstance(folder, str) and re.fullmatch(MOVE_FOLDER_MOID_PATTERN, folder)):
        checks.insert(0, f"{folder!r} is not a folder moid")
    problems = [p for p in checks if p is not None]
    if problems:
        refusal = _failed(envelope, "invalid_request", "; ".join(problems), "Fix the input.")
        return _MovePlan(envelope, refusal, [])
    props = await read_object(
        connector, target, operator, mo_type="Folder", moid=folder, props=["name", "childType"]
    )
    if props is None:
        refusal = _failed(
            envelope, "not_found", f"folder {folder} does not exist", "Nothing was moved."
        )
        return _MovePlan(envelope, refusal, [])
    envelope["folder_name"] = props.get("name")
    raw_types = props.get("childType")
    if not isinstance(raw_types, list):
        raw_types = []
    child_types = [_child_type_name(t) for t in raw_types if isinstance(t, str)]
    rows = await _read_objects(connector, target, operator, objects)
    envelope["objects"] = rows
    missing = [r["moid"] for r in rows if not r["present"]]
    wrong = [r["moid"] for r in rows if r["present"] and not _fits(r, folder, child_types)]
    message = None
    if missing:
        message = f"objects not found: {', '.join(missing)}"
    elif wrong:
        holds = ", ".join(child_types) or "nothing"
        message = f"folder {folder} cannot hold: {', '.join(wrong)} (it holds {holds})"
    if message is not None:
        refusal = _failed(envelope, "precondition_failed", message, "Nothing was moved.")
        return _MovePlan(envelope, refusal, [])
    return _MovePlan(envelope, None, [r for r in rows if r["parent"] != folder])


async def object_move_composite(
    *,
    operator: Operator,
    target: Any,
    params: dict[str, Any],
    connector: VmwareRestConnector,
) -> dict[str, Any] | OperationResult:
    """Move objects into a folder. Op-id: ``vmware.composite.object.move``."""
    plan = await _plan_move(connector, target, operator, params)
    if plan.refusal is not None:
        return plan.refusal
    envelope, folder = plan.envelope, params["folder"]
    if not plan.to_move:
        return {**envelope, "status": "unchanged", "guidance": "All are in the folder already."}
    refs = [vim_moref(OBJECT_KINDS[r["object_type"]].mo_type, r["moid"]) for r in plan.to_move]
    outcome = await _run_task(
        connector,
        target,
        operator,
        op_id=_OP_MOVE_INTO_FOLDER_TASK,
        vmomi_path=f"/Folder/{folder}/MoveIntoFolder_Task",
        body={"list": refs},
        params={"folder": folder, "objects": [r["moid"] for r in plan.to_move]},
        label="object.move",
    )
    if isinstance(outcome, OperationResult):
        return outcome
    envelope["task"] = outcome.task
    if outcome.timed_out:
        return {
            **envelope,
            "status": "timeout",
            "issues": [_task_timeout_issue("MoveIntoFolder_Task", outcome.task)],
            "guidance": "The move may still finish. Read the objects again before a retry.",
        }
    rows = await _read_objects(connector, target, operator, params["objects"])
    before = {r["moid"]: r["parent"] for r in envelope["objects"]}
    envelope["objects"] = [
        {**r, "parent_before": before.get(r["moid"]), "moved": r["parent"] == folder} for r in rows
    ]
    if outcome.state == TASK_STATE_ERROR:
        return _move_failed(envelope, outcome)
    if all(r["moved"] for r in envelope["objects"]):
        return {**envelope, "status": "moved"}
    return {
        **envelope,
        "status": "partial",
        "issues": [issue("connector", "warning", "some objects did not read back in the folder")],
        "guidance": "Check the rows with moved=false.",
    }


def _move_failed(envelope: dict[str, Any], outcome: VimTaskResult) -> dict[str, Any]:
    """The answer of a move whose task failed, with the objects as read back.

    vCenter moves the objects one by one and stops at the first fault, so the
    objects before it are in the folder. The rows say which.
    """
    folder = envelope["folder"]
    moved = [r["moid"] for r in envelope["objects"] if r["moved"] and r["parent_before"] != folder]
    return {
        **envelope,
        "status": "failed",
        "issues": [issue("connector", "error", _task_fault(outcome, "object.move"))],
        "guidance": (
            f"vCenter moved {', '.join(moved)} before the fault; the others stayed where they were."
            if moved
            else "Nothing moved."
        ),
    }


async def _object_move_preview(ctx: PreviewContext) -> dict[str, Any] | None:
    """Show the approver the folder, each object and where it is now."""
    if ctx.connector_instance is None or not isinstance(ctx.params.get("objects"), list):
        return None
    connector: VmwareRestConnector = ctx.connector_instance  # type: ignore[assignment]
    try:
        plan = await _plan_move(connector, ctx.target, ctx.operator, ctx.params)
    except Exception as exc:
        reason = preview_failure_reason(exc)
        return {"summary": "MEHO could not read the objects.", "read_error": reason}
    if plan.refusal is not None:
        return {"summary": "The call will be refused.", **plan.refusal}
    envelope = plan.envelope
    return {
        "summary": (
            f"Move {len(plan.to_move)} object(s) into folder {envelope['folder_name']!r} "
            f"({envelope['folder']}). The folder's permissions then apply to them."
        ),
        "folder": envelope["folder"],
        "folder_name": envelope["folder_name"],
        "objects": envelope["objects"],
    }
