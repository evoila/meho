# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Datastore browse + resolution helpers for the datastore file ops (#3339).

Read-only seam shared by ``datastore.file.delete`` and ``datastore.dir.create``
(:mod:`._datastore_files`): the relative-path contract, datastore -> name /
browser / datacenter resolution (walking ``Datastore.parent`` to the
``Datacenter``), ``HostDatastoreBrowser.SearchDatastore_Task`` /
``SearchDatastoreSubFolders_Task`` browsing, and the registered-VM file-claims
check. Every call here is a read (un-gated); a browse of a missing folder
(``FileNotFound`` task fault) reads as "absent", any other fault raises.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final

from meho_backplane.connectors.vmware_rest.composites._read import _extract_props_by_moid
from meho_backplane.connectors.vmware_rest.composites._teardown import (
    STATUS_INVALID_REQUEST,
    TeardownPlan,
    capped,
    moref_values,
    read_object,
)
from meho_backplane.connectors.vmware_rest.composites._write import (
    _DATASTORE_MO_TYPE,
    _VIRTUAL_MACHINE_MO_TYPE,
    _VMOMI_RETRIEVE_PROPERTIES_PATH,
    _moref_value,
    _unwrap_value,
)
from meho_backplane.connectors.vmware_rest.composites.schemas import DATASTORE_PATH_PATTERN
from meho_backplane.connectors.vmware_rest.vim_body import (
    VIM_TYPE_NAME_KEY,
    retrieve_properties_body,
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
    "vm_claims",
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
#: VMs per claims ``RetrievePropertiesEx`` -- small enough never to page.
_VM_CLAIM_CHUNK: Final = 50

_PATH_RE: Final = re.compile(DATASTORE_PATH_PATTERN)


def path_problem(path: Any) -> str | None:
    """Why *path* is not an acceptable datastore-relative path, or ``None``."""
    if not isinstance(path, str) or not path:
        return "path is required (relative to the datastore root; the root itself is refused)"
    if _PATH_RE.fullmatch(path) is None:
        return (
            f"path {path!r} is not a plain relative datastore path: it must not be the root, "
            "start with '/', contain '.' / '..' segments, wildcards, brackets, backslashes or "
            "control characters, or start in a hidden system area ('.*') or a content-library "
            "backing ('contentlib-*' -- use the content_library delete ops)"
        )
    return None


@dataclass(frozen=True)
class Datastore:
    """A resolved datastore: identity + what the FileManager / browser calls need."""

    moid: str
    name: str
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
        props=["name", "browser", "parent", "vm"],
    )
    if props is None:
        return None
    name = props.get("name")
    browser = _moref_value(unwrap_vim_value(props.get("browser")))
    datacenter = await resolve_datacenter(connector, target, operator, props.get("parent"))
    if not isinstance(name, str) or browser is None or datacenter is None:
        raise RuntimeError(
            f"datastore {datastore!r}: could not resolve its name / browser / datacenter"
        )
    vm_moids = [moid for _type, moid in moref_values(props.get("vm"))]
    return Datastore(datastore, name, browser, datacenter, vm_moids)


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
) -> dict[str, Any] | None:
    """The ``FileInfo`` of *path* (exact name match in its parent), else ``None``."""
    parent, _, base = path.rpartition("/")
    rows = await search(
        connector, target, operator, ds, folder=parent, pattern=base, recursive=False
    )
    for row in rows or []:
        for entry in row.get("file") or []:
            if isinstance(entry, dict) and entry.get("path") == base:
                return entry
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


_VM_CLAIM_PROPS: Final = [
    "name",
    "layoutEx.file",
    "config.files.vmPathName",
    "summary.config.vmPathName",
]


def _vm_files(props: dict[str, Any]) -> tuple[list[str], str | None]:
    """A VM's ``layoutEx`` file names + its ``.vmx`` path (either may be missing)."""
    layout = unwrap_vim_value(props.get("layoutEx.file"))
    names = [
        entry["name"]
        for entry in (layout if isinstance(layout, list) else [])
        if isinstance(entry, dict) and isinstance(entry.get("name"), str)
    ]
    vmx = props.get("config.files.vmPathName") or props.get("summary.config.vmPathName")
    return names, vmx if isinstance(vmx, str) else None


def _within(child: str, parent: str) -> bool:
    """``True`` when *child* is *parent* or lies below it (case-insensitive)."""
    child, parent = child.casefold(), parent.casefold()
    return child == parent or child.startswith(parent + "/")


def _claim_hits(ds: Datastore, props: dict[str, Any], path: str) -> list[str] | None:
    """The VM's files on *ds* a delete of *path* would remove; ``None`` = unknowable.

    Known file list (``layoutEx``): a file conflicts when it is *path* or lies
    below it. Without ``layoutEx`` (e.g. an inaccessible VM) the whole VM home
    directory is claimed, so *path* also conflicts when it lies inside that
    home. A VM with neither is unknowable -- the caller refuses.
    """
    names, vmx = _vm_files(props)
    rels = [rel for name in [*names, *([vmx] if vmx else [])] if (rel := ds.relative(name))]
    hits = {rel for rel in rels if _within(rel, path)}
    if not names:
        if vmx is None:
            return None
        if vmx.startswith(f"[{ds.name}]"):
            home = (ds.relative(vmx) or "").rpartition("/")[0]
            if home and _within(path, home):
                hits.add(home + "/")
    return sorted(hits)


async def vm_claims(
    connector: VmwareRestConnector, target: Any, operator: Operator, ds: Datastore, path: str
) -> tuple[list[dict[str, Any]], bool]:
    """Registered VMs owning a file a delete of *path* would remove: ``(rows, complete)``.

    Case-insensitive (refuses more, never less). ``complete`` is ``False`` when
    vCenter paged an answer or a VM's files were unreadable -- the caller then
    refuses rather than guess.
    """
    rows: list[dict[str, Any]] = []
    complete = True
    for start in range(0, len(ds.vm_moids), _VM_CLAIM_CHUNK):
        chunk = ds.vm_moids[start : start + _VM_CLAIM_CHUNK]
        result = await connector._post_vmomi_json(
            target,
            _VMOMI_RETRIEVE_PROPERTIES_PATH,
            operator=operator,
            json=retrieve_properties_body(_VIRTUAL_MACHINE_MO_TYPE, chunk, _VM_CLAIM_PROPS),
        )
        payload = _unwrap_value(result)
        if isinstance(payload, dict) and payload.get("token"):
            complete = False
        by_moid = _extract_props_by_moid(result)
        if set(chunk) - set(by_moid):
            complete = False
        for moid, props in by_moid.items():
            hits = _claim_hits(ds, props, path)
            if hits is None:
                complete = False
            elif hits:
                rows.append(
                    {"kind": "vm", "moid": moid, "name": props.get("name"), "files": hits[:10]}
                )
    return rows, complete


async def resolve_for_path(
    connector: VmwareRestConnector,
    target: Any,
    operator: Operator,
    params: dict[str, Any],
    obj: dict[str, Any],
) -> tuple[Datastore | None, TeardownPlan | None]:
    """Static path check + datastore resolution shared by both ops."""
    problem = path_problem(params.get("path"))
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
    obj.update(datastore_name=ds.name, datastore_path=ds.path_of(params["path"]))
    return ds, None
