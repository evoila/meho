# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Read-only vSAN folder list: ``vmware.composite.datastore.vsan.folder.list`` (#4022).

On a vSAN datastore every top-level folder (a VM home, for example) is a
vSAN object. vCenter names it by a UUID; the name people know is the
``friendlyName`` of the folder entry in the root browse. This read shows both,
so the check after a VM delete ("is the VM's folder gone?") can run through
MEHO instead of ``govc``.

* Without ``folder``: the top-level folders. Per folder the friendly name,
  the UUID path, the size and file count of all files below it (one
  ``SearchDatastoreSubFolders_Task`` from the root), the latest change and
  the registered VMs that use it. A VM uses a folder when one of its files
  (the same rules as the ``datastore.file.delete`` check,
  :mod:`._datastore_claims`) lies below the UUID path or below the friendly
  name. Hidden folders (name starts with ``.``) are left out.
* With ``folder`` (friendly name or UUID path, may go deeper): the entries
  directly in that folder, with size and last change. A missing folder is
  ``status="absent"``, not an error.

The browse is the shared ``HostDatastoreBrowser`` seam of
:mod:`._datastore_browse` on the ``/sdk/vim25`` VI-JSON path. The VMFS / NFS
guard of the datastore writes (``resolve_for_path``) is not used here: this op
writes nothing, and it shows both names instead of trusting one. vCenter
targets only, like the datastore writes.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import TYPE_CHECKING, Any, Final

from meho_backplane.connectors.vmware_rest.composites._datastore_browse import (
    Datastore,
    is_folder,
    resolve_datastore,
    search,
)
from meho_backplane.connectors.vmware_rest.composites._datastore_claims import (
    _CLAIM_PROPS,
    _claim_hits,
)
from meho_backplane.connectors.vmware_rest.composites._teardown_reads import (
    moid_problem,
    read_many,
)
from meho_backplane.connectors.vmware_rest.composites._write import _VIRTUAL_MACHINE_MO_TYPE
from meho_backplane.connectors.vmware_rest.composites.schemas import (
    DATASTORE_MOID_PATTERN,
    VSAN_FOLDER_PATTERN,
)
from meho_backplane.connectors.vmware_rest.vim_body import VIM_TYPE_NAME_KEY

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors.vmware_rest.connector import VmwareRestConnector

__all__ = ["datastore_vsan_folder_list_composite"]

STATUS_OK: Final = "ok"
STATUS_ABSENT: Final = "absent"
STATUS_NOT_FOUND: Final = "not_found"
STATUS_INVALID_REQUEST: Final = "invalid_request"

VM_CHECK_COMPLETE: Final = "complete"
VM_CHECK_INCOMPLETE: Final = "incomplete"

#: ``Datastore.summary.type`` of a vSAN datastore.
_VSAN_TYPE: Final = "vsan"

_FOLDER_RE: Final = re.compile(VSAN_FOLDER_PATTERN)


def _int_or_none(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _str_or_none(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def _files_of(row: dict[str, Any]) -> list[dict[str, Any]]:
    return [entry for entry in row.get("file") or [] if isinstance(entry, dict)]


def _later(current: str | None, candidate: str | None) -> str | None:
    """The later of two vSphere time stamps (ISO 8601); text order if one does not parse."""
    if candidate is None:
        return current
    if current is None:
        return candidate
    try:
        newer = datetime.fromisoformat(candidate) > datetime.fromisoformat(current)
    except ValueError:
        newer = candidate > current
    return candidate if newer else current


def _folder_problem(folder: Any) -> str | None:
    if folder is None:
        return None
    if isinstance(folder, str) and _FOLDER_RE.fullmatch(folder):
        return None
    return (
        f"folder {folder!r} is not allowed. Give a folder name (the friendly name or the "
        "UUID path from the folder list), e.g. 'my-vm'. Not allowed: a leading or trailing "
        "'/', empty segments, '.' or '..', wildcards, brackets, backslashes, control characters"
    )


async def _top_folders(
    connector: VmwareRestConnector, target: Any, operator: Operator, ds: Datastore
) -> list[dict[str, Any]]:
    """One row per visible top-level folder (no sizes or VMs yet)."""
    rows = await search(connector, target, operator, ds, folder="", pattern=None, recursive=False)
    folders: list[dict[str, Any]] = []
    for row in rows or []:
        for entry in _files_of(row):
            uuid_path = entry.get("path")
            if not is_folder(entry) or not isinstance(uuid_path, str):
                continue
            if not uuid_path or uuid_path.startswith("."):
                continue
            friendly = entry.get("friendlyName")
            folders.append(
                {
                    "name": friendly if isinstance(friendly, str) and friendly else uuid_path,
                    "uuid_path": uuid_path,
                    "datastore_path": ds.path_of(uuid_path),
                    "size_bytes": 0,
                    "file_count": 0,
                    "modified": _str_or_none(entry.get("modification")),
                    "vms": [],
                }
            )
    return folders


async def _add_sizes(
    connector: VmwareRestConnector,
    target: Any,
    operator: Operator,
    ds: Datastore,
    folders: list[dict[str, Any]],
) -> None:
    """Sum every file below each top-level folder (one recursive browse from the root)."""
    by_key: dict[str, dict[str, Any]] = {}
    for folder in folders:
        by_key[folder["uuid_path"].casefold()] = folder
    for folder in folders:
        by_key.setdefault(folder["name"].casefold(), folder)
    rows = await search(connector, target, operator, ds, folder="", pattern=None, recursive=True)
    for row in rows or []:
        rel = ds.relative(str(row.get("folderPath") or ""))
        if not rel:
            continue
        top = by_key.get(rel.split("/", 1)[0].casefold())
        if top is None:
            continue
        for entry in _files_of(row):
            if is_folder(entry):
                continue
            top["size_bytes"] += _int_or_none(entry.get("fileSize")) or 0
            top["file_count"] += 1
            top["modified"] = _later(top["modified"], _str_or_none(entry.get("modification")))


async def _add_vms(
    connector: VmwareRestConnector,
    target: Any,
    operator: Operator,
    ds: Datastore,
    folders: list[dict[str, Any]],
) -> str:
    """Name the registered VMs that use each folder; return the ``vm_check`` value."""
    by_moid, complete = await read_many(
        connector,
        target,
        operator,
        mo_type=_VIRTUAL_MACHINE_MO_TYPE,
        moids=ds.vm_moids,
        props=_CLAIM_PROPS,
    )
    for moid, props in by_moid.items():
        name = str(props.get("name") or moid)
        for folder in folders:
            hits = _claim_hits(ds, props, folder["uuid_path"])
            if hits is not None and folder["name"] != folder["uuid_path"]:
                by_name = _claim_hits(ds, props, folder["name"])
                hits = None if by_name is None else hits + by_name
            if hits is None:
                complete = False
                break
            if hits:
                folder["vms"].append(name)
    for folder in folders:
        folder["vms"].sort()
    return VM_CHECK_COMPLETE if complete else VM_CHECK_INCOMPLETE


def _folder_entries(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for row in rows:
        for entry in _files_of(row):
            folder_entry = is_folder(entry)
            out.append(
                {
                    "name": str(entry.get("path") or ""),
                    "kind": "folder" if folder_entry else "file",
                    "file_type": _str_or_none(entry.get(VIM_TYPE_NAME_KEY)),
                    "size_bytes": None if folder_entry else _int_or_none(entry.get("fileSize")),
                    "modified": _str_or_none(entry.get("modification")),
                }
            )
    return out


async def datastore_vsan_folder_list_composite(
    *,
    operator: Operator,
    target: Any,
    params: dict[str, Any],
    connector: VmwareRestConnector,
) -> dict[str, Any]:
    """List the top-level folders of a vSAN datastore, or the files of one folder.

    Op-id: ``vmware.composite.datastore.vsan.folder.list``. ``status`` is
    ``ok``, ``absent`` (the folder does not exist), ``not_found`` (no such
    datastore) or ``invalid_request`` (bad input, or not a vSAN datastore).
    The ``entries`` list turns into a result handle when it is large. Nothing
    here writes.
    """
    datastore = params.get("datastore")
    folder = params.get("folder")
    result: dict[str, Any] = {
        "status": STATUS_OK,
        "datastore": datastore,
        "datastore_name": None,
        "datastore_type": None,
        "folder": folder,
        "datastore_path": None,
        "count": 0,
        "entries": [],
        "vm_check": None,
        "guidance": None,
    }

    def answer(status: str, guidance: str) -> dict[str, Any]:
        result.update(status=status, guidance=guidance)
        return result

    problem = moid_problem(datastore, DATASTORE_MOID_PATTERN, "datastore")
    problem = problem or _folder_problem(folder)
    if problem is not None:
        return answer(STATUS_INVALID_REQUEST, problem)
    ds = await resolve_datastore(connector, target, operator, str(datastore))
    if ds is None:
        return answer(
            STATUS_NOT_FOUND,
            f"datastore {datastore!r} not found; vmware.composite.datastore.usage lists the moids",
        )
    result.update(
        datastore_name=ds.name,
        datastore_type=ds.type or None,
        datastore_path=ds.path_of(folder or ""),
    )
    if ds.type.casefold() != _VSAN_TYPE:
        return answer(
            STATUS_INVALID_REQUEST,
            f"datastore {ds.name!r} has type {ds.type or 'unknown'!r}; this read is for vSAN "
            "datastores only",
        )

    if folder is None:
        entries = await _top_folders(connector, target, operator, ds)
        result["vm_check"] = VM_CHECK_COMPLETE
        if entries:
            await _add_sizes(connector, target, operator, ds, entries)
            result["vm_check"] = await _add_vms(connector, target, operator, ds, entries)
    else:
        rows = await search(
            connector, target, operator, ds, folder=folder, pattern=None, recursive=False
        )
        if rows is None:
            return answer(STATUS_ABSENT, f"folder {ds.path_of(folder)!r} does not exist")
        entries = _folder_entries(rows)
    result.update(entries=entries, count=len(entries))
    return result
