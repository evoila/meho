# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Create a local content library: ``vmware.composite.content_library.create``.

The op (``caution``, waits for approval) creates a LOCAL library on one
datastore (``POST /content/local-library``) and reads it back. When a library
with the same name already exists, nothing is sent: one LOCAL library gives
``unchanged`` with its id, anything else is refused. vCenter allows two
libraries with one name, but MEHO finds libraries by name, so it does not make
a second one.

It calls vCenter REST on the connector session through the shared write seam
(:func:`._write._write_sub_op`), so it needs no catalog ingest. Add a file to
the library with ``content_library.item.upload`` (:mod:`._library_upload`);
delete it with ``content_library.delete`` (:mod:`._library_delete`). The
parameters, answers and AI guidance live in :mod:`._library_local_schemas`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final

import httpx

from meho_backplane.connectors.vmware_rest.composites._library import _resolve_datastore_id
from meho_backplane.connectors.vmware_rest.composites._library_checks import (
    OP_GET_LIBRARY,
    get_or_none,
    id_problem,
)
from meho_backplane.connectors.vmware_rest.composites._library_local_schemas import (
    CONTENT_LIBRARY_CREATE_LLM_INSTRUCTIONS,
    CONTENT_LIBRARY_CREATE_PARAMETER_SCHEMA,
    CONTENT_LIBRARY_CREATE_RESPONSE_SCHEMA,
)
from meho_backplane.connectors.vmware_rest.composites._write import (
    _OP_FIND_LIBRARY,
    _OP_LIST_DATASTORES,
    _find_content_library_ids,
    _parse_vsphere_error,
    _unwrap_value,
    _vsphere_fault_detail,
    _write_sub_op,
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
    "CONTENT_LIBRARY_CREATE_COMPOSITE",
    "content_library_create_composite",
]

OP_ID_CONTENT_LIBRARY_CREATE: Final = "vmware.composite.content_library.create"

_OP_CREATE_LOCAL_LIBRARY: Final = "POST:/content/local-library"

#: Every REST call of the create (reads and the one write). The reconcile test
#: checks them against the pinned vCenter 9.0 API file, and ``_governed_subops``
#: lists them for the grant set.
_SUB_OPS_CONTENT_LIBRARY_CREATE: Final[tuple[str, ...]] = (
    _OP_FIND_LIBRARY,
    OP_GET_LIBRARY,
    _OP_LIST_DATASTORES,
    _OP_CREATE_LOCAL_LIBRARY,
)

LOCAL: Final = "LOCAL"


def library_row(library_id: str, model: dict[str, Any] | None) -> dict[str, Any]:
    return {
        "id": library_id,
        "name": model.get("name") if model else None,
        "type": model.get("type") if model else None,
    }


async def _libraries_named(
    connector: VmwareRestConnector, target: Any, operator: Operator, name: str
) -> list[dict[str, Any]]:
    """``[{id, name, type}]`` of every library called *name* (exact match)."""
    ids = await _find_content_library_ids(
        connector, target, operator, op_id=_OP_FIND_LIBRARY, spec={"name": name}
    )
    rows: list[dict[str, Any]] = []
    for library_id in ids:
        if id_problem(library_id, "library id") is not None:
            rows.append({"id": library_id, "name": name, "type": None})
            continue
        model = await get_or_none(
            connector, target, operator, OP_GET_LIBRARY, {"libraryId": library_id}
        )
        rows.append(library_row(library_id, model))
    return rows


def refused_answer(envelope: dict[str, Any], exc: httpx.HTTPError) -> dict[str, Any] | None:
    """Map a vCenter HTTP 400 to ``refused``; ``None`` for any other fault."""
    status, error_type = _parse_vsphere_error(exc)
    if status != 400:
        return None
    return {
        **envelope,
        "status": "refused",
        "error_type": error_type,
        "guidance": f"vCenter refused the change, nothing changed: {_vsphere_fault_detail(exc)}",
    }


# ===========================================================================
# content_library.create
# ===========================================================================


def _existing_outcome(
    envelope: dict[str, Any], existing: list[dict[str, Any]]
) -> dict[str, Any] | None:
    """The answer when the name is taken, or ``None`` when it is free."""
    if not existing:
        return None
    if len(existing) == 1 and existing[0]["type"] == LOCAL:
        return {
            **envelope,
            "status": "unchanged",
            "library_id": existing[0]["id"],
            "existing": existing,
            "guidance": "A local library with this name already exists; nothing was sent.",
        }
    return {
        **envelope,
        "status": "precondition_failed",
        "existing": existing,
        "guidance": (
            f"{len(existing)} library(ies) already use this name and they are not one local "
            "library; nothing was sent. Pick another name."
        ),
    }


async def content_library_create_composite(
    *,
    operator: Operator,
    target: Any,
    params: dict[str, Any],
    connector: VmwareRestConnector,
) -> dict[str, Any] | OperationResult:
    """Create a LOCAL content library. Op-id: ``vmware.composite.content_library.create``."""
    name = params["name"]
    envelope: dict[str, Any] = {
        "name": name,
        "library_id": None,
        "datastore_id": None,
        "type": None,
        "existing": [],
        "error_type": None,
        "guidance": None,
    }
    existing = await _libraries_named(connector, target, operator, name)
    early = _existing_outcome(envelope, existing)
    if early is not None:
        return early
    datastore_id, ds_err = await _resolve_datastore_id(
        connector, target, operator, params["datastore"]
    )
    if ds_err is not None:
        return {**envelope, "status": ds_err["status"], "guidance": ds_err["issues"][0]["message"]}
    envelope["datastore_id"] = datastore_id
    body: dict[str, Any] = {
        "name": name,
        "storage_backings": [{"type": "DATASTORE", "datastore_id": datastore_id}],
    }
    if isinstance(params.get("description"), str):
        body["description"] = params["description"]
    try:
        gate, payload = await _write_sub_op(
            connector, target, operator, _OP_CREATE_LOCAL_LIBRARY, body
        )
    except httpx.HTTPError as exc:
        refused = refused_answer(envelope, exc)
        if refused is None:
            raise
        return refused
    if gate is not None:
        return gate
    library_id = _unwrap_value(payload)
    if id_problem(library_id, "library id") is not None:
        return {
            **envelope,
            "status": "not_verified",
            "guidance": f"vCenter answered with no library id ({library_id!r}). Look for the "
            "library by name before you try again.",
        }
    after = await get_or_none(
        connector, target, operator, OP_GET_LIBRARY, {"libraryId": library_id}
    )
    if after is None:
        return {
            **envelope,
            "status": "not_verified",
            "library_id": library_id,
            "guidance": "vCenter returned an id, but the library does not read back. Check "
            "vCenter before you try again.",
        }
    return {**envelope, "status": "created", "library_id": library_id, "type": after.get("type")}


async def _content_library_create_preview(ctx: PreviewContext) -> dict[str, Any] | None:
    """Show the approver the datastore and any library that already has the name."""
    if ctx.connector_instance is None:
        return None
    connector: VmwareRestConnector = ctx.connector_instance  # type: ignore[assignment]
    name = ctx.params.get("name")
    datastore = ctx.params.get("datastore")
    effect: dict[str, Any] = {"name": name, "datastore": datastore}
    try:
        existing = await _libraries_named(connector, ctx.target, ctx.operator, str(name))
        datastore_id, ds_err = await _resolve_datastore_id(
            connector, ctx.target, ctx.operator, str(datastore)
        )
    except Exception as exc:
        return {
            **effect,
            "summary": "MEHO could not read vCenter, so it cannot check the name and datastore.",
            "read_error": preview_failure_reason(exc),
        }
    if existing:
        summary = f"A library called {name!r} already exists; the call will create nothing."
    elif ds_err is not None:
        summary = f"Datastore {datastore!r} was not found once; the call will create nothing."
    else:
        summary = f"Create the local library {name!r} on datastore {datastore_id}."
    return {**effect, "summary": summary, "datastore_id": datastore_id, "existing": existing}


register_preview_builder(OP_ID_CONTENT_LIBRARY_CREATE, _content_library_create_preview)


#: The registration row, as ``_CompositeSpec`` keyword arguments.
CONTENT_LIBRARY_CREATE_COMPOSITE: Final[dict[str, Any]] = {
    "op_id": OP_ID_CONTENT_LIBRARY_CREATE,
    "handler": content_library_create_composite,
    "summary": "Create a local content library on one datastore.",
    "description": (
        "Creates a local content library on a datastore (found by name) and reads it "
        "back. If a local library with this name exists, nothing is sent (unchanged). "
        "Waits until a second person approves."
    ),
    "parameter_schema": CONTENT_LIBRARY_CREATE_PARAMETER_SCHEMA,
    "response_schema": CONTENT_LIBRARY_CREATE_RESPONSE_SCHEMA,
    "group_key": "content_library",
    "tags": ["composite", "write", "content-library", "local"],
    "safety_level": "caution",
    "requires_approval": True,
    "llm_instructions": CONTENT_LIBRARY_CREATE_LLM_INSTRUCTIONS,
}
