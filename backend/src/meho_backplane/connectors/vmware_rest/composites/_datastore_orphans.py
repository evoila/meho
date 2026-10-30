# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Read-only orphan folder check: ``vmware.composite.datastore.orphan_dirs`` (#3814).

One call on a vCenter target answers "which top-level folders on my
datastores does no registered VM use?". For every datastore (or the ones
named in ``datastores``) it returns one row per top-level folder with the
size and count of the files directly inside it, its latest change, the
registered VMs that use it (``claimed_by``) and a ``classification``:

* ``claimed`` -- a registered VM (templates included) uses a file below it;
* ``system`` -- a folder vSphere or ESXi keeps for itself (names starting
  with ``.``, ``contentlib-*``, ``vsantraces``, ``vmkdump``, ``fcd``,
  ``catalog``);
* ``orphan`` -- everything else. This only means "no VM registered in this
  vCenter uses it". ISO stores, content library storage and the folders of
  VMs registered in another vCenter are also unused here, so a row is a
  candidate to review, never a reason to delete.

How it reads, all through the shared seams:

1. ``GET /vcenter/datastore`` lists the datastores (filtered by name).
2. One ``read_many`` reads ``name``, ``summary.type``,
   ``summary.accessible``, ``browser`` and ``vm`` of every datastore.
3. One ``read_many`` reads the files of every VM on those datastores
   (``Datastore.vm``), with the rules of the delete check
   (:mod:`._datastore_claims`).
   Both reads follow every page of a paged answer, so a datastore or a VM
   on a later page is not missed.
4. Per datastore, one ``SearchDatastoreSubFolders_Task`` from the root
   (:func:`._datastore_browse.search`) gives the top-level folders and the
   files below each.

VMFS, NFS and vSAN datastores are read. On vSAN a top-level folder has a
UUID name and a friendly name; a VM uses the folder when its files match
either one (as in :mod:`._datastore_vsan`). Other types (vVol), datastores
that are not accessible, and datastores whose browse fails go into
``skipped`` with a reason; the rest of the answer still comes back.
vCenter targets only: a standalone ESXi target gets a typed
``unsupported_host_target`` answer. Nothing here writes.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final

from meho_backplane.connectors.vmware_rest.composites._datastore_browse import (
    _SUPPORTED_TYPES,
    Datastore,
    is_folder,
    search,
)
from meho_backplane.connectors.vmware_rest.composites._datastore_claims import (
    _CLAIM_PROPS,
    _claim_hits,
)
from meho_backplane.connectors.vmware_rest.composites._datastore_list import (
    _files_of,
    _int_or_none,
    _later,
    _str_or_none,
)
from meho_backplane.connectors.vmware_rest.composites._read import (
    _OP_LIST_DATASTORES,
    _read_sub_op,
    _unwrap_value,
)
from meho_backplane.connectors.vmware_rest.composites._teardown_reads import (
    moref_values,
    read_many,
)
from meho_backplane.connectors.vmware_rest.composites._write import (
    _DATASTORE_MO_TYPE,
    _VIRTUAL_MACHINE_MO_TYPE,
    _moref_value,
)
from meho_backplane.connectors.vmware_rest.host_target import (
    HOST_FLAVOR_VCENTER,
    classify_host_target,
)
from meho_backplane.connectors.vmware_rest.vim_body import unwrap_vim_value

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors.vmware_rest.connector import VmwareRestConnector

__all__ = ["datastore_orphan_dirs_composite", "is_system_folder"]

STATUS_OK: Final = "ok"
STATUS_UNSUPPORTED_HOST_TARGET: Final = "unsupported_host_target"

CLASS_ORPHAN: Final = "orphan"
CLASS_SYSTEM: Final = "system"
CLASS_CLAIMED: Final = "claimed"

VM_CHECK_COMPLETE: Final = "complete"
VM_CHECK_INCOMPLETE: Final = "incomplete"

_DS_PROPS: Final = ["name", "summary.type", "summary.accessible", "browser", "vm"]
_READ_TYPES: Final = _SUPPORTED_TYPES | {"VSAN"}

#: Top-level folders vSphere / ESXi keep for themselves (compared case-blind).
#: ``fcd`` and ``catalog`` hold first-class disks and their index: no VM lists
#: them, but they are not orphans.
_SYSTEM_NAMES: Final = frozenset({"vsantraces", "vmkdump", "fcd", "catalog"})
_SYSTEM_PREFIXES: Final = (".", "contentlib-")
_MAX_REASON_CHARS: Final = 300


def is_system_folder(name: str) -> bool:
    """``True`` for a top-level folder name vSphere or ESXi keeps for itself."""
    folded = name.casefold()
    return folded in _SYSTEM_NAMES or folded.startswith(_SYSTEM_PREFIXES)


async def _list_datastores(
    connector: VmwareRestConnector, target: Any, operator: Operator, names: list[str]
) -> list[tuple[str, str]]:
    """``[(moid, name)]`` of the datastores (only the ones in *names* when given)."""
    query = {"filter.names": names} if names else {}
    listing = _unwrap_value(
        await _read_sub_op(connector, target, operator, _OP_LIST_DATASTORES, query=query)
    )
    if not isinstance(listing, list):
        raise RuntimeError(
            f"datastore.orphan_dirs: expected a list from {_OP_LIST_DATASTORES!r}, "
            f"got {type(listing).__name__}"
        )
    found: list[tuple[str, str]] = []
    for entry in listing:
        if not isinstance(entry, dict) or not isinstance(entry.get("datastore"), str):
            continue
        name = str(entry.get("name") or entry["datastore"])
        # Some builds ignore a list filter, so filter here too.
        if not names or name in names:
            found.append((entry["datastore"], name))
    return found


def _skip_reason(props: dict[str, Any] | None) -> str | None:
    """Why a datastore cannot be read, or ``None`` when it can."""
    if props is None:
        return "vCenter did not answer for this datastore"
    if props.get("summary.accessible") is False:
        return "the datastore is not accessible (unmounted, or its storage cannot be reached)"
    ds_type = props.get("summary.type")
    if not isinstance(ds_type, str) or ds_type.upper() not in _READ_TYPES:
        return (
            f"datastore type {ds_type or 'unknown'!r} is not supported; only VMFS, NFS and "
            "vSAN datastores are read"
        )
    if _moref_value(unwrap_vim_value(props.get("browser"))) is None:
        return "the datastore browser could not be read"
    return None


def _as_datastore(moid: str, name: str, props: dict[str, Any]) -> Datastore:
    browser = _moref_value(unwrap_vim_value(props.get("browser"))) or ""
    vm_moids = [vm for _type, vm in moref_values(props.get("vm"))]
    raw_name = props.get("name")
    ds_name = raw_name if isinstance(raw_name, str) else name
    # The datacenter is only needed by the file writes; this read never uses it.
    return Datastore(moid, ds_name, str(props["summary.type"]), browser, "", vm_moids)


def _top_folders(ds: Datastore, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One record per top-level folder, with the size of the files directly inside it.

    Files in sub-folders do not count (as #3814 asks). Folder paths are keyed
    exactly: on case-sensitive storage ``Backup`` and ``backup`` are two
    folders.
    """
    folders: list[dict[str, Any]] = []
    by_key: dict[str, dict[str, Any]] = {}
    for row in rows:
        if ds.relative(str(row.get("folderPath") or "")) != "":
            continue
        for entry in _files_of(row):
            path = entry.get("path")
            if not is_folder(entry) or not isinstance(path, str) or not path:
                continue
            friendly = entry.get("friendlyName")
            folder: dict[str, Any] = {
                "folder": friendly if isinstance(friendly, str) and friendly else path,
                "path": path,
                "size_bytes": 0,
                "file_count": 0,
                "newest_modified": _str_or_none(entry.get("modification")),
                "claimed_by": [],
            }
            folders.append(folder)
            by_key[path] = folder
    for folder in folders:
        by_key.setdefault(folder["folder"], folder)
    for row in rows:
        rel = ds.relative(str(row.get("folderPath") or ""))
        top = by_key.get(rel) if rel and "/" not in rel else None
        if top is None:
            continue
        for entry in _files_of(row):
            if is_folder(entry):
                continue
            top["size_bytes"] += _int_or_none(entry.get("fileSize")) or 0
            top["file_count"] += 1
            top["newest_modified"] = _later(
                top["newest_modified"], _str_or_none(entry.get("modification"))
            )
    return folders


def _add_claims(
    ds: Datastore, folders: list[dict[str, Any]], vms: dict[str, dict[str, Any]]
) -> bool:
    """Fill ``claimed_by``; ``False`` when a VM's files could not be known."""
    complete = True
    for moid in ds.vm_moids:
        props = vms.get(moid)
        if props is None:
            complete = False
            continue
        name = str(props.get("name") or moid)
        for folder in folders:
            hits = _claim_hits(ds, props, folder["path"])
            if hits is not None and folder["folder"] != folder["path"]:
                by_name = _claim_hits(ds, props, folder["folder"])
                hits = None if by_name is None else hits + by_name
            if hits is None:
                complete = False
                break
            if hits:
                folder["claimed_by"].append(name)
    return complete


def _row(ds: Datastore, folder: dict[str, Any]) -> dict[str, Any]:
    claimed_by = sorted(set(folder["claimed_by"]))
    if claimed_by:
        classification = CLASS_CLAIMED
    elif is_system_folder(folder["folder"]) or is_system_folder(folder["path"]):
        classification = CLASS_SYSTEM
    else:
        classification = CLASS_ORPHAN
    return {
        "datastore": ds.name,
        "datastore_moid": ds.moid,
        "folder": folder["folder"],
        "datastore_path": ds.path_of(folder["path"]),
        "size_bytes": folder["size_bytes"],
        "file_count": folder["file_count"],
        "newest_modified": folder["newest_modified"],
        "classification": classification,
        "claimed_by": claimed_by,
    }


async def _resolve_datastores(
    connector: VmwareRestConnector, target: Any, operator: Operator, names: list[str]
) -> tuple[list[Datastore], list[dict[str, str]], bool]:
    """``(readable datastores, skipped rows, complete)`` for the requested names."""
    listed = await _list_datastores(connector, target, operator, names)
    found_names = {name for _moid, name in listed}
    skipped = [
        {"datastore": name, "reason": "no datastore with this name"}
        for name in dict.fromkeys(names)
        if name not in found_names
    ]
    ds_props, complete = await read_many(
        connector,
        target,
        operator,
        mo_type=_DATASTORE_MO_TYPE,
        moids=[moid for moid, _name in listed],
        props=_DS_PROPS,
        all_pages=True,
    )
    datastores: list[Datastore] = []
    for moid, name in listed:
        reason = _skip_reason(ds_props.get(moid))
        if reason is None:
            datastores.append(_as_datastore(moid, name, ds_props[moid]))
        else:
            skipped.append({"datastore": name, "reason": reason})
    return datastores, skipped, complete


def _reason(exc: Exception) -> str:
    text = f"the browse failed: {exc}" if str(exc) else f"the browse failed ({type(exc).__name__})"
    return text[:_MAX_REASON_CHARS]


async def datastore_orphan_dirs_composite(
    *,
    operator: Operator,
    target: Any,
    params: dict[str, Any],
    connector: VmwareRestConnector,
) -> dict[str, Any]:
    """List the top-level datastore folders and which of them no registered VM uses.

    Op-id: ``vmware.composite.datastore.orphan_dirs``. ``status`` is ``ok`` or
    ``unsupported_host_target``. The ``directories`` list turns into a result
    handle when it is large; ``skipped`` names the datastores that could not
    be read, with the reason.
    """
    names = [str(name) for name in params.get("datastores") or []]
    result: dict[str, Any] = {
        "status": STATUS_OK,
        "datastore_count": 0,
        "count": 0,
        "orphan_count": 0,
        "system_count": 0,
        "claimed_count": 0,
        "vm_check": None,
        "directories": [],
        "skipped": [],
        "guidance": None,
    }
    flavor, refusal = classify_host_target(target)
    if flavor != HOST_FLAVOR_VCENTER:
        result.update(
            status=STATUS_UNSUPPORTED_HOST_TARGET,
            target_product=(refusal or {}).get("target_product", flavor),
            guidance=(
                "this read needs a vCenter target: it compares the folders with the VMs "
                "registered in vCenter. On a standalone ESXi host, use "
                "vmware.composite.datastore.file.list with include_usage per datastore"
            ),
        )
        return result

    datastores, skipped, complete = await _resolve_datastores(connector, target, operator, names)
    vm_moids = list(dict.fromkeys(vm for ds in datastores for vm in ds.vm_moids))
    vms, vms_complete = await read_many(
        connector,
        target,
        operator,
        mo_type=_VIRTUAL_MACHINE_MO_TYPE,
        moids=vm_moids,
        props=_CLAIM_PROPS,
        all_pages=True,
    )
    complete = complete and vms_complete

    rows: list[dict[str, Any]] = []
    for ds in datastores:
        try:
            tree = await search(
                connector, target, operator, ds, folder="", pattern=None, recursive=True
            )
        except Exception as exc:  # one datastore failing must not sink the others
            skipped.append({"datastore": ds.name, "reason": _reason(exc)})
            continue
        if tree is None:
            skipped.append({"datastore": ds.name, "reason": "the datastore root was not found"})
            continue
        folders = _top_folders(ds, tree)
        complete = _add_claims(ds, folders, vms) and complete
        rows.extend(_row(ds, folder) for folder in folders)
        result["datastore_count"] += 1

    rows.sort(key=lambda row: (row["datastore"], row["folder"]))
    rows.sort(key=lambda row: row["newest_modified"] or "", reverse=True)
    for row in rows:
        result[f"{row['classification']}_count"] += 1
    result.update(
        count=len(rows),
        directories=rows,
        skipped=skipped,
        vm_check=VM_CHECK_COMPLETE if complete else VM_CHECK_INCOMPLETE,
    )
    return result
