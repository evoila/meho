# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Read-only datastore file list: ``vmware.composite.datastore.file.list`` (#3432).

Lists the files and folders in one folder of a datastore, by datastore
**name** and a path relative to the datastore root (empty = the root), with
an optional file-name pattern such as ``*.iso``. Each row has ``name``,
``path``, ``kind`` (file / folder), ``file_type``, ``size_bytes`` and
``modified``. The browse is ``HostDatastoreBrowser.SearchDatastore_Task`` on
the shared seam in :mod:`._datastore_browse`.

With ``include_usage`` the op also:

* sums every folder: one ``SearchDatastoreSubFolders_Task`` below the listed
  folder gives each folder row ``size_bytes`` (all files below it),
  ``file_count`` and ``latest_modified``;
* names the registered VMs that use each row (the same rules as the
  ``datastore.file.delete`` check, :func:`._datastore_claims.vm_users`). Only
  on VMFS and NFS datastores: on vSAN and vVol, vCenter names VM files by
  folder UUID, so the check is skipped (``vm_check="not_supported"``).

Works on a vCenter target and on a standalone ESXi host. The flavor comes
from :func:`~..host_target.classify_host_target`: on vCenter the name is
found with ``GET /vcenter/datastore``; on ESXi (which has no such call) with
the ``ha-datacenter`` ``datastore`` list. The browse and its task poll go
through ``_post_vmomi_json``, which speaks VI-JSON to vCenter and SOAP to
ESXi. A target that is neither gets a typed ``unsupported_host_target``
answer. Nothing here writes.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import TYPE_CHECKING, Any, Final

from meho_backplane.connectors.vmware_rest.composites._datastore_browse import (
    _SUPPORTED_TYPES,
    Datastore,
    is_folder,
    resolve_datastore,
    search,
)
from meho_backplane.connectors.vmware_rest.composites._datastore_claims import vm_users
from meho_backplane.connectors.vmware_rest.composites._read import (
    _OP_LIST_DATASTORES,
    _read_sub_op,
    _unwrap_value,
)
from meho_backplane.connectors.vmware_rest.composites._teardown_reads import (
    IncompleteAnswerError,
    moref_values,
    read_many,
    read_object,
)
from meho_backplane.connectors.vmware_rest.composites.schemas import DATASTORE_MOID_PATTERN
from meho_backplane.connectors.vmware_rest.host_target import (
    HOST_FLAVOR_ESXI,
    classify_host_target,
)
from meho_backplane.connectors.vmware_rest.vim_body import VIM_TYPE_NAME_KEY

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors.vmware_rest.connector import VmwareRestConnector

__all__ = ["datastore_file_list_composite"]

STATUS_OK: Final = "ok"
STATUS_NOT_FOUND: Final = "not_found"
STATUS_AMBIGUOUS: Final = "ambiguous"
STATUS_INVALID_REQUEST: Final = "invalid_request"

VM_CHECK_COMPLETE: Final = "complete"
VM_CHECK_INCOMPLETE: Final = "incomplete"
VM_CHECK_NOT_SUPPORTED: Final = "not_supported"

#: The one Datacenter of a standalone ESXi host.
_ESXI_DATACENTER_MOID: Final = "ha-datacenter"
_DATACENTER_MO_TYPE: Final = "Datacenter"
_DATASTORE_MO_TYPE: Final = "Datastore"

#: A path segment may not hold these: wildcards, brackets, a backslash or
#: control characters. ``.`` and ``..`` are refused separately.
_BAD_SEGMENT_RE: Final = re.compile(r"[\[\]\\*?\x00-\x1f\x7f]")
_BAD_PATTERN_RE: Final = re.compile(r"[/\\\x00-\x1f\x7f]")


def _path_problem(path: str) -> str | None:
    if not path:
        return None
    for segment in path.split("/"):
        if segment in ("", ".", "..") or _BAD_SEGMENT_RE.search(segment):
            return (
                f"path {path!r} is not allowed. Give a folder relative to the datastore root "
                "(empty for the root), e.g. 'iso' or 'images/esxi'. Not allowed: empty "
                "segments, '.' or '..', wildcards, brackets, backslashes, control characters"
            )
    return None


def _pattern_problem(pattern: Any) -> str | None:
    if pattern is None:
        return None
    if not isinstance(pattern, str) or not pattern or _BAD_PATTERN_RE.search(pattern):
        return (
            f"pattern {pattern!r} is not allowed. Give a file name pattern such as '*.iso' "
            "(no '/', backslash or control characters)"
        )
    return None


def _int_or_none(value: Any) -> int | None:
    """A size as ``int``. The ESXi SOAP answer has the number as text."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return None
    return None


def _str_or_none(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def _files_of(row: dict[str, Any]) -> list[dict[str, Any]]:
    """The ``file`` entries of a search result (a single SOAP entry is not a list)."""
    files = row.get("file")
    if isinstance(files, dict):
        files = [files]
    return [entry for entry in files or [] if isinstance(entry, dict)]


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


def _join(folder: str, name: str) -> str:
    return f"{folder}/{name}" if folder else name


async def _find_datastores(
    connector: VmwareRestConnector, target: Any, operator: Operator, flavor: str, name: str
) -> list[str]:
    """Moids of the datastores named *name* (on vCenter, a ``datastore-N`` moid also works).

    On ESXi the names come from one property read over the host's datastores.
    When that read is partial (a datastore left out, or a name the session may
    not read), the lookup raises :class:`IncompleteAnswerError` instead of
    answering ``not_found`` for a datastore that may well exist.
    """
    if flavor == HOST_FLAVOR_ESXI:
        datacenter = await read_object(
            connector,
            target,
            operator,
            mo_type=_DATACENTER_MO_TYPE,
            moid=_ESXI_DATACENTER_MOID,
            props=["datastore"],
        )
        moids = [moid for _type, moid in moref_values((datacenter or {}).get("datastore"))]
        names, complete = await read_many(
            connector, target, operator, mo_type=_DATASTORE_MO_TYPE, moids=moids, props=["name"]
        )
        if not complete:
            raise IncompleteAnswerError(
                "the ESXi host answered only part of its datastore list (a datastore left "
                "out, or a name this session may not read); refusing to say whether "
                f"datastore {name!r} exists on a partial answer"
            )
        return [moid for moid, props in names.items() if props.get("name") == name]
    listing = _unwrap_value(
        await _read_sub_op(
            connector, target, operator, _OP_LIST_DATASTORES, query={"filter.names": [name]}
        )
    )
    found = [
        entry["datastore"]
        for entry in (listing if isinstance(listing, list) else [])
        if isinstance(entry, dict)
        and entry.get("name") == name
        and isinstance(entry.get("datastore"), str)
    ]
    if not found and re.fullmatch(DATASTORE_MOID_PATTERN, name):
        return [name]
    return found


def _entries(ds: Datastore, path: str, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One row per file / folder directly in *path*."""
    out: list[dict[str, Any]] = []
    for row in rows:
        folder = ds.relative(str(row.get("folderPath") or ""))
        folder = path if folder is None else folder
        for entry in _files_of(row):
            name = str(entry.get("path") or "")
            folder_entry = is_folder(entry)
            out.append(
                {
                    "name": name,
                    "path": _join(folder, name),
                    "kind": "folder" if folder_entry else "file",
                    "file_type": _str_or_none(entry.get(VIM_TYPE_NAME_KEY)),
                    "size_bytes": None if folder_entry else _int_or_none(entry.get("fileSize")),
                    "modified": _str_or_none(entry.get("modification")),
                }
            )
    return out


def _folder_totals(
    ds: Datastore, path: str, rows: list[dict[str, Any]]
) -> dict[str, dict[str, Any]]:
    """Per child folder of *path*: total bytes, file count and latest change of all files below."""
    totals: dict[str, dict[str, Any]] = {}
    prefix = f"{path}/" if path else ""
    for row in rows:
        folder = ds.relative(str(row.get("folderPath") or ""))
        if folder is None or folder == path or not folder.startswith(prefix):
            continue
        child = folder[len(prefix) :].split("/", 1)[0]
        total = totals.setdefault(child, {"size_bytes": 0, "file_count": 0, "latest": None})
        for entry in _files_of(row):
            if is_folder(entry):
                continue
            total["size_bytes"] += _int_or_none(entry.get("fileSize")) or 0
            total["file_count"] += 1
            total["latest"] = _later(total["latest"], _str_or_none(entry.get("modification")))
    return totals


async def _add_usage(
    connector: VmwareRestConnector,
    target: Any,
    operator: Operator,
    ds: Datastore,
    path: str,
    entries: list[dict[str, Any]],
) -> str:
    """Fill folder sums and VM users into *entries*; return the ``vm_check`` value."""
    if any(entry["kind"] == "folder" for entry in entries):
        tree_rows = await search(
            connector, target, operator, ds, folder=path, pattern=None, recursive=True
        )
        totals = _folder_totals(ds, path, tree_rows or [])
        for entry in entries:
            if entry["kind"] == "folder":
                total = totals.get(entry["name"], {})
                entry["size_bytes"] = total.get("size_bytes", 0)
                entry["file_count"] = total.get("file_count", 0)
                entry["latest_modified"] = total.get("latest")
    if ds.type.upper() not in _SUPPORTED_TYPES:
        for entry in entries:
            entry["vms"] = None
        return VM_CHECK_NOT_SUPPORTED
    users, complete = await vm_users(
        connector, target, operator, ds, [entry["path"] for entry in entries]
    )
    for entry in entries:
        entry["vms"] = users.get(entry["path"], [])
    return VM_CHECK_COMPLETE if complete else VM_CHECK_INCOMPLETE


async def datastore_file_list_composite(
    *,
    operator: Operator,
    target: Any,
    params: dict[str, Any],
    connector: VmwareRestConnector,
) -> dict[str, Any]:
    """List the files in one datastore folder (read-only).

    Op-id: ``vmware.composite.datastore.file.list``. ``status`` is ``ok``,
    ``not_found`` (no such datastore or folder), ``ambiguous`` (two datastores
    share the name), ``invalid_request`` or ``unsupported_host_target``. The
    ``entries`` list turns into a result handle when it is large.
    """
    name = str(params.get("datastore") or "")
    path = str(params.get("path") or "").strip("/")
    pattern = params.get("pattern")
    include_usage = bool(params.get("include_usage", False))
    result: dict[str, Any] = {
        "status": STATUS_OK,
        "datastore": name,
        "datastore_moid": None,
        "datastore_type": None,
        "path": path,
        "datastore_path": None,
        "pattern": pattern,
        "count": 0,
        "entries": [],
        "vm_check": None,
        "guidance": None,
    }

    def refuse(status: str, guidance: str) -> dict[str, Any]:
        result.update(status=status, guidance=guidance)
        return result

    flavor, refusal = classify_host_target(target)
    if flavor is None or refusal is not None:
        result.update(refusal or {})
        return result
    problem = _path_problem(path) or _pattern_problem(pattern)
    if not name:
        problem = "datastore is required (the datastore name)"
    if problem is not None:
        return refuse(STATUS_INVALID_REQUEST, problem)

    moids = await _find_datastores(connector, target, operator, flavor, name)
    if len(moids) > 1:
        return refuse(
            STATUS_AMBIGUOUS,
            f"{len(moids)} datastores are named {name!r} ({', '.join(sorted(moids))}); "
            "pass one of these moids as datastore",
        )
    ds = await resolve_datastore(connector, target, operator, moids[0]) if moids else None
    if ds is None:
        return refuse(
            STATUS_NOT_FOUND,
            f"no datastore named {name!r}; vmware.composite.datastore.usage lists the names",
        )
    result.update(
        datastore=ds.name,
        datastore_moid=ds.moid,
        datastore_type=ds.type or None,
        datastore_path=ds.path_of(path),
    )

    rows = await search(
        connector, target, operator, ds, folder=path, pattern=pattern, recursive=False
    )
    if rows is None:
        return refuse(STATUS_NOT_FOUND, f"folder {ds.path_of(path)!r} does not exist")
    entries = _entries(ds, path, rows)
    if include_usage and entries:
        result["vm_check"] = await _add_usage(connector, target, operator, ds, path, entries)
    result.update(entries=entries, count=len(entries))
    return result
