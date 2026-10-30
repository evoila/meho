# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The read-only plan of ``vmware.composite.folder.teardown`` (#4624).

One planning function (:func:`plan_folder_teardown`) feeds both the park-time
blast radius (:func:`folder_teardown_preview`) and the handler in
:mod:`._folder_teardown`, which plans again on the live state after approval.
So the approver reads exactly the refusal the handler applies.

The plan reads the folder, walks every sub-folder level by level, and reads
every VM and template under it (power state, template flag, ``managedBy``,
disks, NICs, snapshots). It refuses on a root or datacenter top folder, a
folder that cannot hold VMs, a vApp or another object type, a managed VM, a VM
that is not in ``expected_vms``, and an answer vCenter did not fully give.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final

from meho_backplane.connectors.vmware_rest.composites._teardown import (
    _DATACENTER_MO_TYPE,
    STATUS_INVALID_REQUEST,
    STATUS_PRECONDITION_FAILED,
    TeardownPlan,
    blast_radius,
    capped,
)
from meho_backplane.connectors.vmware_rest.composites._teardown_reads import (
    moid_problem,
    moref_values,
    read_many,
    read_names,
    read_object,
)
from meho_backplane.connectors.vmware_rest.composites._write import (
    _FOLDER_MO_TYPE,
    _VIRTUAL_MACHINE_MO_TYPE,
    _flatten_snapshot_tree,
    _moref_value,
)
from meho_backplane.connectors.vmware_rest.composites.schemas import (
    VM_FOLDER_MOID_PATTERN,
    VM_MOID_PATTERN,
)
from meho_backplane.connectors.vmware_rest.vim_body import unwrap_vim_value

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors.vmware_rest.connector import VmwareRestConnector
    from meho_backplane.operations._preview import PreviewContext

__all__ = [
    "POWERED_OFF",
    "folder_teardown_preview",
    "plan_folder_teardown",
    "teardown_blast_radius",
]

POWERED_OFF: Final = "poweredOff"

_FOLDER_PROPS: Final = ["name", "childEntity", "childType", "parent"]
_SUBFOLDER_PROPS: Final = ["name", "childEntity"]
_VM_PROPS: Final = [
    "name",
    "runtime.powerState",
    "config.template",
    "config.managedBy",
    "config.hardware.device",
    "snapshot",
]


def _vm_row(moid: str, folder: str, values: dict[str, Any]) -> dict[str, Any]:
    """One VM or template of the plan, with its disks, NICs and snapshots."""
    devices = values.get("config.hardware.device")
    devices = devices if isinstance(devices, list) else []
    disks = [
        {
            "file": (d.get("backing") or {}).get("fileName"),
            "capacity_bytes": d.get("capacityInBytes"),
        }
        for d in devices
        if isinstance(d, dict) and d.get("_typeName") == "VirtualDisk"
    ]
    nics = [
        {"label": (d.get("deviceInfo") or {}).get("label"), "mac": d.get("macAddress")}
        for d in devices
        if isinstance(d, dict) and "macAddress" in d
    ]
    snapshot = values.get("snapshot")
    tree = snapshot.get("rootSnapshotList") if isinstance(snapshot, dict) else None
    managed_by = values.get("config.managedBy")
    return {
        "kind": "template" if values.get("config.template") is True else "vm",
        "moid": moid,
        "name": values.get("name"),
        "folder": folder,
        "power_state": values.get("runtime.powerState"),
        "managed_by": managed_by.get("extensionKey") if isinstance(managed_by, dict) else None,
        "disks": disks,
        "nics": nics,
        "snapshots": len(_flatten_snapshot_tree(tree)),
    }


async def _walk(
    connector: VmwareRestConnector,
    target: Any,
    operator: Operator,
    root: str,
    children: list[tuple[str, str]],
) -> tuple[dict[str, str], list[dict[str, Any]], list[dict[str, Any]], bool]:
    """Every object under *root*: ``(vm -> folder, sub-folders, other objects, complete)``.

    Reads one level of sub-folders per call. Sub-folder rows carry their
    ``depth`` (1 = directly under *root*) so they can be deleted bottom up.
    """
    vms: dict[str, str] = {}
    folders: dict[str, dict[str, Any]] = {}
    others: list[dict[str, Any]] = []
    level = [(root, children)]
    depth = 1
    complete = True
    while level:
        next_ids: list[str] = []
        for parent, kids in level:
            for mo_type, moid in kids:
                if mo_type == _VIRTUAL_MACHINE_MO_TYPE:
                    vms[moid] = parent
                elif mo_type == _FOLDER_MO_TYPE and moid not in folders:
                    folders[moid] = {"kind": "folder", "moid": moid, "parent": parent}
                    folders[moid]["depth"] = depth
                    next_ids.append(moid)
                else:
                    others.append({"kind": mo_type, "moid": moid, "parent": parent})
        if not next_ids:
            break
        found, ok = await read_many(
            connector, target, operator, mo_type=_FOLDER_MO_TYPE, moids=next_ids,
            props=_SUBFOLDER_PROPS,
        )  # fmt: skip
        complete = complete and ok
        for moid, values in found.items():
            folders[moid]["name"] = values.get("name")
        level = [(m, moref_values(found[m].get("childEntity"))) for m in next_ids if m in found]
        depth += 1
    return vms, list(folders.values()), others, complete


async def _blockers(
    connector: VmwareRestConnector,
    target: Any,
    operator: Operator,
    vms: list[dict[str, Any]],
    others: list[dict[str, Any]],
    expected: set[str],
) -> list[dict[str, Any]]:
    """What stops the teardown: vApps / other objects, managed VMs, unexpected VMs."""
    rows: list[dict[str, Any]] = []
    for mo_type in sorted({o["kind"] for o in others}):
        moids = [o["moid"] for o in others if o["kind"] == mo_type]
        names = await read_names(connector, target, operator, mo_type=mo_type, moids=moids)
        reason = (
            "a vApp; delete the vApp first"
            if mo_type == "VirtualApp"
            else "an object type this op does not delete"
        )
        rows.extend(
            {"kind": mo_type, "moid": m, "name": names.get(m), "reason": reason} for m in moids
        )
    for vm in vms:
        if vm["managed_by"]:
            reason = f"managed by another product ({vm['managed_by']})"
            rows.append(
                {"kind": vm["kind"], "moid": vm["moid"], "name": vm["name"], "reason": reason}
            )
        if vm["moid"] not in expected:
            reason = "not in expected_vms (the folder changed, or the list is incomplete)"
            rows.append(
                {"kind": vm["kind"], "moid": vm["moid"], "name": vm["name"], "reason": reason}
            )
    return rows


def _input_problem(params: dict[str, Any]) -> str | None:
    """Why the folder / expected_vms input is malformed, or ``None``."""
    problem = moid_problem(params.get("folder"), VM_FOLDER_MOID_PATTERN, "folder")
    if problem is not None:
        return problem
    expected = params.get("expected_vms")
    if not isinstance(expected, list):
        return "expected_vms must be a list of VM ids, like ['vm-101']"
    for vm in expected:
        problem = moid_problem(vm, VM_MOID_PATTERN, "expected_vms entry")
        if problem is not None:
            return problem
    return None


async def plan_folder_teardown(
    connector: VmwareRestConnector,
    target: Any,
    operator: Operator,
    params: dict[str, Any],
) -> TeardownPlan:
    """Plan the teardown of one VM folder (read-only).

    ``plan.context`` carries ``vms`` (one row per VM / template), ``folders``
    (the sub-folders, deepest first) and ``missing`` (expected VMs not under
    the folder). ``plan.children`` is the capped list the approver reads.
    """
    folder = params.get("folder")
    obj: dict[str, Any] = {"kind": "folder", "moid": folder}
    empty: dict[str, Any] = {"vms": [], "folders": [], "missing": []}
    problem = _input_problem(params)
    if problem is not None:
        return TeardownPlan(obj, False, refusal=(STATUS_INVALID_REQUEST, problem), context=empty)
    folder = str(folder)
    expected = set(params["expected_vms"])
    props = await read_object(
        connector, target, operator, mo_type=_FOLDER_MO_TYPE, moid=folder, props=_FOLDER_PROPS
    )
    if props is None:
        return TeardownPlan(obj, False, context={**empty, "missing": sorted(expected)})
    parent = unwrap_vim_value(props.get("parent"))
    obj.update(name=props.get("name"), parent=_moref_value(parent))
    plan = TeardownPlan(obj, True, context=empty)
    parent_type = parent.get("type") if isinstance(parent, dict) else None
    child_type = unwrap_vim_value(props.get("childType"))
    if parent_type in (None, _DATACENTER_MO_TYPE):
        detail = f"folder {folder!r} is a root or datacenter top folder and cannot be torn down"
        plan.refusal = (STATUS_PRECONDITION_FAILED, detail)
        return plan
    if not isinstance(child_type, list) or _VIRTUAL_MACHINE_MO_TYPE not in child_type:
        detail = f"folder {folder!r} is not a VM folder (it cannot hold VMs)"
        plan.refusal = (STATUS_INVALID_REQUEST, detail)
        return plan
    vm_parents, folders, others, complete = await _walk(
        connector, target, operator, folder, moref_values(props.get("childEntity"))
    )
    found, vms_ok = await read_many(
        connector, target, operator, mo_type=_VIRTUAL_MACHINE_MO_TYPE,
        moids=list(vm_parents), props=_VM_PROPS,
    )  # fmt: skip
    vms = [_vm_row(m, vm_parents[m], found[m]) for m in vm_parents if m in found]
    folders.sort(key=lambda f: -int(f["depth"]))
    plan.context = {
        "vms": vms,
        "folders": folders,
        "missing": sorted(expected - set(vm_parents)),
    }
    plan.children = capped([*vms, *folders])
    plan.blockers = capped(await _blockers(connector, target, operator, vms, others, expected))
    if not (complete and vms_ok):
        detail = "vCenter did not fully answer the read of the folder content; nothing was changed"
        plan.refusal = (STATUS_PRECONDITION_FAILED, detail)
    elif plan.blockers:
        detail = (
            f"folder {folder!r} holds objects this op will not delete (see 'blockers'): "
            "delete vApps first, leave managed VMs alone, and list every VM in expected_vms"
        )
        plan.refusal = (STATUS_PRECONDITION_FAILED, detail)
    return plan


def _summary(plan: TeardownPlan, delete_folder: bool) -> str:
    """The plain-words line the approver reads first."""
    vms = plan.context["vms"]
    running = sum(1 for vm in vms if vm["power_state"] != POWERED_OFF)
    templates = sum(1 for vm in vms if vm["kind"] == "template")
    folders = len(plan.context["folders"]) + (1 if delete_folder and plan.present else 0)
    return (
        f"Powers off {running} running VM(s). Deletes {len(vms) - templates} VM(s) with "
        f"their disks, {templates} template(s) and {folders} folder(s). This cannot be undone."
    )


def teardown_blast_radius(plan: TeardownPlan, params: dict[str, Any]) -> dict[str, Any]:
    """The destructive-tier blast radius: the shared block plus counts and a summary."""
    preview = blast_radius(plan)
    vms = plan.context["vms"]
    delete_folder = params.get("delete_folder", True) is not False
    preview["blast_radius"].update(
        summary=_summary(plan, delete_folder),
        power_off_count=sum(1 for vm in vms if vm["power_state"] != POWERED_OFF),
        vm_count=len(vms),
        folder_count=len(plan.context["folders"]),
        delete_folder=delete_folder,
        missing_expected_vms=plan.context["missing"],
    )
    return preview


async def folder_teardown_preview(ctx: PreviewContext) -> dict[str, Any] | None:
    """Blast radius for ``folder.teardown`` (the handler's own plan)."""
    if ctx.connector_instance is None or not isinstance(ctx.params.get("folder"), str):
        return None
    params = dict(ctx.params)
    plan = await plan_folder_teardown(
        ctx.connector_instance,  # type: ignore[arg-type]
        ctx.target,
        ctx.operator,
        params,
    )
    return teardown_blast_radius(plan, params)
