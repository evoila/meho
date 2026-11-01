# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Create and delete a datacenter, through the VIM API.

* ``vmware.composite.datacenter.create`` (``caution``) -- vim
  ``Folder.CreateDatacenter`` on the root folder (or a datacenter folder). The
  call is synchronous: it returns the new datacenter at once, with no task.
  MEHO then reads the datacenter back (name, parent and its four system
  folders).
* ``vmware.composite.datacenter.delete`` (``destructive``) -- vim
  ``Datacenter.Destroy_Task``. ``Destroy_Task`` deletes everything in the
  datacenter, so MEHO refuses a datacenter that still holds anything: a host,
  a cluster, a VM, a datastore, a network or a folder. It uses the shared
  teardown plan (:mod:`._teardown`): the approval card and the run after
  approval read the same plan.

Both wait for approval. The VIM methods run on the VI-JSON seam
(``/sdk/vim25/{release}``) of vCenter 8.0 U1 and newer and of vCenter 9, the
same way on both. :mod:`._object_rename_move` holds the rename and the move.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final

import httpx
import structlog

from meho_backplane.connectors import OperationResult
from meho_backplane.connectors.vmware_rest.alarm_state import read_service_content_refs
from meho_backplane.connectors.vmware_rest.composites._teardown import (
    LIST_CAP,
    STATUS_INVALID_REQUEST,
    STATUS_PRECONDITION_FAILED,
    TeardownPlan,
    blast_radius,
    capped,
    destroy_outcome,
    destroy_task,
    pre_write_outcome,
    timeout_envelope,
)
from meho_backplane.connectors.vmware_rest.composites._teardown_reads import (
    moid_problem,
    moref_values,
    read_many,
    read_names,
    read_object,
)
from meho_backplane.connectors.vmware_rest.composites._write import (
    _OP_CONTINUE_RETRIEVE_PROPERTIES,
    _OP_RETRIEVE_PROPERTIES,
    _moref_value,
    _vsphere_fault_detail,
    _write_vmomi_sub_op,
)
from meho_backplane.connectors.vmware_rest.composites.schemas import DATACENTER_MOID_PATTERN
from meho_backplane.connectors.vmware_rest.ovf_import_control import OP_RETRIEVE_SERVICE_CONTENT

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors.vmware_rest.connector import VmwareRestConnector
    from meho_backplane.operations._preview import PreviewContext

__all__ = [
    "DATACENTER_FOLDER_MOID_PATTERN",
    "OP_ID_DATACENTER_CREATE",
    "OP_ID_DATACENTER_DELETE",
    "datacenter_create_composite",
    "datacenter_delete_composite",
    "plan_datacenter_delete",
]

_log = structlog.get_logger(__name__)

OP_ID_DATACENTER_CREATE: Final = "vmware.composite.datacenter.create"
OP_ID_DATACENTER_DELETE: Final = "vmware.composite.datacenter.delete"

_OP_CREATE_DATACENTER: Final = "POST:/Folder/{moId}/CreateDatacenter"
_OP_DESTROY_DATACENTER_TASK: Final = "POST:/Datacenter/{moId}/Destroy_Task"

#: Every VIM call of each change. The reconcile test checks them against the
#: pinned VI-JSON files of vCenter 8.0 U1 to U3 and 9.0, and
#: ``_governed_subops`` lists them for the grant set.
_VIM_SUB_OPS_DATACENTER_CREATE: Final[tuple[str, ...]] = (
    OP_RETRIEVE_SERVICE_CONTENT,
    _OP_CREATE_DATACENTER,
    _OP_RETRIEVE_PROPERTIES,
)
_VIM_SUB_OPS_DATACENTER_DELETE: Final[tuple[str, ...]] = (
    _OP_RETRIEVE_PROPERTIES,
    _OP_CONTINUE_RETRIEVE_PROPERTIES,
    _OP_DESTROY_DATACENTER_TASK,
)

#: The root folder (``group-d1``) and every datacenter folder below it.
DATACENTER_FOLDER_MOID_PATTERN: Final = r"^group-d[0-9]+$"

_FOLDER_MO_TYPE: Final = "Folder"
_DATACENTER_MO_TYPE: Final = "Datacenter"

#: The four system folders every datacenter has, as ``(property, label)``.
_SYSTEM_FOLDERS: Final[tuple[tuple[str, str], ...]] = (
    ("vmFolder", "vm"),
    ("hostFolder", "host"),
    ("datastoreFolder", "datastore"),
    ("networkFolder", "network"),
)
_DATACENTER_PROPS: Final = ["name", "parent", *(prop for prop, _ in _SYSTEM_FOLDERS)]


def _datacenter_state(datacenter: str, props: dict[str, Any]) -> dict[str, Any]:
    """The datacenter as the answers show it: name, parent and system folders."""
    state: dict[str, Any] = {
        "datacenter": datacenter,
        "name": props.get("name"),
        "parent_folder_id": _moref_value(props.get("parent")),
    }
    for prop, label in _SYSTEM_FOLDERS:
        state[f"{label}_folder_id"] = _moref_value(props.get(prop))
    return state


# ===========================================================================
# datacenter.create -- Folder.CreateDatacenter (synchronous)
# ===========================================================================


async def datacenter_create_composite(
    *,
    operator: Operator,
    target: Any,
    params: dict[str, Any],
    connector: VmwareRestConnector,
) -> dict[str, Any] | OperationResult:
    """Create a datacenter. Op-id: ``vmware.composite.datacenter.create``.

    The parent is ``parent_folder_id`` or, when it is not given, the root
    folder that ``ServiceContent`` names. A vim fault (for example
    ``DuplicateName`` for a name that exists) raises with its type and text;
    the dispatcher records the call as failed.
    """
    name = params["name"]
    parent = params.get("parent_folder_id")
    if not isinstance(parent, str):
        parent, _alarm_manager = await read_service_content_refs(connector, target, operator)
    try:
        gate, payload = await _write_vmomi_sub_op(
            connector,
            target,
            operator,
            op_id=_OP_CREATE_DATACENTER,
            vmomi_path=f"/Folder/{parent}/CreateDatacenter",
            body={"name": name},
            params={"parent_folder_id": parent, "name": name},
        )
    except httpx.HTTPStatusError as exc:
        raise RuntimeError(
            f"datacenter.create: vCenter refused CreateDatacenter under folder {parent!r}: "
            f"{_vsphere_fault_detail(exc)}"
        ) from exc
    if gate is not None:
        return gate
    datacenter = _moref_value(payload)
    envelope: dict[str, Any] = {
        "status": "created",
        "name": name,
        "parent_folder_id": parent,
        "datacenter": datacenter,
        "datacenter_info": None,
        "guidance": None,
    }
    if datacenter is None:
        return {
            **envelope,
            "guidance": "vCenter created the datacenter but sent no id. Find it with "
            "vmware.datacenter.list.",
        }
    try:
        props = await read_object(
            connector,
            target,
            operator,
            mo_type=_DATACENTER_MO_TYPE,
            moid=datacenter,
            props=_DATACENTER_PROPS,
        )
    except Exception as exc:  # the create already happened; report, do not fail
        _log.warning("datacenter_create_read_back_failed", error=type(exc).__name__)
        props = None
    if props is None:
        return {
            **envelope,
            "guidance": "vCenter sent the new id, but the datacenter did not read back. "
            "Check with vmware.datacenter.list.",
        }
    return {**envelope, "datacenter_info": _datacenter_state(datacenter, props)}


async def _datacenter_create_preview(ctx: PreviewContext) -> dict[str, Any] | None:
    """Show the approver the new name and where it goes (no vendor call)."""
    name = ctx.params.get("name")
    if not isinstance(name, str):
        return None
    parent = ctx.params.get("parent_folder_id")
    where = f"folder {parent}" if isinstance(parent, str) else "the root folder"
    return {
        "summary": f"Create the datacenter {name!r} in {where}.",
        "name": name,
        "parent_folder_id": parent if isinstance(parent, str) else None,
    }


# ===========================================================================
# datacenter.delete -- Datacenter.Destroy_Task (empty datacenters only)
# ===========================================================================


async def plan_datacenter_delete(
    connector: VmwareRestConnector,
    target: Any,
    operator: Operator,
    params: dict[str, Any],
) -> TeardownPlan:
    """Plan an empty-datacenter delete (read-only).

    ``Destroy_Task`` on a datacenter removes everything in it, so the plan
    reads the four system folders and refuses when any of them holds an object
    (each one is named as a blocker), or when vCenter did not fully answer.
    The four empty system folders are the children that go with it.

    Known, accepted gap: vSphere has no "delete only if empty" call. An object
    added in the moment between this check and the ``Destroy_Task`` would be
    deleted with it.
    """
    datacenter = params["datacenter"]
    obj: dict[str, Any] = {"kind": "datacenter", "moid": datacenter}
    problem = moid_problem(datacenter, DATACENTER_MOID_PATTERN, "datacenter")
    if problem is not None:
        return TeardownPlan(obj, False, refusal=(STATUS_INVALID_REQUEST, problem))
    props = await read_object(
        connector,
        target,
        operator,
        mo_type=_DATACENTER_MO_TYPE,
        moid=datacenter,
        props=_DATACENTER_PROPS,
    )
    if props is None:
        return TeardownPlan(object=obj, present=False)
    obj.update(name=props.get("name"), parent=_moref_value(props.get("parent")))
    folders = {
        label: moid
        for prop, label in _SYSTEM_FOLDERS
        if (moid := _moref_value(props.get(prop))) is not None
    }
    plan = TeardownPlan(
        object=obj,
        present=True,
        children=[
            {"kind": "folder", "moid": moid, "name": label} for label, moid in folders.items()
        ],
    )
    contents, complete = await read_many(
        connector,
        target,
        operator,
        mo_type=_FOLDER_MO_TYPE,
        moids=list(folders.values()),
        props=["childEntity"],
    )
    if not complete or len(folders) < len(_SYSTEM_FOLDERS):
        plan.refusal = (
            STATUS_PRECONDITION_FAILED,
            f"vCenter did not fully answer what datacenter {datacenter!r} holds; refusing to "
            "delete on a partial answer",
        )
        return plan
    held = [
        ref for moid in folders.values() for ref in moref_values(contents[moid].get("childEntity"))
    ]
    obj["child_count"] = len(held)
    if held:
        plan.blockers = capped(await _named_rows(connector, target, operator, held))
        plan.refusal = (
            STATUS_PRECONDITION_FAILED,
            f"datacenter {datacenter!r} is not empty ({len(held)} object(s)); Destroy_Task "
            "would delete them too. Move or delete each object first (see 'blockers')",
        )
    return plan


async def _named_rows(
    connector: VmwareRestConnector,
    target: Any,
    operator: Operator,
    refs: list[tuple[str, str]],
) -> list[dict[str, Any]]:
    """``[{kind, moid, name}]`` for vim references (names best-effort, at most 50 per type)."""
    rows: list[dict[str, Any]] = []
    for mo_type in sorted({mo_type for mo_type, _ in refs}):
        moids = [moid for t, moid in refs if t == mo_type][:LIST_CAP]
        names = await read_names(connector, target, operator, mo_type=mo_type, moids=moids)
        rows.extend({"kind": mo_type, "moid": m, "name": names.get(m)} for m in moids)
    return rows


async def datacenter_delete_composite(
    *,
    operator: Operator,
    target: Any,
    params: dict[str, Any],
    connector: VmwareRestConnector,
) -> dict[str, Any] | OperationResult:
    """Delete one EMPTY datacenter. Op-id: ``vmware.composite.datacenter.delete``.

    Re-plans at run time, so a datacenter that changed after approval is
    judged on its live state: a refusal before any write, ``unchanged`` when it
    is already gone, else the gated ``Destroy_Task``, polled, then a read-back.
    """
    plan = await plan_datacenter_delete(connector, target, operator, params)
    early = pre_write_outcome(
        plan, absent_guidance="the datacenter does not exist; nothing was deleted"
    )
    if early is not None:
        return early
    datacenter = params["datacenter"]
    outcome = await destroy_task(
        connector,
        target,
        operator,
        op_id=_OP_DESTROY_DATACENTER_TASK,
        mo_type=_DATACENTER_MO_TYPE,
        moid=datacenter,
        params={"datacenter": datacenter},
        label="datacenter.delete",
    )
    if isinstance(outcome, OperationResult):
        return outcome
    if outcome.timed_out:
        return timeout_envelope(
            plan, outcome, method="Destroy_Task", reread="re-run to confirm (absent = unchanged)"
        )
    after = await read_object(
        connector,
        target,
        operator,
        mo_type=_DATACENTER_MO_TYPE,
        moid=datacenter,
        props=["name"],
    )
    return destroy_outcome(plan, outcome, gone=after is None, what="datacenter")


async def _datacenter_delete_preview(ctx: PreviewContext) -> dict[str, Any] | None:
    """Blast radius for ``datacenter.delete``; refuses a datacenter that does not exist."""
    datacenter = ctx.params.get("datacenter")
    if not isinstance(datacenter, str) or ctx.connector_instance is None:
        return None
    plan = await plan_datacenter_delete(
        ctx.connector_instance,  # type: ignore[arg-type]
        ctx.target,
        ctx.operator,
        {"datacenter": datacenter},
    )
    if plan.refusal is None and not plan.present:
        raise ValueError(f"datacenter {datacenter!r} does not exist; there is nothing to delete")
    return blast_radius(plan)
