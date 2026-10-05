# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Governed datastore file delete + directory create (#3339).

* ``vmware.composite.datastore.file.delete`` -- ``FileManager.
  DeleteDatastoreFile_Task`` (task-polled; a directory delete is recursive).
  The teardown residue it retires: VM home directories that survive a
  ``vm.destroy`` of a library-deployed appliance, staged ISOs, leftover
  installer folders.
* ``vmware.composite.datastore.dir.create`` -- ``FileManager.MakeDirectory``
  (synchronous, 204), the paired create.

Both ride the ``/sdk/vim25`` VI-JSON seam (the pinned ``vcenter.yaml`` has no
datastore-file surface) and address a file as ``datastore`` (moid) + ``path``
(relative to the datastore root). The datacenter the ``FileManager`` call
needs is resolved by walking ``Datastore.parent`` up to the ``Datacenter``;
existence and size come from ``HostDatastoreBrowser.SearchDatastore_Task`` on
the parent folder (``SearchDatastoreSubFolders_Task`` sizes a directory tree
for the blast radius). vCenter targets only (the standalone-ESXi SOAP
transport has no builder for these methods).

Guards (before any write; the parameter schema rejects the static ones at
preview time too):

* the path must be relative and plain: no root, no leading ``/``, no empty
  segment, no segment that starts with ``.`` (hidden / system files at any
  depth, and so no ``.`` / ``..``) or starts or ends with a space, no
  wildcards, brackets, backslashes or control characters;
* the top-level folder may not be ``contentlib-*`` (content-library backing --
  use the ``content_library`` delete ops), ``fcd`` or ``catalog`` (first-class
  disks, e.g. Kubernetes volumes, and their index), ignoring case;
* only VMFS and NFS datastores (on vSAN / vVol vCenter lists VM files by folder
  UUID, so the VM file check below could not be trusted);
* ``file.delete`` refuses a path that is -- or contains -- a file a registered
  VM uses (:mod:`._datastore_claims`: ``layoutEx.file``, device backings such
  as a mounted ISO, the ``.vmx``), naming the VMs. A partial answer from
  vCenter refuses rather than guessing.

Every check, the delete call and the read-back use the path as vSphere
resolved it while browsing, never the raw input text.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final

from meho_backplane.connectors import OperationResult
from meho_backplane.connectors.vmware_rest.composites._datastore_browse import (
    DATACENTER_MO_TYPE,
    SEARCH_DATASTORE_OP,
    SEARCH_DATASTORE_SUBFOLDERS_OP,
    Datastore,
    is_folder,
    resolve_for_path,
    search,
    stat_path,
    tree,
)
from meho_backplane.connectors.vmware_rest.composites._datastore_claims import vm_claims
from meho_backplane.connectors.vmware_rest.composites._teardown import (
    STATUS_DELETED,
    STATUS_PRECONDITION_FAILED,
    STATUS_STILL_PRESENT,
    STATUS_UNCHANGED,
    TeardownPlan,
    blast_radius,
    capped,
    envelope,
    pre_write_outcome,
    timeout_envelope,
)
from meho_backplane.connectors.vmware_rest.composites._write import (
    _OP_RETRIEVE_PROPERTIES,
    _unwrap_value,
    _write_vmomi_sub_op,
)
from meho_backplane.connectors.vmware_rest.vim_body import (
    VIM_TYPE_NAME_KEY,
    vim_moref,
)
from meho_backplane.connectors.vmware_rest.vim_task import TASK_STATE_ERROR, poll_vim_task

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors.vmware_rest.connector import VmwareRestConnector
    from meho_backplane.operations._preview import PreviewContext

__all__ = [
    "datastore_dir_create_composite",
    "datastore_dir_create_preview",
    "datastore_file_delete_composite",
    "datastore_file_delete_preview",
]

_OP_DELETE_DATASTORE_FILE_TASK: Final = "POST:/FileManager/{moId}/DeleteDatastoreFile_Task"
_OP_MAKE_DIRECTORY: Final = "POST:/FileManager/{moId}/MakeDirectory"

_VIM_SUB_OPS_DATASTORE_FILE_DELETE: Final[tuple[str, ...]] = (
    _OP_RETRIEVE_PROPERTIES,
    SEARCH_DATASTORE_OP,
    SEARCH_DATASTORE_SUBFOLDERS_OP,
    _OP_DELETE_DATASTORE_FILE_TASK,
)
_VIM_SUB_OPS_DATASTORE_DIR_CREATE: Final[tuple[str, ...]] = (
    _OP_RETRIEVE_PROPERTIES,
    SEARCH_DATASTORE_OP,
    _OP_MAKE_DIRECTORY,
)


#: ``ServiceContent.fileManager`` singleton moid on vCenter.
_FILE_MANAGER_MOID: Final = "FileManager"

#: Wall-clock bound for the delete task (module-global so tests can zero it).
_DELETE_TASK_TIMEOUT_SECONDS = 600.0


# ===========================================================================
# datastore.file.delete
# ===========================================================================


async def plan_datastore_file_delete(
    connector: VmwareRestConnector,
    target: Any,
    operator: Operator,
    params: dict[str, Any],
    *,
    enumerate_tree: bool,
) -> TeardownPlan:
    """Plan a datastore file / folder delete (read-only).

    Every check and the delete itself use the path as vSphere resolved it
    (``context["path"]``), not the raw input.
    """
    obj: dict[str, Any] = {
        "kind": "datastore_path",
        "datastore": params.get("datastore"),
        "path": params.get("path"),
    }
    ds, early = await resolve_for_path(connector, target, operator, params, obj)
    if early is not None or ds is None:
        return early or TeardownPlan(obj, False)
    found = await stat_path(connector, target, operator, ds, str(params["path"]))
    if found is None:
        return TeardownPlan(obj, False)
    entry, path = found
    folder = is_folder(entry)
    obj.update(
        kind="datastore_directory" if folder else "datastore_file",
        resolved_path=ds.path_of(path),
        file_type=entry.get(VIM_TYPE_NAME_KEY),
        size_bytes=entry.get("fileSize"),
        modified=entry.get("modification"),
    )
    plan = TeardownPlan(obj, True, context={"ds": ds, "path": path})
    claims, complete = await vm_claims(connector, target, operator, ds, path)
    plan.blockers = capped(claims)
    if claims:
        names = ", ".join(str(row.get("name") or row["moid"]) for row in claims[:10])
        plan.refusal = (
            STATUS_PRECONDITION_FAILED,
            f"{ds.path_of(path)!r} holds files that {len(claims)} registered VM(s) use "
            f"[{names}] (disks, .vmx, snapshots, logs, or a mounted ISO / floppy image). "
            "Delete or unregister the VM, or unmount the image, first; see 'blockers'",
        )
    elif not complete:
        plan.refusal = (
            STATUS_PRECONDITION_FAILED,
            "vCenter answered only part of the question 'which VMs use files here?' (a "
            "paged or unreadable answer, or a VM whose files cannot be read). Refusing rather "
            "than risk deleting a file a VM uses",
        )
    if folder and enumerate_tree:
        plan.children, obj["file_count"], obj["total_bytes"] = await tree(
            connector, target, operator, ds, path
        )
    return plan


async def datastore_file_delete_composite(
    *,
    operator: Operator,
    target: Any,
    params: dict[str, Any],
    connector: VmwareRestConnector,
) -> dict[str, Any] | OperationResult:
    """Delete one datastore file or directory (recursive) via ``DeleteDatastoreFile_Task``.

    Op-id: ``vmware.composite.datastore.file.delete``. Refuses an invalid /
    system path and any path holding a registered VM's files before any write;
    ``unchanged`` when the path does not exist; otherwise the gated task,
    polled (a fault raises), then a read-back that must find the path gone.
    """
    plan = await plan_datastore_file_delete(
        connector, target, operator, params, enumerate_tree=False
    )
    early = pre_write_outcome(plan, absent_guidance="the path does not exist; nothing was deleted")
    if early is not None:
        return early
    ds: Datastore = plan.context["ds"]
    path: str = plan.context["path"]
    gate, task_payload = await _write_vmomi_sub_op(
        connector,
        target,
        operator,
        op_id=_OP_DELETE_DATASTORE_FILE_TASK,
        vmomi_path=f"/FileManager/{_FILE_MANAGER_MOID}/DeleteDatastoreFile_Task",
        body={
            "name": ds.path_of(path),
            "datacenter": vim_moref(DATACENTER_MO_TYPE, ds.datacenter),
        },
        params={"datastore": ds.moid, "path": path},
    )
    if gate is not None:
        return gate
    outcome = await poll_vim_task(
        connector,
        target,
        operator,
        task=_unwrap_value(task_payload),
        timeout_seconds=_DELETE_TASK_TIMEOUT_SECONDS,
    )
    if outcome.state == TASK_STATE_ERROR:
        raise RuntimeError(
            f"datastore.file.delete: DeleteDatastoreFile_Task on {ds.path_of(path)!r} faulted: "
            f"{outcome.error_message or '<no fault reported>'}"
        )
    if outcome.timed_out:
        return timeout_envelope(
            plan, outcome, method="DeleteDatastoreFile_Task", reread="re-run to confirm"
        )
    gone = await stat_path(connector, target, operator, ds, path) is None
    return envelope(
        plan,
        STATUS_DELETED if gone else STATUS_STILL_PRESENT,
        task=outcome.task,
        task_state=outcome.state,
        guidance=None if gone else "the delete task succeeded but the path still reads back",
    )


async def datastore_file_delete_preview(ctx: PreviewContext) -> dict[str, Any] | None:
    """Blast radius for ``datastore.file.delete``: identity, size, the files a directory holds."""
    datastore = ctx.params.get("datastore")
    if not isinstance(datastore, str) or ctx.connector_instance is None:
        return None
    plan = await plan_datastore_file_delete(
        ctx.connector_instance,  # type: ignore[arg-type]
        ctx.target,
        ctx.operator,
        {"datastore": datastore, "path": ctx.params.get("path")},
        enumerate_tree=True,
    )
    return blast_radius(plan)


# ===========================================================================
# datastore.dir.create
# ===========================================================================

STATUS_CREATED: Final = "created"
STATUS_NOT_VERIFIED: Final = "not_verified"


async def _plan_dir_create(
    connector: VmwareRestConnector, target: Any, operator: Operator, params: dict[str, Any]
) -> TeardownPlan:
    """Plan a directory create; ``present`` means it already exists (-> unchanged)."""
    obj: dict[str, Any] = {
        "kind": "datastore_directory",
        "datastore": params.get("datastore"),
        "path": params.get("path"),
    }
    ds, early = await resolve_for_path(connector, target, operator, params, obj)
    if early is not None or ds is None:
        return early or TeardownPlan(obj, False)
    path = str(params["path"])  # validated by resolve_for_path
    parent, _, base = path.rpartition("/")
    rows = await search(
        connector, target, operator, ds, folder=parent, pattern=base, recursive=False
    )
    entry = next(
        (
            e
            for row in rows or []
            for e in row.get("file") or []
            if isinstance(e, dict) and e.get("path") == base
        ),
        None,
    )
    plan = TeardownPlan(obj, entry is not None, context={"ds": ds})
    if entry is not None and not is_folder(entry):
        plan.refusal = (STATUS_PRECONDITION_FAILED, f"{ds.path_of(path)!r} exists and is a file")
    elif rows is None and not params.get("create_parents", False):
        plan.refusal = (
            STATUS_PRECONDITION_FAILED,
            f"parent directory of {ds.path_of(path)!r} does not exist; pass create_parents=true",
        )
    return plan


async def datastore_dir_create_composite(
    *,
    operator: Operator,
    target: Any,
    params: dict[str, Any],
    connector: VmwareRestConnector,
) -> dict[str, Any] | OperationResult:
    """Create one datastore directory via ``FileManager.MakeDirectory`` (#3339).

    Op-id: ``vmware.composite.datastore.dir.create``. Same path guards as
    ``file.delete``; ``unchanged`` when the directory already exists; a missing
    parent is refused unless ``create_parents``. ``MakeDirectory`` is
    synchronous -- a vim fault raises (``connector_error``). A read-back must
    find the directory.
    """
    plan = await _plan_dir_create(connector, target, operator, params)
    if plan.refusal is not None:
        return envelope(plan, plan.refusal[0], guidance=plan.refusal[1])
    if plan.present:
        return envelope(plan, STATUS_UNCHANGED, guidance="the directory already exists")
    ds: Datastore = plan.context["ds"]
    path = params["path"]
    create_parents = bool(params.get("create_parents", False))
    gate, _payload = await _write_vmomi_sub_op(
        connector,
        target,
        operator,
        op_id=_OP_MAKE_DIRECTORY,
        vmomi_path=f"/FileManager/{_FILE_MANAGER_MOID}/MakeDirectory",
        body={
            "name": ds.path_of(path),
            "datacenter": vim_moref(DATACENTER_MO_TYPE, ds.datacenter),
            "createParentDirectories": create_parents,
        },
        params={"datastore": ds.moid, "path": path, "create_parents": create_parents},
    )
    if gate is not None:
        return gate
    found = await stat_path(connector, target, operator, ds, path)
    if found is not None and is_folder(found[0]):
        return envelope(plan, STATUS_CREATED)
    return envelope(
        plan,
        STATUS_NOT_VERIFIED,
        guidance="MakeDirectory returned but the directory does not read back as a folder",
    )


async def datastore_dir_create_preview(ctx: PreviewContext) -> dict[str, Any] | None:
    """Preview ``datastore.dir.create`` -- param echo (no I/O; a create, not destructive)."""
    datastore = ctx.params.get("datastore")
    path = ctx.params.get("path")
    if not isinstance(datastore, str) or not isinstance(path, str):
        return None
    return {
        "action": "create_datastore_directory",
        "datastore": datastore,
        "path": path,
        "create_parents": bool(ctx.params.get("create_parents", False)),
    }
