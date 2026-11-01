# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Update the vSAN HCL database and the release catalog (#4446).

Op-id: ``vmware.composite.vsan.hcl.update``.

vSAN checks the hardware of each host against its HCL database (the
hardware compatibility list) and plans updates with its release catalog. Both
go stale. This change replaces them, in up to two steps, in this order:

1. ``hcl`` -- the HCL database:

   - ``source: web`` -- ``VsanVcUpdateHclDbFromWeb``: vCenter downloads it,
     from the vendor's standard location or from the ``url`` given (for
     example a web server inside a network without internet access);
   - ``source: datastore_file`` -- ``VsanVcUploadHclDb``: MEHO reads the file
     from a datastore and sends it. The vendor call wants gzipped JSON as
     base64; MEHO gzips a plain JSON file first.

2. ``release_catalog`` -- ``VsanVcUploadReleaseDb``: MEHO reads the release
   catalog (JSON) from a datastore and sends it.

MEHO reads a datastore file through vCenter's datastore file access
(``GET /folder/<path>?dcPath=<datacenter path>&dsName=<datastore>``, vSphere
Web Services SDK programming guide, "URL Syntax for HTTP Access") with the
cookie of a vCenter SOAP login. A file is at most 64 MiB.

Each step is checked by the sub-call gate before it is sent. When a step
fails, the answer lists the steps that ran, the step that failed with the
vendor's error, and the steps that did not run. MEHO does not undo a step
that ran.
"""

from __future__ import annotations

import base64
import gzip
import json
from typing import TYPE_CHECKING, Any, Final
from urllib.parse import quote

from meho_backplane.connectors import OperationResult
from meho_backplane.connectors._shared.cache_key import target_cache_key
from meho_backplane.connectors.vmware_rest.composite_issues import issue
from meho_backplane.connectors.vmware_rest.composites._datastore_browse import (
    DATACENTER_MO_TYPE,
    path_problem,
    resolve_datastore,
)
from meho_backplane.connectors.vmware_rest.composites._teardown_reads import (
    moid_problem,
    read_object,
)
from meho_backplane.connectors.vmware_rest.composites._vsan_common import (
    VSAN_UPDATE_HCL_DB_FROM_WEB,
    VSAN_UPLOAD_HCL_DB,
    VSAN_UPLOAD_RELEASE_DB,
    vendor_error,
    vsan_write,
)
from meho_backplane.connectors.vmware_rest.composites.schemas import DATASTORE_MOID_PATTERN
from meho_backplane.connectors.vmware_rest.vim_body import unwrap_vim_value

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors.vmware_rest.connector import VmwareRestConnector
    from meho_backplane.operations._preview import PreviewContext

__all__ = [
    "HCL_UPDATE_OP_ID",
    "MAX_FILE_BYTES",
    "DatastoreFileError",
    "hcl_update_preview",
    "read_datastore_file",
    "vsan_hcl_update_composite",
]

HCL_UPDATE_OP_ID: Final = "vmware.composite.vsan.hcl.update"

STEP_HCL: Final = "hcl"
STEP_RELEASE_CATALOG: Final = "release_catalog"
SOURCE_WEB: Final = "web"
SOURCE_FILE: Final = "datastore_file"

#: The largest datastore file MEHO reads (the HCL database is a few MiB).
MAX_FILE_BYTES: Final = 64 * 1024 * 1024
_GZIP_MAGIC: Final = b"\x1f\x8b"
_SOAP_SESSION_COOKIE: Final = "vmware_soap_session"
_FOLDER_PATH: Final = "/folder/"
_MAX_PARENT_HOPS: Final = 12


class DatastoreFileError(RuntimeError):
    """A datastore file MEHO cannot read or use (missing, too large, not JSON)."""


async def _datacenter_path(
    connector: VmwareRestConnector, target: Any, operator: Operator, datacenter: str
) -> str:
    """The inventory path of *datacenter*, the ``dcPath`` of the file access.

    The datacenter name, with the names of the folders above it (the root
    folder is not part of it), for example ``Europe/dc-1``.
    """
    names: list[str] = []
    mo_type, moid = DATACENTER_MO_TYPE, datacenter
    for _ in range(_MAX_PARENT_HOPS):
        props = await read_object(
            connector, target, operator, mo_type=mo_type, moid=moid, props=["name", "parent"]
        )
        if props is None:
            break
        parent = unwrap_vim_value(props.get("parent"))
        if not isinstance(parent, dict) or not isinstance(parent.get("value"), str):
            break  # the root folder has no parent and is not part of the path
        name = props.get("name")
        if isinstance(name, str):
            names.append(name)
        mo_type, moid = str(parent.get("type") or "Folder"), parent["value"]
    if not names:
        raise DatastoreFileError(f"could not read the path of datacenter {datacenter!r}")
    return "/".join(reversed(names))


async def read_datastore_file(
    connector: VmwareRestConnector,
    target: Any,
    operator: Operator,
    datastore: str,
    path: str,
) -> tuple[bytes, str]:
    """Read one datastore file through vCenter; return ``(content, "[datastore] path")``.

    Logs in again once when vCenter answers 401 or 403 (an expired SOAP
    session). Raises :class:`DatastoreFileError` for a missing datastore or
    file, or a file larger than :data:`MAX_FILE_BYTES`.
    """
    ds = await resolve_datastore(connector, target, operator, datastore)
    if ds is None:
        raise DatastoreFileError(f"datastore {datastore!r} not found")
    dc_path = await _datacenter_path(connector, target, operator, ds.datacenter)
    client = await connector._http_client(target)
    extensions = connector._request_extensions(target)
    url = _FOLDER_PATH + quote(path, safe="/")
    query = {"dcPath": dc_path, "dsName": ds.name}
    for attempt in range(2):
        cookie, _ = await connector._ensure_vsan_session(target, operator)
        headers = {"Cookie": f"{_SOAP_SESSION_COOKIE}={cookie}"}
        async with client.stream(
            "GET", url, params=query, headers=headers, extensions=extensions
        ) as resp:
            if resp.status_code in (401, 403) and attempt == 0:
                connector._vsan_sessions.pop(target_cache_key(target), None)
                continue
            if resp.status_code == 404:
                raise DatastoreFileError(f"file {ds.path_of(path)!r} not found")
            resp.raise_for_status()
            content = bytearray()
            async for chunk in resp.aiter_bytes():
                content.extend(chunk)
                if len(content) > MAX_FILE_BYTES:
                    raise DatastoreFileError(
                        f"file {ds.path_of(path)!r} is larger than {MAX_FILE_BYTES} bytes"
                    )
            return bytes(content), ds.path_of(path)
    raise DatastoreFileError(f"vCenter refused the login to read {ds.path_of(path)!r}")


def hcl_db_argument(content: bytes) -> str:
    """The ``db`` argument of ``VsanVcUploadHclDb``: gzipped JSON as base64.

    A file that is gzipped already is sent as it is; a plain file must be
    JSON and is gzipped first.
    """
    if not content.startswith(_GZIP_MAGIC):
        _require_json(content, "HCL database")
        content = gzip.compress(content)
    return base64.b64encode(content).decode("ascii")


def release_db_argument(content: bytes) -> str:
    """The ``db`` argument of ``VsanVcUploadReleaseDb``: the JSON text (unzipped)."""
    if content.startswith(_GZIP_MAGIC):
        content = gzip.decompress(content)
    _require_json(content, "release catalog")
    return content.decode("utf-8")


def _require_json(content: bytes, what: str) -> None:
    try:
        json.loads(content)
    except (UnicodeDecodeError, ValueError) as exc:
        raise DatastoreFileError(f"the {what} file is not JSON") from exc


def _file_problem(value: Any, label: str) -> str | None:
    if not isinstance(value, dict):
        return f"{label} needs datastore and path"
    return moid_problem(value.get("datastore"), DATASTORE_MOID_PATTERN, f"{label}.datastore") or (
        path_problem(value.get("path"))
    )


def _plan(params: dict[str, Any]) -> tuple[list[tuple[str, dict[str, Any]]], str | None]:
    """The steps to run, in order, or the reason the input is wrong."""
    steps: list[tuple[str, dict[str, Any]]] = []
    hcl = params.get("hcl")
    if hcl is not None:
        source = hcl.get("source") if isinstance(hcl, dict) else None
        if source == SOURCE_FILE:
            problem = _file_problem(hcl, "hcl")
            if problem is not None:
                return [], problem
        elif source != SOURCE_WEB:
            return [], f"hcl.source must be {SOURCE_WEB} or {SOURCE_FILE}"
        steps.append((STEP_HCL, hcl))
    catalog = params.get("release_catalog")
    if catalog is not None:
        problem = _file_problem(catalog, "release_catalog")
        if problem is not None:
            return [], problem
        steps.append((STEP_RELEASE_CATALOG, catalog))
    if not steps:
        return [], "give hcl, release_catalog or both"
    return steps, None


async def _run_step(
    connector: VmwareRestConnector,
    target: Any,
    operator: Operator,
    step: str,
    spec: dict[str, Any],
) -> tuple[OperationResult | None, dict[str, Any]]:
    """Run one step; return the gate result (when it held the call) and the step row."""
    row: dict[str, Any] = {"step": step, "source": SOURCE_WEB, "url": None, "file": None}
    if step == STEP_HCL and spec.get("source") == SOURCE_WEB:
        url = spec.get("url")
        row["url"] = url
        gate, _ = await vsan_write(
            connector,
            target,
            operator,
            method=VSAN_UPDATE_HCL_DB_FROM_WEB,
            body={"url": url} if url else {},
            params={"step": step, "url": url or "vendor default"},
        )
        return gate, row
    content, file_label = await read_datastore_file(
        connector, target, operator, str(spec["datastore"]), str(spec["path"])
    )
    row.update(source=SOURCE_FILE, file=file_label, size_bytes=len(content))
    if step == STEP_HCL:
        method, db = VSAN_UPLOAD_HCL_DB, hcl_db_argument(content)
    else:
        method, db = VSAN_UPLOAD_RELEASE_DB, release_db_argument(content)
    gate, _ = await vsan_write(
        connector,
        target,
        operator,
        method=method,
        body={"db": db},
        params={"step": step, "file": file_label},
    )
    return gate, row


async def vsan_hcl_update_composite(
    *,
    operator: Operator,
    target: Any,
    params: dict[str, Any],
    connector: VmwareRestConnector,
) -> dict[str, Any] | OperationResult:
    """Replace the vSAN HCL database, the release catalog, or both.

    Op-id: ``vmware.composite.vsan.hcl.update``. ``updated`` when every step
    ran. When a step fails: ``failed`` (nothing ran) or ``partly_done`` (an
    earlier step ran), with an error issue, ``failed_step`` (the step and the
    vendor's fault type and text) and ``steps_not_run``.
    """
    steps, problem = _plan(params)
    if problem is not None:
        return {
            "status": "invalid_request",
            "steps_ran": [],
            "failed_step": None,
            "steps_not_run": [],
            "issues": [issue("validation", "error", problem)],
            "guidance": "Fix the input and call again. Nothing was changed.",
        }
    ran: list[dict[str, Any]] = []
    for index, (step, spec) in enumerate(steps):
        try:
            gate, row = await _run_step(connector, target, operator, step, spec)
        except Exception as exc:
            not_run = [name for name, _ in steps[index + 1 :]]
            error = vendor_error(exc)
            return {
                "status": "partly_done" if ran else "failed",
                "steps_ran": ran,
                "failed_step": {"step": step, **error},
                "steps_not_run": not_run,
                "issues": [issue("connector", "error", f"step {step} failed: {error['message']}")],
                "guidance": (
                    "MEHO did not undo the steps that ran. Fix the cause, then call again with "
                    "only the steps that did not run."
                ),
            }
        if gate is not None:
            return gate
        ran.append(row)
    return {
        "status": "updated",
        "steps_ran": ran,
        "failed_step": None,
        "steps_not_run": [],
        "guidance": (
            "Done. To see the hardware checks with the new data, call vmware.host.vsan_health."
        ),
    }


async def hcl_update_preview(ctx: PreviewContext) -> dict[str, Any] | None:
    """For the approver: the steps and their sources (no file is read here)."""
    steps, problem = _plan(ctx.params)
    if problem is not None:
        return {"summary": "The input is wrong; the call will change nothing.", "problem": problem}
    shown = []
    for step, spec in steps:
        if spec.get("source") == SOURCE_WEB:
            shown.append({"step": step, "from": spec.get("url") or "vendor default location"})
        else:
            shown.append({"step": step, "from": f"{spec.get('datastore')}: {spec.get('path')}"})
    return {
        "summary": "Replace the vSAN data: " + ", ".join(step for step, _ in steps) + ".",
        "steps": shown,
        "blast_radius": {
            "affected": "The hardware checks and the update plans of every vSAN cluster of "
            "this vCenter use the new data.",
            "cannot_undo": "The old data is replaced; to go back, load an older file.",
        },
    }
