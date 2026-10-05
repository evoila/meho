# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Datastore browse + resolution helpers for the datastore file ops (#3339).

Read-only seam shared by ``datastore.file.delete`` and ``datastore.dir.create``
(:mod:`._datastore_files`): the relative-path rules, datastore -> name /
type / browser / datacenter resolution (walking ``Datastore.parent`` to the
``Datacenter``), and ``HostDatastoreBrowser.SearchDatastore_Task`` /
``SearchDatastoreSubFolders_Task`` browsing. Every call here is a read
(un-gated); a browse of a missing folder (``FileNotFound`` task fault) reads
as "absent", any other fault raises. The registered-VM file check lives in
:mod:`._datastore_claims`.

Only VMFS and NFS datastores are supported. On vSAN and vVol datastores
vCenter lists VM files by a folder UUID, while people use the friendly folder
name, so the VM file check could not match them; those datastores (and any
other or unknown type) are refused.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final

from meho_backplane.connectors.vmware_rest.composites._teardown import (
    STATUS_INVALID_REQUEST,
    TeardownPlan,
    capped,
)
from meho_backplane.connectors.vmware_rest.composites._teardown_reads import (
    moid_problem,
    moref_values,
    read_object,
)
from meho_backplane.connectors.vmware_rest.composites._write import (
    _DATASTORE_MO_TYPE,
    _moref_value,
    _unwrap_value,
)
from meho_backplane.connectors.vmware_rest.composites.schemas import (
    DATASTORE_MOID_PATTERN,
    DATASTORE_PATH_PATTERN,
)
from meho_backplane.connectors.vmware_rest.vim_body import (
    VIM_TYPE_NAME_KEY,
    unwrap_vim_value,
)
from meho_backplane.connectors.vmware_rest.vim_task import TASK_STATE_ERROR, poll_vim_task

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors.vmware_rest.connector import VmwareRestConnector

__all__ = [
    "DATACENTER_MO_TYPE",
    "DATASTORE_PATH_PATTERN",
    "SEARCH_DATASTORE_OP",
    "SEARCH_DATASTORE_SUBFOLDERS_OP",
    "Datastore",
    "is_folder",
    "path_problem",
    "resolve_for_path",
    "search",
    "stat_path",
    "tree",
]

#: Read-only browse methods (``Datastore.Browse``); un-gated like the
#: ``RetrievePropertiesEx`` reads. They return a Task whose result carries the
#: ``HostDatastoreBrowserSearchResults``.
SEARCH_DATASTORE_OP: Final = "POST:/HostDatastoreBrowser/{moId}/SearchDatastore_Task"
SEARCH_DATASTORE_SUBFOLDERS_OP: Final = (
    "POST:/HostDatastoreBrowser/{moId}/SearchDatastoreSubFolders_Task"
)

_BROWSER_MO_TYPE: Final = "HostDatastoreBrowser"
DATACENTER_MO_TYPE: Final = "Datacenter"
#: ``_typeName`` discriminators of the browse request DataObjects (#3103).
_SEARCH_SPEC_TYPE: Final = "HostDatastoreBrowserSearchSpec"
_FILE_QUERY_FLAGS_TYPE: Final = "FileQueryFlags"
_FOLDER_FILE_INFO_TYPE: Final = "FolderFileInfo"
_FILE_NOT_FOUND_FAULT: Final = "FileNotFound"

#: Wall-clock bound for a browse task (module-global so tests can zero it).
_SEARCH_TASK_TIMEOUT_SECONDS = 120.0

#: Max ``Datastore.parent`` hops to the Datacenter (folders / a storage pod).
_MAX_PARENT_HOPS: Final = 12
#: ``Datastore.summary.type`` values these ops support. vSAN / vVol (and any
#: other or unknown type) are refused -- see the module docstring.
_SUPPORTED_TYPES: Final = frozenset({"VMFS", "NFS", "NFS41"})

_PATH_RE: Final = re.compile(DATASTORE_PATH_PATTERN)


def path_problem(path: Any) -> str | None:
    """Why *path* is not an acceptable datastore-relative path, or ``None``."""
    if not isinstance(path, str) or not path:
        return "path is required (relative to the datastore root; the root itself is refused)"
    if _PATH_RE.fullmatch(path) is None:
        return (
            f"path {path!r} is not allowed. Use a plain path relative to the datastore root. "
            "Not allowed: the root itself, a leading '/', empty segments, spaces at the start "
            "or end of a segment, names starting with '.' (hidden or system files, at any "
            "depth), wildcards, brackets, backslashes, control characters, and the top-level "
            "folders 'contentlib-*' (use the content_library delete ops), 'fcd' and "
            "'catalog' (first-class disks and their index)"
        )
    return None


@dataclass(frozen=True)
class Datastore:
    """A resolved datastore: identity + what the FileManager / browser calls need."""

    moid: str
    name: str
    type: str
    browser: str
    datacenter: str
    vm_moids: list[str]

    def path_of(self, rel: str) -> str:
        """``[name] rel`` (``[name]`` for the root)."""
        return f"[{self.name}] {rel}" if rel else f"[{self.name}]"

    def relative(self, ds_path: str) -> str | None:
        """*ds_path* relative to this datastore's root, or ``None`` if elsewhere."""
        prefix = f"[{self.name}]"
        if not ds_path.startswith(prefix):
            return None
        return ds_path[len(prefix) :].strip().strip("/")


async def resolve_datacenter(
    connector: VmwareRestConnector, target: Any, operator: Operator, parent: Any
) -> str | None:
    """Walk ``parent`` MoRefs up to the owning ``Datacenter`` moid."""
    ref = unwrap_vim_value(parent)
    for _ in range(_MAX_PARENT_HOPS):
        if not isinstance(ref, dict) or not isinstance(ref.get("value"), str):
            return None
        mo_type = str(ref.get("type") or "")
        if mo_type == DATACENTER_MO_TYPE:
            return str(ref["value"])
        props = await read_object(
            connector, target, operator, mo_type=mo_type, moid=ref["value"], props=["parent"]
        )
        ref = unwrap_vim_value(props.get("parent")) if props is not None else None
    return None


async def resolve_datastore(
    connector: VmwareRestConnector, target: Any, operator: Operator, datastore: str
) -> Datastore | None:
    """Resolve *datastore* (moid); ``None`` when it does not exist."""
    props = await read_object(
        connector,
        target,
        operator,
        mo_type=_DATASTORE_MO_TYPE,
        moid=datastore,
        props=["name", "summary.type", "browser", "parent", "vm"],
    )
    if props is None:
        return None
    name = props.get("name")
    ds_type = props.get("summary.type")
    browser = _moref_value(unwrap_vim_value(props.get("browser")))
    datacenter = await resolve_datacenter(connector, target, operator, props.get("parent"))
    if not isinstance(name, str) or browser is None or datacenter is None:
        raise RuntimeError(
            f"datastore {datastore!r}: could not resolve its name / browser / datacenter"
        )
    vm_moids = [moid for _type, moid in moref_values(props.get("vm"))]
    type_name = ds_type if isinstance(ds_type, str) else ""
    return Datastore(datastore, name, type_name, browser, datacenter, vm_moids)


async def search(
    connector: VmwareRestConnector,
    target: Any,
    operator: Operator,
    ds: Datastore,
    *,
    folder: str,
    pattern: str | None,
    recursive: bool,
) -> list[dict[str, Any]] | None:
    """Browse *folder* (relative); the result rows, or ``None`` if the folder is missing."""
    spec: dict[str, Any] = {
        VIM_TYPE_NAME_KEY: _SEARCH_SPEC_TYPE,
        "details": {
            VIM_TYPE_NAME_KEY: _FILE_QUERY_FLAGS_TYPE,
            "fileType": True,
            "fileSize": True,
            "modification": True,
            "fileOwner": False,
        },
        "sortFoldersFirst": True,
    }
    if pattern is not None:
        spec["matchPattern"] = [pattern]
    method = "SearchDatastoreSubFolders_Task" if recursive else "SearchDatastore_Task"
    task = await connector._post_vmomi_json(
        target,
        f"/{_BROWSER_MO_TYPE}/{ds.browser}/{method}",
        operator=operator,
        json={"datastorePath": ds.path_of(folder), "searchSpec": spec},
    )
    outcome = await poll_vim_task(
        connector,
        target,
        operator,
        task=_unwrap_value(task),
        timeout_seconds=_SEARCH_TASK_TIMEOUT_SECONDS,
    )
    if outcome.state == TASK_STATE_ERROR:
        if outcome.fault_type == _FILE_NOT_FOUND_FAULT:
            return None
        raise RuntimeError(
            f"{method} on {ds.path_of(folder)!r} faulted: "
            f"{outcome.error_message or '<no fault reported>'}"
        )
    if outcome.timed_out:
        raise RuntimeError(f"{method} on {ds.path_of(folder)!r} did not finish in time")
    result = outcome.result
    rows = result if isinstance(result, list) else [result]
    return [row for row in rows if isinstance(row, dict)]


async def stat_path(
    connector: VmwareRestConnector, target: Any, operator: Operator, ds: Datastore, path: str
) -> tuple[dict[str, Any], str] | None:
    """``(FileInfo, path as vSphere resolved it)`` for *path*, or ``None`` if absent.

    Browses the parent folder for an exact name match. The returned path is
    built from vSphere's own answer (the result's ``folderPath`` + the entry
    name), so every later check and the delete itself use the object vSphere
    actually found, never the raw input text.
    """
    parent, _, base = path.rpartition("/")
    rows = await search(
        connector, target, operator, ds, folder=parent, pattern=base, recursive=False
    )
    for row in rows or []:
        for entry in row.get("file") or []:
            if isinstance(entry, dict) and entry.get("path") == base:
                folder = ds.relative(str(row.get("folderPath") or ""))
                if folder is None:
                    raise RuntimeError(
                        f"browse of {ds.path_of(parent)!r} answered with a folder on another "
                        f"datastore ({row.get('folderPath')!r}); refusing"
                    )
                return entry, f"{folder}/{base}" if folder else base
    return None


def is_folder(entry: dict[str, Any]) -> bool:
    return entry.get(VIM_TYPE_NAME_KEY) == _FOLDER_FILE_INFO_TYPE


async def tree(
    connector: VmwareRestConnector, target: Any, operator: Operator, ds: Datastore, path: str
) -> tuple[list[dict[str, Any]], int, int]:
    """Every file under directory *path*: ``(capped rows, file count, total bytes)``."""
    rows = await search(connector, target, operator, ds, folder=path, pattern=None, recursive=True)
    files: list[dict[str, Any]] = []
    total = 0
    for row in rows or []:
        folder = ds.relative(str(row.get("folderPath") or "")) or path
        for entry in row.get("file") or []:
            if not isinstance(entry, dict) or is_folder(entry):
                continue
            size = entry.get("fileSize")
            size = size if isinstance(size, int) and not isinstance(size, bool) else 0
            total += size
            files.append(
                {"kind": "file", "path": f"{folder}/{entry.get('path')}", "size_bytes": size}
            )
    return capped(files), len(files), total


async def resolve_for_path(
    connector: VmwareRestConnector,
    target: Any,
    operator: Operator,
    params: dict[str, Any],
    obj: dict[str, Any],
) -> tuple[Datastore | None, TeardownPlan | None]:
    """Path rules + datastore resolution shared by both ops (refusal plan or datastore)."""
    problem = moid_problem(params.get("datastore"), DATASTORE_MOID_PATTERN, "datastore")
    problem = problem or path_problem(params.get("path"))
    if problem is not None:
        return None, TeardownPlan(obj, False, refusal=(STATUS_INVALID_REQUEST, problem))
    ds = await resolve_datastore(connector, target, operator, params["datastore"])
    if ds is None:
        refusal = (
            STATUS_INVALID_REQUEST,
            f"datastore {params['datastore']!r} not found; pass a Datastore moid "
            "(e.g. from vmware.composite.datastore.usage)",
        )
        return None, TeardownPlan(obj, False, refusal=refusal)
    obj.update(
        datastore_name=ds.name,
        datastore_type=ds.type,
        datastore_path=ds.path_of(params["path"]),
    )
    if ds.type.upper() not in _SUPPORTED_TYPES:
        refusal = (
            STATUS_INVALID_REQUEST,
            f"datastore {ds.name!r} has type {ds.type or 'unknown'!r}. Only VMFS and NFS "
            "datastores are supported: on vSAN and vVol datastores vCenter lists VM files by "
            "folder UUID, so the check for files of registered VMs could not be trusted",
        )
        return None, TeardownPlan(obj, False, refusal=refusal)
    return ds, None
