# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Governed teardown deletes -- shared plan seam + ``folder.delete`` (#3339).

The teardown family closes the inverse of the create-side composites so an
environment teardown stays governed end to end:

* ``vmware.composite.network.portgroup.delete`` /
  ``vmware.composite.host.standard_portgroup.delete`` -- :mod:`._teardown_network`;
* ``vmware.composite.folder.delete`` -- here;
* ``vmware.composite.datastore.file.delete`` / ``datastore.dir.create`` --
  :mod:`._datastore_files`;
* ``vmware.composite.content_library.delete`` / ``content_library.item.delete``
  -- :mod:`._library`.

Every delete is ``safety_level="destructive"`` + ``requires_approval=True`` (the
governed-delete tier, ``docs/decisions/governed-delete-operations.md``): a human
approves every dispatch against a park-time blast radius, never the requester.
The vim deletes ride the composites' documented ``/sdk/vim25/{release}``
VI-JSON seam -- the pinned ``vcenter.yaml`` has no REST delete for these
objects and the ingested vim-object bindings 404 under ``/api`` (#3534) --
the same seam ``vm.destroy``'s pre-9.0 ``Destroy_Task`` arm runs on vCenter
8.0.x.

Plan-then-act
-------------

Each op computes one read-only *plan* (:class:`TeardownPlan`): the object's
identity, whether it exists, what goes with it, and -- before any write --
whether the delete must be refused. The park-time preview builder and the
post-approval handler call the **same** planning function, so the approver
reads exactly the refusal the handler will apply (the #3312 / #3323
preview/call parity lesson), and the handler re-plans at dispatch time so an
object that changed between park and approval is judged on its live state.

* absent object -> ``status="unchanged"`` (idempotent; nothing is written);
* a blocker -> ``status="precondition_failed"`` (or ``invalid_request`` for a
  malformed reference) naming the blockers, before any write;
* otherwise the gated write, then a task poll where the method is a
  ``*_Task`` -- a task **fault raises** (the dispatcher wraps it
  ``connector_error`` and audits the call failed, the #3881 review lesson), a
  poll timeout returns ``status="timeout"`` -- then a read-back that must find
  the object gone (``status="deleted"``), else ``status="still_present"``.

The blast-radius builders decline (``None``) without a resolved connector, like
``vm.destroy``'s: the live identity is what the approver signs, so an
egress-free projection would misstate the children. The dispatcher resolves
the connector at park time; the egress-free ``preview_operation`` binds the
params hash only.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Final

from meho_backplane.connectors import OperationResult
from meho_backplane.connectors.vmware_rest.composites._teardown_reads import (
    moid_problem,
    moref_values,
    read_names,
    read_object,
)
from meho_backplane.connectors.vmware_rest.composites._write import (
    _FOLDER_MO_TYPE,
    _OP_RETRIEVE_PROPERTIES,
    _VIRTUAL_MACHINE_MO_TYPE,
    _moref_value,
    _unwrap_value,
    _write_vmomi_sub_op,
)
from meho_backplane.connectors.vmware_rest.composites.schemas import FOLDER_MOID_PATTERN
from meho_backplane.connectors.vmware_rest.vim_body import unwrap_vim_value
from meho_backplane.connectors.vmware_rest.vim_task import TASK_STATE_ERROR, poll_vim_task

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors.vmware_rest.connector import VmwareRestConnector
    from meho_backplane.connectors.vmware_rest.vim_task import VimTaskResult
    from meho_backplane.operations._preview import PreviewContext

__all__ = [
    "TeardownPlan",
    "blast_radius",
    "destroy_outcome",
    "destroy_task",
    "envelope",
    "folder_delete_composite",
    "folder_delete_preview",
    "pre_write_outcome",
    "timeout_envelope",
    "vm_rows",
]

_OP_DESTROY_FOLDER_TASK: Final = "POST:/Folder/{moId}/Destroy_Task"

#: vi-json sub-op manifest (``_VIM_SUB_OPS_*`` namespace, so the vcenter.yaml
#: sweep skips it; the vi-json reconcile lane asserts every path exists).
_VIM_SUB_OPS_FOLDER_DELETE: Final[tuple[str, ...]] = (
    _OP_RETRIEVE_PROPERTIES,
    _OP_DESTROY_FOLDER_TASK,
)

_DATACENTER_MO_TYPE: Final = "Datacenter"

#: Wall-clock bound for the ``Destroy_Task`` polls -- the 600 s convention;
#: module-global so tests can zero it.
_DESTROY_TASK_TIMEOUT_SECONDS = 600.0

#: Cap on enumerated children / blockers carried inline (the counts stay exact).
LIST_CAP: Final = 50

#: Irreversibility classes stamped on the blast radius.
IRREVERSIBLE: Final = "permanent"
ALREADY_ABSENT: Final = "none-object-already-absent"

STATUS_DELETED: Final = "deleted"
STATUS_UNCHANGED: Final = "unchanged"
STATUS_PRECONDITION_FAILED: Final = "precondition_failed"
STATUS_INVALID_REQUEST: Final = "invalid_request"
STATUS_STILL_PRESENT: Final = "still_present"
STATUS_TIMEOUT: Final = "timeout"

# ---------------------------------------------------------------------------
# Shared plan / envelope helpers (also used by _datastore_files + _library)
# ---------------------------------------------------------------------------


@dataclass
class TeardownPlan:
    """The read-only decision a teardown delete is taken on.

    ``object`` is the identity the approver reads (and the response echoes);
    ``present`` whether it exists right now; ``children`` what goes with it;
    ``blockers`` what prevents the delete; ``refusal`` a ``(status,
    guidance)`` pair when the delete must not run; ``context`` the
    handler-internal resolution (switch / datacenter / browser moids) the
    write needs.
    """

    object: dict[str, Any]
    present: bool
    children: list[dict[str, Any]] = field(default_factory=list)
    blockers: list[dict[str, Any]] = field(default_factory=list)
    refusal: tuple[str, str] | None = None
    context: dict[str, Any] = field(default_factory=dict)


def blast_radius(plan: TeardownPlan) -> dict[str, Any]:
    """Render *plan* as the destructive-tier ``blast_radius`` preview block.

    ``object`` / ``children`` / ``irreversibility`` are the three fields the
    park gate requires (``_preview.blast_radius_missing_reason``). An absent
    object still yields a well-formed block (``present=False``, class
    :data:`ALREADY_ABSENT`) so a re-run of a finished teardown parks with an
    honest "nothing to delete" instead of a ``blast_radius_required`` denial;
    a refusal the handler will apply rides along so the approver sees it.
    """
    block: dict[str, Any] = {
        "object": {**plan.object, "present": plan.present},
        "children": plan.children,
        "irreversibility": IRREVERSIBLE if plan.present else ALREADY_ABSENT,
    }
    if plan.blockers:
        block["blockers"] = plan.blockers
    if plan.refusal is not None:
        block["refusal"] = {"status": plan.refusal[0], "guidance": plan.refusal[1]}
    return {"blast_radius": block}


def envelope(
    plan: TeardownPlan,
    status: str,
    *,
    task: str | None = None,
    task_state: str | None = None,
    guidance: str | None = None,
) -> dict[str, Any]:
    """The uniform teardown response envelope."""
    return {
        "status": status,
        "object": plan.object,
        "blockers": plan.blockers,
        "task": task,
        "task_state": task_state,
        "guidance": guidance,
    }


def pre_write_outcome(plan: TeardownPlan, *, absent_guidance: str) -> dict[str, Any] | None:
    """Return the refusal / ``unchanged`` envelope, or ``None`` to proceed with the write."""
    if plan.refusal is not None:
        status, guidance = plan.refusal
        return envelope(plan, status, guidance=guidance)
    if not plan.present:
        return envelope(plan, STATUS_UNCHANGED, guidance=absent_guidance)
    return None


def capped(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The first :data:`LIST_CAP` rows (counts are carried separately)."""
    return rows[:LIST_CAP]


async def vm_rows(
    connector: VmwareRestConnector,
    target: Any,
    operator: Operator,
    vm_moids: list[str],
) -> list[dict[str, Any]]:
    """``[{kind: vm, moid, name}]`` rows for blocker lists (names best-effort)."""
    names = await read_names(
        connector, target, operator, mo_type=_VIRTUAL_MACHINE_MO_TYPE, moids=vm_moids
    )
    return [{"kind": "vm", "moid": moid, "name": names.get(moid)} for moid in vm_moids]


async def destroy_task(
    connector: VmwareRestConnector,
    target: Any,
    operator: Operator,
    *,
    op_id: str,
    mo_type: str,
    moid: str,
    params: dict[str, Any],
    label: str,
) -> OperationResult | VimTaskResult:
    """Gate + issue one ``Destroy_Task`` and poll it to a terminal state.

    A parked / denied gate is returned verbatim (an :class:`OperationResult`)
    with nothing on the wire. A task fault raises (``connector_error``,
    audited failed -- never a returned status); success / timeout return the
    :class:`VimTaskResult`.
    """
    gate, task_payload = await _write_vmomi_sub_op(
        connector,
        target,
        operator,
        op_id=op_id,
        vmomi_path=f"/{mo_type}/{moid}/Destroy_Task",
        body={},
        params=params,
    )
    if gate is not None:
        return gate
    outcome = await poll_vim_task(
        connector,
        target,
        operator,
        task=_unwrap_value(task_payload),
        timeout_seconds=_DESTROY_TASK_TIMEOUT_SECONDS,
    )
    if outcome.state == TASK_STATE_ERROR:
        raise RuntimeError(
            f"{label}: Destroy_Task on {mo_type} {moid!r} faulted: "
            f"{outcome.error_message or '<no fault reported>'}"
        )
    return outcome


def destroy_outcome(
    plan: TeardownPlan, outcome: VimTaskResult, *, gone: bool, what: str
) -> dict[str, Any]:
    """The post-``Destroy_Task`` envelope: ``deleted`` iff the read-back found it gone."""
    return envelope(
        plan,
        STATUS_DELETED if gone else STATUS_STILL_PRESENT,
        task=outcome.task,
        task_state=outcome.state,
        guidance=None
        if gone
        else f"Destroy_Task succeeded but the {what} still reads back; re-check the inventory",
    )


def timeout_envelope(
    plan: TeardownPlan, outcome: VimTaskResult, *, method: str, reread: str
) -> dict[str, Any]:
    """The ``status='timeout'`` envelope for a task that outran the poll bound."""
    return envelope(
        plan,
        STATUS_TIMEOUT,
        task=outcome.task,
        task_state=outcome.state,
        guidance=(
            f"{method} {outcome.task} did not reach a terminal state within the poll "
            f"bound; it may still complete -- {reread}"
        ),
    )


# ===========================================================================
# folder.delete -- Folder.Destroy_Task (empty folders only)
# ===========================================================================

_FOLDER_PROPS: Final = ["name", "childEntity", "childType", "parent"]
#: A datastore cluster (``StoragePod``) is a kind of folder; refused.
_STORAGE_POD_PREFIX: Final = "group-p"


async def plan_folder_delete(
    connector: VmwareRestConnector,
    target: Any,
    operator: Operator,
    params: dict[str, Any],
) -> TeardownPlan:
    """Plan an empty-folder delete (read-only).

    ``Folder.Destroy_Task`` deletes a folder's *contents* recursively (VMs
    included), so a non-empty folder is always refused -- naming every child --
    and so is a root / datacenter system folder (``vm`` / ``host`` /
    ``datastore`` / ``network``) and a datastore cluster (``group-p``).

    Known, accepted gap: vSphere has no "delete only if empty" call. Something
    moved into the folder in the moment between this check and the
    ``Destroy_Task`` would be deleted with it.
    """
    folder = params["folder"]
    obj: dict[str, Any] = {"kind": "folder", "moid": folder}
    if isinstance(folder, str) and folder.startswith(_STORAGE_POD_PREFIX):
        detail = (
            f"{folder!r} is a datastore cluster (storage pod), not an inventory folder; "
            "this op does not delete datastore clusters"
        )
        return TeardownPlan(obj, False, refusal=(STATUS_INVALID_REQUEST, detail))
    problem = moid_problem(folder, FOLDER_MOID_PATTERN, "folder")
    if problem is not None:
        return TeardownPlan(obj, False, refusal=(STATUS_INVALID_REQUEST, problem))
    props = await read_object(
        connector, target, operator, mo_type=_FOLDER_MO_TYPE, moid=folder, props=_FOLDER_PROPS
    )
    if props is None:
        return TeardownPlan(object=obj, present=False)
    parent = unwrap_vim_value(props.get("parent"))
    parent_type = parent.get("type") if isinstance(parent, dict) else None
    child_type = unwrap_vim_value(props.get("childType"))
    obj.update(
        name=props.get("name"),
        parent=_moref_value(parent),
        child_type=child_type if isinstance(child_type, list) else [],
    )
    children = moref_values(props.get("childEntity"))
    obj["child_count"] = len(children)
    plan = TeardownPlan(object=obj, present=True)
    if parent_type in (None, _DATACENTER_MO_TYPE):
        plan.refusal = (
            STATUS_PRECONDITION_FAILED,
            f"folder {folder!r} is a root or datacenter system folder and cannot be deleted",
        )
        return plan
    if children:
        rows: list[dict[str, Any]] = []
        for mo_type in sorted({mo_type for mo_type, _ in children}):
            moids = [moid for t, moid in children if t == mo_type][:LIST_CAP]
            names = await read_names(connector, target, operator, mo_type=mo_type, moids=moids)
            rows.extend({"kind": mo_type, "moid": m, "name": names.get(m)} for m in moids)
        plan.blockers = capped(rows)
        plan.refusal = (
            STATUS_PRECONDITION_FAILED,
            f"folder {folder!r} is not empty ({len(children)} child object(s)); Destroy_Task "
            "would delete them too. Move or delete each child first (see 'blockers')",
        )
    return plan


async def folder_delete_composite(
    *,
    operator: Operator,
    target: Any,
    params: dict[str, Any],
    connector: VmwareRestConnector,
) -> dict[str, Any] | OperationResult:
    """Delete one EMPTY inventory folder via ``Folder.Destroy_Task`` (#3339).

    Op-id: ``vmware.composite.folder.delete``. Refuses a non-empty or system
    folder before any write; ``unchanged`` when the moid no longer exists;
    otherwise the gated ``Destroy_Task``, polled, then a read-back.
    """
    plan = await plan_folder_delete(connector, target, operator, params)
    early = pre_write_outcome(
        plan, absent_guidance="the folder does not exist; nothing was deleted"
    )
    if early is not None:
        return early
    folder = params["folder"]
    outcome = await destroy_task(
        connector,
        target,
        operator,
        op_id=_OP_DESTROY_FOLDER_TASK,
        mo_type=_FOLDER_MO_TYPE,
        moid=folder,
        params={"folder": folder},
        label="folder.delete",
    )
    if isinstance(outcome, OperationResult):
        return outcome
    if outcome.timed_out:
        return timeout_envelope(
            plan, outcome, method="Destroy_Task", reread="re-run to confirm (absent = unchanged)"
        )
    after = await read_object(
        connector, target, operator, mo_type=_FOLDER_MO_TYPE, moid=folder, props=["name"]
    )
    return destroy_outcome(plan, outcome, gone=after is None, what="folder")


async def folder_delete_preview(ctx: PreviewContext) -> dict[str, Any] | None:
    """Blast radius for ``folder.delete`` (the handler's own plan)."""
    folder = ctx.params.get("folder")
    if not isinstance(folder, str) or ctx.connector_instance is None:
        return None
    plan = await plan_folder_delete(
        ctx.connector_instance,  # type: ignore[arg-type]
        ctx.target,
        ctx.operator,
        {"folder": folder},
    )
    return blast_radius(plan)
