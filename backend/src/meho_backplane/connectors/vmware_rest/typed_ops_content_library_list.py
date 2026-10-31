# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Typed lists of vCenter content libraries and of the items of one library.

Two ``safe`` reads. Each calls the vCenter REST API on the connector's own
session, so it needs no spec ingest and works the same on vCenter 8 and 9:

* ``vmware.content_library.list`` -- every library: ``GET /content/library``
  for the ids, then ``GET /content/library/{id}`` and
  ``GET /content/library/item?library_id={id}`` (the item count) for each one.
* ``vmware.content_library.item.list`` -- the items of one library. A library
  name is found first with ``POST /content/library?action=find``. Then
  ``GET /content/library/item?library_id={id}`` lists the item ids, and
  ``GET /content/library/item/{id}`` reads each item.

The rows are flat, so a long list rides a result handle like any other list.
A library or item that is gone between the list and its read is left out. A
read that fails another way gives a row with ``read_note`` set, so one bad
library does not hide the others. Every id vCenter returns must be a UUID
before it goes into a URL path. No row holds a password.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, Final

import httpx
import structlog

from meho_backplane.connectors.base import ConnectorResourceNotFoundError
from meho_backplane.connectors.vmware_rest._content_library_schemas import (
    CONTENT_LIBRARY_GROUP_KEY,
    FIND_LIBRARY_CALL,
    text,
)
from meho_backplane.connectors.vmware_rest.composites.schemas import CONTENT_LIBRARY_ID_PATTERN
from meho_backplane.connectors.vmware_rest.typed_ops import VmwareTypedOp, _unwrap_value

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors.vmware_rest.connector import VmwareRestConnector
    from meho_backplane.connectors.vmware_rest.session import VsphereTargetLike

__all__ = [
    "CONTENT_LIBRARY_LIST_OPS",
    "ITEM_LIST_OP_ID",
    "LIST_OP_ID",
    "VENDOR_OP_IDS",
    "VMWARE_CONTENT_LIBRARY_ITEM_LIST_OP",
    "VMWARE_CONTENT_LIBRARY_LIST_OP",
    "content_library_item_list_impl",
    "content_library_list_impl",
]

_log = structlog.get_logger(__name__)

LIST_OP_ID: Final = "vmware.content_library.list"
ITEM_LIST_OP_ID: Final = "vmware.content_library.item.list"

_LIBRARIES_PATH: Final = "/content/library"
_LIBRARY_PATH: Final = "/content/library/{id}"
_ITEMS_PATH: Final = "/content/library/item"
#: The query parameter of the item list; part of its vendor call id.
_ITEMS_QUERY: Final = "library_id"
_ITEM_PATH: Final = "/content/library/item/{id}"

_PLACEHOLDER: Final = re.compile(r"\{[^}]*\}")


def _call(method: str, path: str) -> str:
    """The ``METHOD:/path`` call id of *path*, with every placeholder written ``{}``."""
    return f"{method}:{_PLACEHOLDER.sub('{}', path)}"


#: The vendor calls the two lists send, built from the paths the handlers use,
#: so the reconcile test checks the paths that run.
VENDOR_OP_IDS: Final[frozenset[str]] = frozenset(
    {
        FIND_LIBRARY_CALL,
        _call("GET", _LIBRARIES_PATH),
        _call("GET", _LIBRARY_PATH),
        _call("GET", f"{_ITEMS_PATH}?{_ITEMS_QUERY}"),
        _call("GET", _ITEM_PATH),
    }
)


def _id_problem(value: Any, label: str) -> str | None:
    # Lazy import: the composites package imports the typed ops module.
    from meho_backplane.connectors.vmware_rest.composites._library_checks import id_problem

    return id_problem(value, label)


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _ids(value: Any) -> list[str]:
    return [entry for entry in value if isinstance(entry, str)] if isinstance(value, list) else []


async def _get(
    connector: VmwareRestConnector,
    operator: Operator,
    target: VsphereTargetLike,
    path: str,
    *,
    query: dict[str, Any] | None = None,
) -> Any:
    """One ``GET`` on the connector session; ``None`` on HTTP 404."""
    mounted = await connector.mount_op_path(target, path, operator)
    try:
        payload = await connector._get_json(target, mounted, operator=operator, params=query)
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 404:
            return None
        raise
    return _unwrap_value(payload)


def _failure(exc: httpx.HTTPError) -> str:
    if isinstance(exc, httpx.HTTPStatusError):
        return f"not read: HTTP {exc.response.status_code}"
    return f"not read: {type(exc).__name__}"


# ---------------------------------------------------------------------------
# vmware.content_library.list
# ---------------------------------------------------------------------------


def library_list_row(
    library_id: str, model: Any, *, item_count: int | None, read_note: str | None = None
) -> dict[str, Any]:
    """One flat library row. No password is copied."""
    data = _mapping(model)
    datastores = [
        backing.get("datastore_id")
        for backing in data.get("storage_backings") or []
        if isinstance(backing, Mapping) and isinstance(backing.get("datastore_id"), str)
    ]
    publish = _mapping(data.get("publish_info"))
    return {
        "library_id": library_id,
        "name": data.get("name"),
        "type": data.get("type"),
        "datastore_ids": datastores,
        "published": publish.get("published") if publish else None,
        "item_count": item_count,
        "last_sync_time": data.get("last_sync_time"),
        "last_modified_time": data.get("last_modified_time"),
        "read_note": read_note,
    }


async def _library_row(
    connector: VmwareRestConnector,
    operator: Operator,
    target: VsphereTargetLike,
    library_id: str,
) -> dict[str, Any] | None:
    """The row of one library; ``None`` when it is gone."""
    problem = _id_problem(library_id, "library id")
    if problem is not None:
        return library_list_row(library_id, None, item_count=None, read_note=problem)
    try:
        model = await _get(connector, operator, target, _LIBRARY_PATH.format(id=library_id))
        if model is None:
            return None
        items = await _get(
            connector, operator, target, _ITEMS_PATH, query={_ITEMS_QUERY: library_id}
        )
    except httpx.HTTPError as exc:
        return library_list_row(library_id, None, item_count=None, read_note=_failure(exc))
    count = len(items) if isinstance(items, list) else None
    return library_list_row(library_id, model, item_count=count)


async def content_library_list_impl(
    connector: VmwareRestConnector,
    operator: Operator,
    target: VsphereTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Implementation of ``vmware.content_library.list``."""
    wanted = params.get("library_type")
    ids = _ids(await _get(connector, operator, target, _LIBRARIES_PATH))
    rows: list[dict[str, Any]] = []
    for library_id in ids:
        row = await _library_row(connector, operator, target, library_id)
        if row is None:
            continue
        if wanted is not None and row["read_note"] is None and row["type"] != wanted:
            continue
        rows.append(row)
    _log.info("vmware_content_library_list_read", target=target.name, library_count=len(rows))
    return {"library_count": len(rows), "libraries": rows}


# ---------------------------------------------------------------------------
# vmware.content_library.item.list
# ---------------------------------------------------------------------------


def item_list_row(item_id: str, model: Any, *, read_note: str | None = None) -> dict[str, Any]:
    """One flat item row."""
    data = _mapping(model)
    return {
        "library_item_id": item_id,
        "name": data.get("name"),
        "type": data.get("type"),
        "size": data.get("size"),
        "content_version": data.get("content_version"),
        "cached": data.get("cached"),
        "last_modified_time": data.get("last_modified_time"),
        "read_note": read_note,
    }


async def _library_id(
    connector: VmwareRestConnector,
    operator: Operator,
    target: VsphereTargetLike,
    params: dict[str, Any],
) -> str:
    """The library id from ``library_id``, or the one library ``library_name`` names."""
    from meho_backplane.connectors.vmware_rest.composites._library_checks import (
        resolve_library_id,
    )

    library_id, refusal = await resolve_library_id(connector, target, operator, dict(params))
    if refusal is not None:
        raise ValueError(f"{ITEM_LIST_OP_ID}: {refusal[1]}")
    if library_id is None:
        name = str(params.get("library_name"))
        raise ConnectorResourceNotFoundError([name], f"vCenter has no library named {name!r}")
    return library_id


async def _item_row(
    connector: VmwareRestConnector,
    operator: Operator,
    target: VsphereTargetLike,
    item_id: str,
) -> dict[str, Any] | None:
    problem = _id_problem(item_id, "item id")
    if problem is not None:
        return item_list_row(item_id, None, read_note=problem)
    try:
        model = await _get(connector, operator, target, _ITEM_PATH.format(id=item_id))
    except httpx.HTTPError as exc:
        return item_list_row(item_id, None, read_note=_failure(exc))
    return None if model is None else item_list_row(item_id, model)


async def content_library_item_list_impl(
    connector: VmwareRestConnector,
    operator: Operator,
    target: VsphereTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Implementation of ``vmware.content_library.item.list``."""
    library_id = await _library_id(connector, operator, target, params)
    ids = await _get(connector, operator, target, _ITEMS_PATH, query={_ITEMS_QUERY: library_id})
    if ids is None:
        raise ConnectorResourceNotFoundError(
            [library_id], f"vCenter has no content library {library_id}"
        )
    rows: list[dict[str, Any]] = []
    for item_id in _ids(ids):
        row = await _item_row(connector, operator, target, item_id)
        if row is not None:
            rows.append(row)
    _log.info("vmware_content_library_item_list_read", target=target.name, item_count=len(rows))
    return {"library_id": library_id, "item_count": len(rows), "items": rows}


# ---------------------------------------------------------------------------
# Op metadata
# ---------------------------------------------------------------------------

_TAGS: Final[tuple[str, ...]] = ("read-only", "vmware", "vcenter", "content-library")
_READ_NOTE: Final[dict[str, Any]] = text(
    "Why the row is incomplete (the read failed or the id is not a UUID). Null when all was read."
)

_LIBRARY_ROW_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "library_id": {"type": "string", "description": "The library id (a UUID)."},
        "name": text("The library name."),
        "type": text("LOCAL or SUBSCRIBED."),
        "datastore_ids": {
            "type": "array",
            "items": {"type": "string"},
            "description": "The datastores that hold the library files, for example datastore-11.",
        },
        "published": {
            "type": ["boolean", "null"],
            "description": "True when a local library is published. Null for a subscribed one.",
        },
        "item_count": {
            "type": ["integer", "null"],
            "description": "How many items the library holds. Null when not read.",
        },
        "last_sync_time": text("When a subscribed library last synced (ISO 8601)."),
        "last_modified_time": text("When the library last changed (ISO 8601)."),
        "read_note": _READ_NOTE,
    },
    "required": ["library_id", "name", "type", "item_count", "read_note"],
}

VMWARE_CONTENT_LIBRARY_LIST_OP = VmwareTypedOp(
    op_id=LIST_OP_ID,
    handler_attr="content_library_list",
    summary="List all content libraries: name, type, storage, item count and last sync.",
    description=(
        "Lists every content library of the vCenter: name, type (local or subscribed), the "
        "datastores that hold it, whether it is published, the number of items and the last "
        "sync. A long list comes back as a link to the stored result. Read-only."
    ),
    parameter_schema={
        "type": "object",
        "properties": {
            "library_type": {
                "type": "string",
                "enum": ["LOCAL", "SUBSCRIBED"],
                "description": "Return only local or only subscribed libraries.",
            }
        },
        "additionalProperties": False,
        "examples": [{}],
    },
    response_schema={
        "type": "object",
        "properties": {
            "library_count": {"type": "integer", "description": "How many rows came back."},
            "libraries": {
                "type": "array",
                "description": "One flat row per library.",
                "items": _LIBRARY_ROW_SCHEMA,
            },
        },
        "required": ["library_count", "libraries"],
    },
    group_key=CONTENT_LIBRARY_GROUP_KEY,
    tags=_TAGS,
    safety_level="safe",
    requires_approval=False,
    llm_instructions={
        "when_to_call": (
            "Call to see which content libraries a vCenter has, before you pick one to "
            "deploy from, to capture a VM into, or to export from."
        ),
        "parameter_hints": {"library_type": "Optional: LOCAL or SUBSCRIBED."},
        "output_shape": (
            "{library_count, libraries: [{library_id, name, type, datastore_ids, published, "
            "item_count, last_sync_time, last_modified_time, read_note}]}. A long list is "
            "stored behind a result handle; read it with result_query."
        ),
        "next_step": (
            "For the items of one library, call vmware.content_library.item.list with its "
            "library_id. For the full settings of one library, call "
            "vmware.content_library.get."
        ),
    },
)

_ITEM_ROW_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "library_item_id": {"type": "string", "description": "The item id (a UUID)."},
        "name": text("The item name."),
        "type": text("The item type, for example ovf, vm-template or iso."),
        "size": {
            "type": ["integer", "null"],
            "description": "The size of the item files in bytes.",
        },
        "content_version": text("The content version; it changes when the files change."),
        "cached": {
            "type": ["boolean", "null"],
            "description": "True when the files are on this vCenter (always for a local item).",
        },
        "last_modified_time": text("When the item last changed (ISO 8601)."),
        "read_note": _READ_NOTE,
    },
    "required": ["library_item_id", "name", "type", "read_note"],
}

VMWARE_CONTENT_LIBRARY_ITEM_LIST_OP = VmwareTypedOp(
    op_id=ITEM_LIST_OP_ID,
    handler_attr="content_library_item_list",
    summary="List the items of one content library: name, type, size, version, last change.",
    description=(
        "Lists the items of one content library, found by id or name: name, type (OVF, VM "
        "template, ISO or other), size in bytes, content version and when it last changed. A "
        "long list comes back as a link to the stored result. Read-only."
    ),
    parameter_schema={
        "type": "object",
        "properties": {
            "library_id": {
                "type": "string",
                "pattern": CONTENT_LIBRARY_ID_PATTERN,
                "description": "The id of the library (a UUID).",
            },
            "library_name": {
                "type": "string",
                "minLength": 1,
                "maxLength": 255,
                "description": (
                    "The library name. A name that matches more than one library is "
                    "refused; then pass library_id."
                ),
            },
        },
        "anyOf": [{"required": ["library_id"]}, {"required": ["library_name"]}],
        "additionalProperties": False,
        "examples": [{"library_name": "<library-name>"}],
    },
    response_schema={
        "type": "object",
        "properties": {
            "library_id": {"type": "string", "description": "The library id (a UUID)."},
            "item_count": {"type": "integer", "description": "How many rows came back."},
            "items": {
                "type": "array",
                "description": "One flat row per item.",
                "items": _ITEM_ROW_SCHEMA,
            },
        },
        "required": ["library_id", "item_count", "items"],
    },
    group_key=CONTENT_LIBRARY_GROUP_KEY,
    tags=_TAGS,
    safety_level="safe",
    requires_approval=False,
    llm_instructions={
        "when_to_call": (
            "Call to see what one content library holds: its OVF templates, VM templates, "
            "ISO files and other items."
        ),
        "parameter_hints": {
            "library_id": "Pass the id when you have it.",
            "library_name": "Or pass the exact library name.",
        },
        "output_shape": (
            "{library_id, item_count, items: [{library_item_id, name, type, size, "
            "content_version, cached, last_modified_time, read_note}]}. size is in bytes. A "
            "long list is stored behind a result handle; read it with result_query."
        ),
        "next_step": (
            "For the files of one item, call vmware.content_library.item.files.list. To copy "
            "an item's files out of vCenter, call vmware.composite.content_library.item.export."
        ),
    },
)

#: The two lists, in the order :data:`.typed_ops.VMWARE_TYPED_OPS` lists them.
CONTENT_LIBRARY_LIST_OPS: Final[tuple[VmwareTypedOp, ...]] = (
    VMWARE_CONTENT_LIBRARY_LIST_OP,
    VMWARE_CONTENT_LIBRARY_ITEM_LIST_OP,
)
