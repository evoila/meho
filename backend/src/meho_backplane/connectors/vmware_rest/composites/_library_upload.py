# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Add a file to a local content library from a URL.

``vmware.composite.content_library.item.upload`` (``caution``, waits for
approval): vCenter downloads one file (an ISO, OVA or OVF) from an http(s) URL
into an item of a LOCAL library. MEHO sends no file bytes. The steps:

1. create the empty item (``POST /content/library/item``);
2. open an update session (``POST /content/library/item/update-session``);
3. add the file with ``source_type=PULL`` and the URL;
4. complete the session; vCenter keeps downloading after this call;
5. read the session until it leaves ``ACTIVE`` or ``wait_seconds`` runs out.

An item with the same name that already has content gives ``unchanged``. An
empty one (left by an earlier failed try) is filled again, so a retry needs no
clean-up first. The URL must be http or https and must not hold a user name or
password. The approval previews show it without its query part, so the
token of a signed URL stays out of the preview: the parent operation's own,
and the add-file step's when that step waits for approval by itself (the
generic step preview would echo the full URL). vCenter itself would also take
``file`` and ``ds`` URLs; MEHO does not send them.

Steps 2 to 4 run after the item exists, so each saves where its resume
continues (#4217): a step that waits for approval stores the library, item and
session ids, the file name and its own name. Approving it continues at that
step and opens no second session (vCenter allows one active session per item).
A park at step 1 saves nothing: its resume runs from the start and finds no
item yet.

It calls vCenter REST on the connector session through the shared write seam
(:func:`._write._write_sub_op`), so it needs no catalog ingest. The
parameters, answers and AI guidance live in :mod:`._library_local_schemas`.
"""

from __future__ import annotations

import asyncio
import posixpath
import time
from typing import TYPE_CHECKING, Any, Final
from urllib.parse import unquote, urlsplit, urlunsplit

import httpx

from meho_backplane.connectors.vmware_rest.composites._library_checks import (
    OP_GET_LIBRARY,
    OP_GET_LIBRARY_ITEM,
    get_or_none,
    id_problem,
    int_or_none,
    resolve_library_id,
)
from meho_backplane.connectors.vmware_rest.composites._library_local import (
    LOCAL,
    library_row,
    refused_answer,
)
from meho_backplane.connectors.vmware_rest.composites._library_local_schemas import (
    CONTENT_LIBRARY_ITEM_UPLOAD_LLM_INSTRUCTIONS,
    CONTENT_LIBRARY_ITEM_UPLOAD_PARAMETER_SCHEMA,
    CONTENT_LIBRARY_ITEM_UPLOAD_RESPONSE_SCHEMA,
    DEFAULT_WAIT_SECONDS,
)
from meho_backplane.connectors.vmware_rest.composites._spec import CompositeSpec
from meho_backplane.connectors.vmware_rest.composites._write import (
    _OP_FIND_LIBRARY,
    _OP_FIND_LIBRARY_ITEM,
    _find_content_library_ids,
    _parse_vsphere_error,
    _read_sub_op,
    _refuse_resume_without_state,
    _unwrap_value,
    _vsphere_fault_detail,
    _write_sub_op,
)
from meho_backplane.operations._preview import (
    PreviewContext,
    preview_failure_reason,
    register_preview_builder,
)
from meho_backplane.operations.composite import composite_resume_state

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors import OperationResult
    from meho_backplane.connectors.vmware_rest.connector import VmwareRestConnector

__all__ = [
    "COMPOSITES",
    "content_library_item_upload_composite",
]

OP_ID_CONTENT_LIBRARY_ITEM_UPLOAD: Final = "vmware.composite.content_library.item.upload"

_OP_CREATE_LIBRARY_ITEM: Final = "POST:/content/library/item"
_OP_CREATE_UPDATE_SESSION: Final = "POST:/content/library/item/update-session"
_OP_ADD_SESSION_FILE: Final = "POST:/content/library/item/update-session/{updateSessionId}/file"
_OP_COMPLETE_SESSION: Final = (
    "POST:/content/library/item/update-session/{updateSessionId}?action=complete"
)
_OP_GET_SESSION: Final = "GET:/content/library/item/update-session/{updateSessionId}"

#: Every REST call of the upload (reads and writes). The reconcile test checks
#: them against the pinned vCenter 9.0 API file, and ``_governed_subops`` lists
#: them for the grant set.
_SUB_OPS_CONTENT_LIBRARY_ITEM_UPLOAD: Final[tuple[str, ...]] = (
    _OP_FIND_LIBRARY,
    OP_GET_LIBRARY,
    _OP_FIND_LIBRARY_ITEM,
    OP_GET_LIBRARY_ITEM,
    _OP_CREATE_LIBRARY_ITEM,
    _OP_CREATE_UPDATE_SESSION,
    _OP_ADD_SESSION_FILE,
    _OP_COMPLETE_SESSION,
    _OP_GET_SESSION,
)

#: Seconds between two reads of the update session. A test may lower it.
_POLL_INTERVAL_SECONDS: float = 5.0

#: The steps after the item exists. Each is a governed write that saves where
#: its resume continues (#4217).
_STEP_SESSION: Final = "session"
_STEP_ADD_FILE: Final = "add_file"
_STEP_COMPLETE: Final = "complete"
_PULL_STEPS: Final[tuple[str, ...]] = (_STEP_SESSION, _STEP_ADD_FILE, _STEP_COMPLETE)
_PULL_WRITE_OPS: Final[frozenset[str]] = frozenset(
    {_OP_CREATE_UPDATE_SESSION, _OP_ADD_SESSION_FILE, _OP_COMPLETE_SESSION}
)


def _source_problem(url: Any) -> str | None:
    """Why *url* is not an http(s) URL vCenter may download from, or ``None``."""
    if not isinstance(url, str):
        return "source_url must be a string"
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return "source_url must be an http or https URL with a host"
    if parts.username is not None or parts.password is not None:
        return "source_url must not hold a user name or password"
    return None


def _shown_url(url: Any) -> Any:
    """*url* without its query, fragment, user name and password, for a preview.

    A signed download URL carries its token in the query. The full URL stays
    in the parameters, so vCenter still downloads from it.
    """
    if not isinstance(url, str):
        return url
    parts = urlsplit(url)
    return urlunsplit(parts._replace(netloc=parts.netloc.rpartition("@")[2], query="", fragment=""))


_SOURCE_URL_NOTE: Final = "The URL is shown without its query part, user name and password."


def _shown_url_fields(url: Any) -> dict[str, Any]:
    """The ``source_url`` a preview shows, and a note when a part was cut."""
    shown = _shown_url(url)
    fields: dict[str, Any] = {"source_url": shown}
    if shown != url:
        fields["source_url_note"] = _SOURCE_URL_NOTE
    return fields


def _file_name(params: dict[str, Any]) -> str:
    """``file_name``, or the last part of the URL path."""
    given = params.get("file_name")
    if isinstance(given, str) and given:
        return given
    return posixpath.basename(unquote(urlsplit(params["source_url"]).path))


async def _upload_target(
    connector: VmwareRestConnector, target: Any, operator: Operator, params: dict[str, Any]
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """``(library row, None)`` for a LOCAL library, or ``(None, answer)`` to return."""
    library_id, refusal = await resolve_library_id(connector, target, operator, params)
    if refusal is not None:
        return None, {"status": refusal[0], "guidance": refusal[1]}
    model = (
        await get_or_none(connector, target, operator, OP_GET_LIBRARY, {"libraryId": library_id})
        if library_id is not None
        else None
    )
    if library_id is None or model is None:
        return None, {"status": "library_not_found", "guidance": "No such library; nothing sent."}
    row = library_row(library_id, model)
    if row["type"] != LOCAL:
        return None, {
            "status": "precondition_failed",
            "library_id": library_id,
            "guidance": f"Library {library_id!r} is {row['type']!r}, not LOCAL. Only a local "
            "library takes new items; nothing was sent.",
        }
    return row, None


async def _existing_item(
    connector: VmwareRestConnector,
    target: Any,
    operator: Operator,
    *,
    library_id: str,
    item_name: str,
) -> tuple[dict[str, Any] | None, str | None]:
    """``(item model, None)`` for the one item called *item_name*, ``(None, None)``
    when there is none, or ``(None, problem)``."""
    ids = await _find_content_library_ids(
        connector,
        target,
        operator,
        op_id=_OP_FIND_LIBRARY_ITEM,
        spec={"library_id": library_id, "name": item_name},
    )
    if not ids:
        return None, None
    if len(ids) > 1 or id_problem(ids[0], "item id") is not None:
        return None, f"item_name {item_name!r} matched {ids!r}; expected one item id"
    model = await get_or_none(
        connector, target, operator, OP_GET_LIBRARY_ITEM, {"libraryItemId": ids[0]}
    )
    return ({**model, "id": ids[0]} if model is not None else None), None


def _item_info(item_id: str, model: dict[str, Any]) -> dict[str, Any]:
    """The ``item`` answer field. vCenter's item GET leaves ``id`` out, so it is passed in."""
    return {
        "id": item_id,
        "name": model.get("name"),
        "type": model.get("type"),
        "size_bytes": int_or_none(model.get("size")),
        "cached": model.get("cached"),
    }


async def _create_item(
    connector: VmwareRestConnector,
    target: Any,
    operator: Operator,
    params: dict[str, Any],
    library_id: str,
) -> tuple[OperationResult | None, str | None]:
    """Create the empty item; ``(gate, None)`` or ``(None, item id)``."""
    body: dict[str, Any] = {"library_id": library_id, "name": params["item_name"]}
    for key, field in (("item_type", "type"), ("description", "description")):
        if isinstance(params.get(key), str):
            body[field] = params[key]
    gate, payload = await _write_sub_op(connector, target, operator, _OP_CREATE_LIBRARY_ITEM, body)
    if gate is not None:
        return gate, None
    item_id = _unwrap_value(payload)
    if id_problem(item_id, "item id") is not None:
        raise RuntimeError(f"vCenter answered the item create with no item id ({item_id!r})")
    return None, str(item_id)


async def _start_pull(
    connector: VmwareRestConnector,
    target: Any,
    operator: Operator,
    *,
    envelope: dict[str, Any],
    file_name: str,
    params: dict[str, Any],
    step: str = _STEP_SESSION,
) -> OperationResult | None:
    """Open a session, add the PULL file and complete it, from *step* on.

    Fills ``envelope["session_id"]``. Returns the gate of a step that waits
    for approval or is denied, else ``None``. The item exists by now, so each
    step saves the ids, the file name and its own name as resume state
    (#4217): approving it continues here at that step and opens no second
    session.
    """

    async def write(
        op_id: str, sub_params: dict[str, Any], step: str
    ) -> tuple[OperationResult | None, Any]:
        state = {
            "library_id": envelope["library_id"],
            "item_id": envelope["item_id"],
            "session_id": envelope["session_id"],
            "file_name": file_name,
            "step": step,
        }
        return await _write_sub_op(
            connector,
            target,
            operator,
            op_id,
            sub_params,
            resume_state=state,
            after_main_effect=True,
        )

    if step == _STEP_SESSION:
        gate, payload = await write(
            _OP_CREATE_UPDATE_SESSION, {"library_item_id": envelope["item_id"]}, _STEP_SESSION
        )
        if gate is not None:
            return gate
        session_id = _unwrap_value(payload)
        if not isinstance(session_id, str) or not session_id:
            raise RuntimeError(f"vCenter answered with no update session id ({session_id!r})")
        envelope["session_id"] = session_id
    session_id = str(envelope["session_id"])
    if step != _STEP_COMPLETE:
        endpoint: dict[str, Any] = {"uri": params["source_url"]}
        if isinstance(params.get("ssl_certificate_thumbprint"), str):
            endpoint["ssl_certificate_thumbprint"] = params["ssl_certificate_thumbprint"]
        add = {
            "updateSessionId": session_id,
            "name": file_name,
            "source_type": "PULL",
            "source_endpoint": endpoint,
        }
        gate, _ = await write(_OP_ADD_SESSION_FILE, add, _STEP_ADD_FILE)
        if gate is not None:
            return gate
    gate, _ = await write(_OP_COMPLETE_SESSION, {"updateSessionId": session_id}, _STEP_COMPLETE)
    return gate


async def _wait_for_session(
    connector: VmwareRestConnector,
    target: Any,
    operator: Operator,
    session_id: str,
    wait_seconds: float,
) -> dict[str, Any]:
    """Read the session until it leaves ``ACTIVE`` or *wait_seconds* run out."""
    deadline = time.monotonic() + wait_seconds
    while True:
        session = _unwrap_value(
            await _read_sub_op(
                connector, target, operator, _OP_GET_SESSION, {"updateSessionId": session_id}
            )
        )
        session = session if isinstance(session, dict) else {}
        if session.get("state") != "ACTIVE" or time.monotonic() >= deadline:
            return session
        await asyncio.sleep(min(_POLL_INTERVAL_SECONDS, max(deadline - time.monotonic(), 0)))


async def _finish(
    connector: VmwareRestConnector,
    target: Any,
    operator: Operator,
    envelope: dict[str, Any],
    session: dict[str, Any],
) -> dict[str, Any]:
    """Map the last session state to the answer, reading the item back when done."""
    state = session.get("state")
    envelope = {**envelope, "session_state": state}
    if state == "ACTIVE":
        return {
            **envelope,
            "status": "in_progress",
            "guidance": "vCenter is still downloading the file. Call again later with the "
            "same parameters: it answers unchanged once the item has content.",
        }
    if state != "DONE":
        message = session.get("error_message")
        if isinstance(message, dict):
            message = message.get("default_message")
        return {
            **envelope,
            "status": "upload_failed",
            "guidance": f"The download ended in state {state!r}: "
            f"{message or 'no reason given'}. The item stays empty; fix the cause and call "
            "again (it reuses the empty item), or delete the item with "
            "content_library.item.delete.",
        }
    model = await get_or_none(
        connector, target, operator, OP_GET_LIBRARY_ITEM, {"libraryItemId": envelope["item_id"]}
    )
    if model is None:
        return {
            **envelope,
            "status": "not_verified",
            "guidance": "The download is done, but the item does not read back. Check vCenter.",
        }
    return {**envelope, "status": "uploaded", "item": _item_info(envelope["item_id"], model)}


async def _plan(
    connector: VmwareRestConnector,
    target: Any,
    operator: Operator,
    params: dict[str, Any],
    envelope: dict[str, Any],
) -> tuple[str, dict[str, Any] | None, dict[str, Any] | None]:
    """Read-only checks: ``(file name, existing empty item or None, None)``, or
    ``("", None, answer)`` when the call stops here. Fills ``library_id``."""
    problem = _source_problem(params.get("source_url"))
    file_name = _file_name(params) if problem is None else ""
    if problem is None and not file_name:
        problem = "the URL path has no file name; pass file_name"
    if problem is not None:
        return "", None, {**envelope, "status": "invalid_request", "guidance": problem}
    library, answer = await _upload_target(connector, target, operator, params)
    if answer is not None or library is None:
        return "", None, {**envelope, **(answer or {})}
    envelope["library_id"] = library["id"]
    item, item_problem = await _existing_item(
        connector, target, operator, library_id=library["id"], item_name=params["item_name"]
    )
    if item_problem is not None:
        return "", None, {**envelope, "status": "invalid_request", "guidance": item_problem}
    if item is not None and (int_or_none(item.get("size")) or 0) > 0:
        return (
            "",
            None,
            {
                **envelope,
                "status": "unchanged",
                "item_id": item["id"],
                "item": _item_info(item["id"], item),
                "guidance": "The item already exists and has content; nothing was sent.",
            },
        )
    return file_name, item, None


def _resumed_upload_state() -> dict[str, Any]:
    """The state a waiting pull step saved, during its resume (#4217); empty otherwise.

    It needs the library and item ids, the file name and one of the steps; a
    step after the session open also needs the session id. Anything else
    counts as missing state and is refused before any read or write.
    """
    saved = composite_resume_state(OP_ID_CONTENT_LIBRARY_ITEM_UPLOAD)
    if saved is None or saved.get("step") not in _PULL_STEPS:
        return {}
    needed = ["library_id", "item_id", "file_name"]
    if saved["step"] != _STEP_SESSION:
        needed.append("session_id")
    if not all(isinstance(saved.get(key), str) and saved[key] for key in needed):
        return {}
    return saved


async def content_library_item_upload_composite(
    *,
    operator: Operator,
    target: Any,
    params: dict[str, Any],
    connector: VmwareRestConnector,
) -> dict[str, Any] | OperationResult:
    """vCenter downloads one file into a local library item (``content_library.item.upload``).

    An approved pull step continues at that step with the ids its park saved
    (#4217); it reads nothing again and opens no second session. Such a
    resume without saved state is refused before any call.
    """
    envelope: dict[str, Any] = {
        "library_id": None,
        "item_id": None,
        "item_name": params.get("item_name"),
        "session_id": None,
        "session_state": None,
        "item": None,
        "error_type": None,
        "guidance": None,
    }
    saved = _resumed_upload_state()
    refusal = _refuse_resume_without_state(
        composite_op_id=OP_ID_CONTENT_LIBRARY_ITEM_UPLOAD,
        write_ops=_PULL_WRITE_OPS,
        has_state=bool(saved),
        nothing_done="no file was added",
        remediation=(
            "Nothing ran. Call the operation again with the same parameters (it reuses the "
            "empty item), or delete the item named in the approval request with "
            "content_library.item.delete."
        ),
    )
    if refusal is not None:
        return refusal
    if saved:
        envelope.update(
            library_id=saved["library_id"],
            item_id=saved["item_id"],
            session_id=saved.get("session_id"),
        )
        return await _pull_and_wait(
            connector, target, operator, params, envelope, saved["file_name"], saved["step"]
        )
    file_name, item, answer = await _plan(connector, target, operator, params, envelope)
    if answer is not None:
        return answer
    item_id = item["id"] if item is not None else None
    if item_id is None:
        try:
            gate, item_id = await _create_item(
                connector, target, operator, params, envelope["library_id"]
            )
        except httpx.HTTPError as exc:
            return _upload_fault(envelope, exc)
        if gate is not None:
            return gate
    envelope["item_id"] = item_id
    return await _pull_and_wait(
        connector, target, operator, params, envelope, file_name, _STEP_SESSION
    )


async def _pull_and_wait(
    connector: VmwareRestConnector,
    target: Any,
    operator: Operator,
    params: dict[str, Any],
    envelope: dict[str, Any],
    file_name: str,
    step: str,
) -> dict[str, Any] | OperationResult:
    """Run the pull steps from *step* on, then wait for the download; the answer."""
    try:
        gate = await _start_pull(
            connector,
            target,
            operator,
            envelope=envelope,
            file_name=file_name,
            params=params,
            step=step,
        )
    except httpx.HTTPError as exc:
        return _upload_fault(envelope, exc)
    if gate is not None:
        return gate
    session = await _wait_for_session(
        connector,
        target,
        operator,
        str(envelope["session_id"]),
        float(params.get("wait_seconds", DEFAULT_WAIT_SECONDS)),
    )
    return await _finish(connector, target, operator, envelope, session)


def _upload_fault(envelope: dict[str, Any], exc: httpx.HTTPError) -> dict[str, Any]:
    """Map a fault of a write step: ``refused`` before the item exists, else
    ``in_progress`` (busy) or ``upload_failed``."""
    if envelope["item_id"] is None:
        refused = refused_answer(envelope, exc)
        if refused is None:
            raise exc
        return refused
    _status, error_type = _parse_vsphere_error(exc)
    if error_type == "RESOURCE_BUSY":
        return {
            **envelope,
            "status": "in_progress",
            "error_type": error_type,
            "guidance": "Another update of this item is still running. Call again later.",
        }
    return {
        **envelope,
        "status": "upload_failed",
        "error_type": error_type,
        "guidance": f"Starting the download failed: {_vsphere_fault_detail(exc)}. The item "
        "stays empty; call again to retry (it reuses the empty item), or delete it with "
        "content_library.item.delete.",
    }


async def _content_library_item_upload_preview(ctx: PreviewContext) -> dict[str, Any] | None:
    """Show the approver the library, the item name, the URL, and any item of that name.

    The URL is shown without its query part, user name and password, so the
    token of a signed URL does not land in the approval row.
    """
    if ctx.connector_instance is None:
        return None
    connector: VmwareRestConnector = ctx.connector_instance  # type: ignore[assignment]
    params = dict(ctx.params)
    url_fields = _shown_url_fields(params.get("source_url"))
    shown_url = url_fields["source_url"]
    effect: dict[str, Any] = {
        "item_name": params.get("item_name"),
        **url_fields,
        "item_type": params.get("item_type"),
    }
    problem = _source_problem(params.get("source_url"))
    if problem is not None:
        return {**effect, "summary": f"The call will be refused: {problem}."}
    effect["file_name"] = _file_name(params)
    try:
        library, answer = await _upload_target(connector, ctx.target, ctx.operator, params)
        item = None
        if library is not None:
            item, _ = await _existing_item(
                connector,
                ctx.target,
                ctx.operator,
                library_id=library["id"],
                item_name=str(params.get("item_name")),
            )
    except Exception as exc:
        return {
            **effect,
            "summary": "MEHO could not read vCenter, so it cannot check the library.",
            "read_error": preview_failure_reason(exc),
        }
    if library is None:
        return {
            **effect,
            "summary": f"The call will change nothing: {(answer or {}).get('guidance')}",
        }
    summary = (
        f"vCenter downloads {shown_url} into a new item of library {library['name']!r}."
        if item is None
        else f"Item {params.get('item_name')!r} already exists in library {library['name']!r}."
    )
    existing = _item_info(item["id"], item) if item is not None else None
    return {**effect, "summary": summary, "library": library, "existing_item": existing}


async def _add_session_file_preview(ctx: PreviewContext) -> dict[str, Any] | None:
    """Show the approver the add-file step with the URL's query part cut.

    The step waits for approval on its own when a rule parks it (#4217).
    Without a builder it would get the generic preview, which echoes the
    full ``source_endpoint.uri``; the shared redaction does not know a
    signed URL's token. The full URL stays in the step's parameters, so
    vCenter still downloads from it.
    """
    params = ctx.params
    endpoint = params.get("source_endpoint")
    endpoint = endpoint if isinstance(endpoint, dict) else {}
    url_fields = _shown_url_fields(endpoint.get("uri"))
    effect: dict[str, Any] = {
        "summary": (
            f"vCenter downloads {url_fields['source_url']} as file "
            f"{params.get('name')!r} into the open update session."
        ),
        "file_name": params.get("name"),
        **url_fields,
        "session_id": params.get("updateSessionId"),
    }
    if "ssl_certificate_thumbprint" in endpoint:
        effect["ssl_certificate_thumbprint"] = endpoint["ssl_certificate_thumbprint"]
    return effect


register_preview_builder(_OP_ADD_SESSION_FILE, _add_session_file_preview)


#: The registration row.
COMPOSITES: Final[tuple[CompositeSpec, ...]] = (
    CompositeSpec(
        op_id=OP_ID_CONTENT_LIBRARY_ITEM_UPLOAD,
        handler=content_library_item_upload_composite,
        summary="Add an ISO, OVA or OVF file to a local content library from a URL.",
        description=(
            "vCenter downloads one file from an http(s) URL into a new item of a local "
            "content library; MEHO sends no file bytes. Waits until the download is done or "
            "wait_seconds runs out. An item of that name with content gives unchanged. "
            "Waits until a second person approves."
        ),
        parameter_schema=CONTENT_LIBRARY_ITEM_UPLOAD_PARAMETER_SCHEMA,
        response_schema=CONTENT_LIBRARY_ITEM_UPLOAD_RESPONSE_SCHEMA,
        group_key="content_library",
        tags=["composite", "write", "content-library", "local", "iso", "ovf"],
        safety_level="caution",
        requires_approval=True,
        llm_instructions=CONTENT_LIBRARY_ITEM_UPLOAD_LLM_INSTRUCTIONS,
        preview=_content_library_item_upload_preview,
    ),
)
