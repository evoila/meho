# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Copy the files of a content library item out of vCenter; export a VM as OVF files.

* ``vmware.composite.content_library.item.export`` (``dangerous``, waits for
  approval) copies every file of one library item (OVF, ISO or other files)
  to a destination URL that takes HTTP ``PUT``.
* ``vmware.composite.vm.export_ovf`` (``dangerous``, waits for approval)
  captures a VM into a temporary OVF item of a local library
  (:func:`._library_capture.capture_ovf`), copies the item's files the same
  way, and deletes the temporary item. With ``library_item_id`` it copies an
  OVF item that already exists and captures nothing.

The vCenter side, per run:

1. ``POST /content/library/item/download-session`` with a ``Client-Token``
   header (a new UUID, given back in the answer) opens a download session;
2. ``GET .../download-session/{id}/file`` lists the files;
3. ``POST .../file?action=prepare`` asks for each file, and
   ``GET .../file?file_name=`` is read until the file is ``PREPARED`` and has
   its download URL (``ERROR`` or the time limit stops the run);
4. the copy helper (:func:`.._transfer.copy_files`) streams each file to the
   destination and calls ``POST .../download-session/{id}?action=keep-alive``
   once a minute;
5. ``DELETE .../download-session/{id}`` ends the session, also after an
   error; for a VM export, ``DELETE /content/library/item/{id}`` then removes
   the temporary item.

The work runs inside the call, so the call can take hours; the docs tell
callers to use ``async: true``. The answer lists each file with its size,
SHA-256 and destination path, and what the clean-up did. It never says
``exported`` before the clean-up ran.

The download URL vCenter gives is used only for its path: MEHO sends the
vCenter session only to the target's own host and port, whatever host the
URL names. No answer, error or log carries a download URL.

When MEHO dies during a run, the clean-up cannot run. Then the download
session ends after vCenter's own timeout; a temporary item keeps its
``meho-transfer-ovf-export-`` name, and the next VM export into the same
library deletes such items that are older than a day (a younger one may be
the item of a run that is still going); a ``.meho-partial`` file at the
destination is removed or replaced by the next run.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any, Final

import httpx

from meho_backplane.connectors.vmware_rest._transfer import (
    Destination,
    SourceFile,
    TransferError,
    copy_files,
    meho_temp_name,
)
from meho_backplane.connectors.vmware_rest.composite_issues import issue
from meho_backplane.connectors.vmware_rest.composites._library_capture import (
    _SUB_OPS_VM_CAPTURE_TO_LIBRARY as CAPTURE_CALLS,
)
from meho_backplane.connectors.vmware_rest.composites._library_capture import (
    OP_GET_VM,
    capture_ovf,
    failed_answer,
    local_library,
    ovf_messages,
)
from meho_backplane.connectors.vmware_rest.composites._library_checks import (
    OP_GET_LIBRARY_ITEM,
    get_or_none,
    id_problem,
)
from meho_backplane.connectors.vmware_rest.composites._library_export_schemas import (
    EXPORT_OVF_LLM_INSTRUCTIONS,
    EXPORT_OVF_PARAMETER_SCHEMA,
    EXPORT_RESPONSE_SCHEMA,
    ITEM_EXPORT_LLM_INSTRUCTIONS,
    ITEM_EXPORT_PARAMETER_SCHEMA,
)
from meho_backplane.connectors.vmware_rest.composites._library_export_session import (
    OP_DELETE_ITEM,
    OP_LIST_ITEMS,
    OP_SESSION_CREATE,
    OP_SESSION_DELETE,
    OP_SESSION_FILE,
    OP_SESSION_FILES,
    OP_SESSION_KEEP_ALIVE,
    OP_SESSION_PREPARE,
    TEMP_LABEL,
    DownloadSession,
    ExportRun,
    SessionError,
    leftover_remover,
    vcenter_url,
)
from meho_backplane.operations._preview import (
    PreviewContext,
    preview_failure_reason,
    register_preview_builder,
)

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors import OperationResult
    from meho_backplane.connectors.vmware_rest.connector import VmwareRestConnector

__all__ = [
    "EXPORT_OVF_COMPOSITE",
    "ITEM_EXPORT_COMPOSITE",
    "content_library_item_export_composite",
    "vm_export_ovf_composite",
]

OP_ID_ITEM_EXPORT: Final = "vmware.composite.content_library.item.export"
OP_ID_EXPORT_OVF: Final = "vmware.composite.vm.export_ovf"
_OVF_TYPE: Final = "ovf"

#: Every REST call of the two exports (reads and writes). The reconcile test
#: checks them against the pinned vCenter 8.0 and 9.0 API files, and
#: ``_governed_subops`` lists them for the grant set.
_SUB_OPS_CONTENT_LIBRARY_ITEM_EXPORT: Final[tuple[str, ...]] = (
    OP_GET_LIBRARY_ITEM,
    OP_SESSION_CREATE,
    OP_SESSION_FILES,
    OP_SESSION_PREPARE,
    OP_SESSION_FILE,
    OP_SESSION_KEEP_ALIVE,
    OP_SESSION_DELETE,
)
_SUB_OPS_VM_EXPORT_OVF: Final[tuple[str, ...]] = tuple(
    dict.fromkeys(
        (
            *CAPTURE_CALLS,
            *_SUB_OPS_CONTENT_LIBRARY_ITEM_EXPORT,
            OP_LIST_ITEMS,
            OP_DELETE_ITEM,
        )
    )
)


def _answer(**fields: Any) -> dict[str, Any]:
    return {
        "status": None,
        "library_item_id": None,
        "vm": None,
        "temporary_item": False,
        "client_tokens": {"capture": None, "download_session": None},
        "files": [],
        "leftovers_removed": [],
        "cleanup": None,
        "error_type": None,
        "messages": [],
        "guidance": None,
        **fields,
    }


async def _sources(run: ExportRun, session: DownloadSession) -> list[SourceFile]:
    names = await session.file_names()
    if not names:
        raise SessionError("the library item has no files")
    for name in names:
        await session.prepare(name)
    urls = await session.download_urls(names)
    return [
        SourceFile(name=name, url=await vcenter_url(run.connector, run.target, urls[name]))
        for name in names
    ]


async def _copy(
    run: ExportRun,
    item_id: str,
    params: dict[str, Any],
    answer: dict[str, Any],
    *,
    library_id: str | None = None,
) -> dict[str, Any]:
    """Open the session, copy every file, clean up; the final answer."""
    session = DownloadSession(run.connector, run.target, run.operator, item_id)
    run.session = session
    answer["library_item_id"] = item_id
    failure: str | None = None
    try:
        await session.open(str(uuid.uuid4()))
        answer["client_tokens"]["download_session"] = session.client_token
        sources = await _sources(run, session)
        result = await copy_files(
            sources,
            Destination(
                base_url=params["destination_url"], token_ref=params["destination_token_ref"]
            ),
            operator=run.operator,
            target=run.target,
            source_client=await run.connector._http_client(run.target),
            source_headers=await run.connector.auth_headers(run.target, run.operator),
            keep_alive=session.keep_alive,
            cleanup=run.cleanup,
            remove_leftovers=leftover_remover(run, library_id) if library_id else None,
            overwrite=bool(params.get("overwrite", False)),
        )
        answer["files"] = result.as_dict()["files"]
        answer["leftovers_removed"] = result.leftovers_removed
    except TransferError as exc:
        answer["files"] = [vars(row) for row in exc.files]
        failure = str(exc)
    except SessionError as exc:
        failure = str(exc)
    except httpx.HTTPError as exc:
        failure = failed_answer({}, exc, "A vCenter call")["guidance"]
    finally:
        await run.cleanup()
    answer["cleanup"] = dict(run.cleanup_state)
    return _final(answer, failure)


def _final(answer: dict[str, Any], failure: str | None) -> dict[str, Any]:
    note = answer["cleanup"].get("note")
    if failure is not None:
        copied = sum(1 for row in answer["files"] if row.get("status") != "failed")
        text = f"The export stopped: {failure}. {copied} file(s) were copied before it."
        if note:
            text += f" Clean-up: {note}."
        return {
            **answer,
            "status": "failed",
            "guidance": text + " Fix the cause and call again; the next run replaces the "
            "partial files.",
            "issues": [issue("connector", "error", f"the export stopped: {failure}")],
        }
    partial = [row["name"] for row in answer["files"] if row["status"] == "partial"]
    text = f"All {len(answer['files'])} file(s) were copied."
    if partial:
        text += (
            f" The destination cannot rename files, so {', '.join(partial)} keep the ending "
            ".meho-partial."
        )
    if note:
        text += f" Clean-up: {note}."
    return {**answer, "status": "exported", "guidance": text}


async def _item(
    connector: VmwareRestConnector, target: Any, operator: Operator, item_id: Any
) -> tuple[dict[str, Any] | None, str | None]:
    """``(item model, None)``, or ``(None, problem)``."""
    problem = id_problem(item_id, "library_item_id")
    if problem is not None:
        return None, problem
    model = await get_or_none(
        connector, target, operator, OP_GET_LIBRARY_ITEM, {"libraryItemId": item_id}
    )
    return model, None


async def content_library_item_export_composite(
    *,
    operator: Operator,
    target: Any,
    params: dict[str, Any],
    connector: VmwareRestConnector,
) -> dict[str, Any] | OperationResult:
    """Copy every file of one library item to a destination URL (``item.export``)."""
    model, problem = await _item(connector, target, operator, params.get("library_item_id"))
    if problem is not None:
        return _answer(status="invalid_request", guidance=problem)
    if model is None:
        return _answer(status="not_found", guidance="No such library item; nothing was copied.")
    run = ExportRun(connector, target, operator)
    return await _copy(run, str(params["library_item_id"]), params, _answer())


async def _capture_temporary(
    run: ExportRun, params: dict[str, Any], answer: dict[str, Any]
) -> tuple[OperationResult | None, dict[str, Any] | None, str | None]:
    """Capture the VM into a temporary item: ``(gate, answer to return, library id)``."""
    vm = await get_or_none(run.connector, run.target, run.operator, OP_GET_VM, {"vm": params["vm"]})
    if vm is None:
        return None, {**answer, "status": "not_found", "guidance": "No such VM; nothing ran."}, None
    library_id, refusal = await local_library(run.connector, run.target, run.operator, params)
    if refusal is not None or library_id is None:
        return None, {**answer, **(refusal or {})}, None
    try:
        gate, token, result = await capture_ovf(
            run.connector,
            run.target,
            run.operator,
            vm=params["vm"],
            library_id=library_id,
            item_name=meho_temp_name(TEMP_LABEL),
            description="Temporary copy for an OVF export by MEHO; MEHO deletes it.",
        )
    except httpx.HTTPError as exc:
        return None, failed_answer(answer, exc, "The capture"), None
    answer["client_tokens"]["capture"] = token
    if gate is not None:
        return gate, None, None
    item_id = result.get("ovf_library_item_id") if result.get("succeeded") else None
    if id_problem(item_id, "item id") is not None:
        messages = ovf_messages(result)
        return (
            None,
            {
                **answer,
                "status": "failed",
                "messages": messages,
                "guidance": "vCenter could not capture the VM: "
                + ("; ".join(messages) or "no reason given"),
                "issues": [issue("connector", "error", "the OVF capture did not succeed")],
            },
            None,
        )
    run.temp_item_id = str(item_id)
    answer["temporary_item"] = True
    return None, None, library_id


async def vm_export_ovf_composite(
    *,
    operator: Operator,
    target: Any,
    params: dict[str, Any],
    connector: VmwareRestConnector,
) -> dict[str, Any] | OperationResult:
    """Export a VM as OVF files to a destination URL (``vm.export_ovf``)."""
    answer = _answer(vm=params.get("vm"))
    run = ExportRun(connector, target, operator)
    if params.get("library_item_id") is not None:
        model, problem = await _item(connector, target, operator, params["library_item_id"])
        if problem is not None:
            return {**answer, "status": "invalid_request", "guidance": problem}
        if model is None:
            return {**answer, "status": "not_found", "guidance": "No such library item."}
        if model.get("type") != _OVF_TYPE:
            return {
                **answer,
                "status": "precondition_failed",
                "guidance": f"The item is of type {model.get('type')!r}, not an OVF template. "
                "Use content_library.item.export for other items.",
            }
        return await _copy(run, str(params["library_item_id"]), params, answer)
    try:
        gate, stop, library_id = await _capture_temporary(run, params, answer)
    except BaseException:
        await run.cleanup()
        raise
    if gate is not None or stop is not None:
        return gate or stop  # type: ignore[return-value]
    return await _copy(run, str(run.temp_item_id), params, answer, library_id=library_id)


async def _export_preview(ctx: PreviewContext) -> dict[str, Any] | None:
    """Show the approver what is copied, and where to."""
    if ctx.connector_instance is None:
        return None
    connector: VmwareRestConnector = ctx.connector_instance  # type: ignore[assignment]
    params = dict(ctx.params)
    effect: dict[str, Any] = {
        "destination_url": params.get("destination_url"),
        "destination_token_ref": params.get("destination_token_ref"),
        "overwrite": bool(params.get("overwrite", False)),
    }
    try:
        if params.get("library_item_id") is not None:
            model, _ = await _item(connector, ctx.target, ctx.operator, params["library_item_id"])
            what = (
                f"the files of item {model.get('name')!r} ({model.get('type')}, "
                f"{model.get('size')} bytes)"
                if model
                else "nothing: the item does not exist"
            )
        else:
            vm = await get_or_none(
                connector, ctx.target, ctx.operator, OP_GET_VM, {"vm": params.get("vm")}
            )
            what = (
                f"VM {params.get('vm')!r} ({(vm or {}).get('name')}) as OVF files, through a "
                "temporary library item that MEHO deletes at the end"
                if vm
                else "nothing: the VM does not exist"
            )
    except Exception as exc:
        return {
            **effect,
            "summary": "MEHO could not read vCenter, so it cannot show what is copied.",
            "read_error": preview_failure_reason(exc),
        }
    return {**effect, "summary": f"MEHO copies {what} to {params.get('destination_url')}."}


register_preview_builder(OP_ID_ITEM_EXPORT, _export_preview)
register_preview_builder(OP_ID_EXPORT_OVF, _export_preview)


#: The registration rows, as ``_CompositeSpec`` keyword arguments. The
#: ``token`` tag keeps the vendor request and answer bodies (which carry the
#: download URLs) out of the flight recorder.
ITEM_EXPORT_COMPOSITE: Final[dict[str, Any]] = {
    "op_id": OP_ID_ITEM_EXPORT,
    "handler": content_library_item_export_composite,
    "summary": "Copy the files of a content library item to a URL that accepts HTTP PUT.",
    "description": (
        "Copies every file of one library item (OVF, ISO or other) out of vCenter to a web "
        "server or object store that takes HTTP PUT; the token comes from Vault. Returns each "
        "file with size and SHA-256. Use async: true. Waits until a second person approves."
    ),
    "parameter_schema": ITEM_EXPORT_PARAMETER_SCHEMA,
    "response_schema": EXPORT_RESPONSE_SCHEMA,
    "group_key": "content_library",
    "tags": ["composite", "write", "content-library", "export", "download", "token"],
    "safety_level": "dangerous",
    "requires_approval": True,
    "llm_instructions": ITEM_EXPORT_LLM_INSTRUCTIONS,
}

EXPORT_OVF_COMPOSITE: Final[dict[str, Any]] = {
    "op_id": OP_ID_EXPORT_OVF,
    "handler": vm_export_ovf_composite,
    "summary": "Export a VM as OVF files to a URL that accepts HTTP PUT.",
    "description": (
        "Captures a VM into a temporary OVF item of a local library (or uses an OVF item), "
        "copies its files to a web server or object store that takes HTTP PUT, then deletes "
        "the temporary item. Returns each file with size and SHA-256. Use async: true."
    ),
    "parameter_schema": EXPORT_OVF_PARAMETER_SCHEMA,
    "response_schema": EXPORT_RESPONSE_SCHEMA,
    "group_key": "content_library",
    "tags": ["composite", "write", "vm", "ovf", "export", "content-library", "token"],
    "safety_level": "dangerous",
    "requires_approval": True,
    "llm_instructions": EXPORT_OVF_LLM_INSTRUCTIONS,
}
