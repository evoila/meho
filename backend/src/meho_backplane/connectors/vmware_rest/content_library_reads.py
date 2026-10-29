# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Typed reads of vCenter content libraries.

Six ``safe`` reads. Their metadata is in :mod:`.typed_ops_content_library`.
Each one calls the vCenter REST API on the connector's own session, so it
needs no spec ingest, and works the same on vCenter 8 and 9:

* ``vmware.content_library.get`` -- one library: ``GET /content/library/{id}``,
  then the local-library or subscribed-library read for the type's settings,
  then the item list for the item count. A library name is found first with
  ``POST /content/library?action=find``.
* ``vmware.content_library.item.get`` -- one item: ``GET /content/library/item/{id}``.
* ``vmware.content_library.item.files.list`` -- the files of one item and
  where each one is stored: ``GET .../file`` and ``GET .../storage``.
* ``vmware.content_library.item.ovf_options`` -- what an OVF item needs before
  a deploy: ``POST /vcenter/ovf/library-item/{id}?action=filter``. The call is a
  ``POST`` but changes nothing.
* ``vmware.content_library.subscribed.probe`` -- check a subscription URL:
  ``POST /content/subscribed-library?action=probe``. Also a ``POST`` that
  changes nothing.
* ``vmware.supervisor.content_library.list`` -- the libraries the Supervisors
  use: ``GET /vcenter/namespace-management/lifecycle/content/libraries``. This
  call exists only from vCenter 9.0 on.

Every library and item id must be a UUID before it goes into a URL path. No
result holds a password: the rows copy named fields only, and the OVF
property value of a ``password`` property is never returned.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, Final

import httpx
import structlog

from meho_backplane.connectors.base import ConnectorResourceNotFoundError
from meho_backplane.connectors.vmware_rest._version_gate import require_vcenter_version

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors.vmware_rest.connector import VmwareRestConnector
    from meho_backplane.connectors.vmware_rest.session import VsphereTargetLike

__all__ = [
    "GET_OP_ID",
    "ITEM_FILES_LIST_OP_ID",
    "ITEM_GET_OP_ID",
    "OVF_OPTIONS_OP_ID",
    "PROBE_OP_ID",
    "SUPERVISOR_LIST_OP_ID",
    "SUPERVISOR_MINIMUM_VERSION",
    "VENDOR_OP_IDS",
    "content_library_get_impl",
    "item_files_list_impl",
    "item_get_impl",
    "item_ovf_options_impl",
    "subscribed_probe_impl",
    "supervisor_content_library_list_impl",
]

_log = structlog.get_logger(__name__)

GET_OP_ID: Final = "vmware.content_library.get"
ITEM_GET_OP_ID: Final = "vmware.content_library.item.get"
ITEM_FILES_LIST_OP_ID: Final = "vmware.content_library.item.files.list"
OVF_OPTIONS_OP_ID: Final = "vmware.content_library.item.ovf_options"
PROBE_OP_ID: Final = "vmware.content_library.subscribed.probe"
SUPERVISOR_LIST_OP_ID: Final = "vmware.supervisor.content_library.list"
SUPERVISOR_MINIMUM_VERSION: Final = "9.0"

_LIBRARY_PATH: Final = "/content/library/{id}"
_LOCAL_LIBRARY_PATH: Final = "/content/local-library/{id}"
_SUBSCRIBED_LIBRARY_PATH: Final = "/content/subscribed-library/{id}"
_ITEMS_PATH: Final = "/content/library/item"
_ITEM_PATH: Final = "/content/library/item/{id}"
_ITEM_FILES_PATH: Final = "/content/library/item/{id}/file"
_ITEM_STORAGE_PATH: Final = "/content/library/item/{id}/storage"
_OVF_FILTER_PATH: Final = "/vcenter/ovf/library-item/{id}?action=filter"
_PROBE_PATH: Final = "/content/subscribed-library?action=probe"
_SUPERVISOR_LIBRARIES_PATH: Final = "/vcenter/namespace-management/lifecycle/content/libraries"

#: The vendor calls, in the ``METHOD:/path`` form the reconcile test checks.
#: Path placeholders are written ``{}``: the 8.0 and 9.0 files name them
#: differently (``{library_id}`` and ``{libraryId}``).
VENDOR_OP_IDS: Final[frozenset[str]] = frozenset(
    {
        "POST:/content/library?action=find",
        "GET:/content/library/{}",
        "GET:/content/local-library/{}",
        "GET:/content/subscribed-library/{}",
        "GET:/content/library/item?library_id",
        "GET:/content/library/item/{}",
        "GET:/content/library/item/{}/file",
        "GET:/content/library/item/{}/storage",
        "POST:/vcenter/ovf/library-item/{}?action=filter",
        "POST:/content/subscribed-library?action=probe",
        f"GET:{_SUPERVISOR_LIBRARIES_PATH}",
    }
)

_LIBRARY_TYPE_LOCAL: Final = "LOCAL"
_LIBRARY_TYPE_SUBSCRIBED: Final = "SUBSCRIBED"
_PROBE_SUCCESS: Final = "SUCCESS"
#: The OVF parameter types the options read turns into fields.
_DEPLOYMENT_OPTION_PARAMS: Final = "DeploymentOptionParams"
_PROPERTY_PARAMS: Final = "PropertyParams"
_SIZE_PARAMS: Final = "SizeParams"
_IP_ALLOCATION_PARAMS: Final = "IpAllocationParams"
_SIZE_FIELDS: Final = (
    "approximate_download_size",
    "approximate_flat_deployment_size",
    "approximate_sparse_deployment_size",
    "variable_disk_size",
)
_IP_ALLOCATION_FIELDS: Final = (
    "supported_ip_allocation_policy",
    "ip_allocation_policy",
    "supported_ip_protocol",
    "ip_protocol",
)
_PROPERTY_FIELDS: Final = (
    "id",
    "label",
    "description",
    "type",
    "category",
    "class_id",
    "instance_id",
)


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _unwrap(payload: Any) -> Any:
    """The inner ``value`` of a legacy ``/rest`` answer, else the answer itself."""
    if isinstance(payload, dict) and set(payload) == {"value"}:
        return payload["value"]
    return payload


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _rows(value: Any) -> list[Mapping[str, Any]]:
    return [row for row in value if isinstance(row, Mapping)] if isinstance(value, list) else []


def _strings(value: Any) -> list[str]:
    return [entry for entry in value if isinstance(entry, str)] if isinstance(value, list) else []


def _pick(data: Mapping[str, Any], fields: tuple[str, ...]) -> dict[str, Any]:
    return {name: data.get(name) for name in fields}


def _checked_id(params: Mapping[str, Any], name: str) -> str:
    """Return ``params[name]`` when it is a content-library id (a UUID).

    The parameter schema already refuses other values. This second check runs
    before any request, so no other text can reach a URL path.
    """
    from meho_backplane.connectors.vmware_rest.composites._library_checks import id_problem

    value = params.get(name)
    problem = id_problem(value, name)
    if problem is not None:
        raise ValueError(problem)
    return str(value)


async def _get(
    connector: VmwareRestConnector,
    operator: Operator,
    target: VsphereTargetLike,
    path: str,
    *,
    missing: str,
    query: dict[str, Any] | None = None,
) -> Any:
    """One ``GET`` on the connector session; a 404 is ``not_found`` for *missing*."""
    mounted = await connector.mount_op_path(target, path, operator)
    try:
        payload = await connector._get_json(target, mounted, operator=operator, params=query)
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code != 404:
            raise
        error = ConnectorResourceNotFoundError([missing], f"vCenter has no {missing}")
    else:
        return _unwrap(payload)
    raise error


async def _post(
    connector: VmwareRestConnector,
    operator: Operator,
    target: VsphereTargetLike,
    path: str,
    body: dict[str, Any],
    *,
    missing: str | None = None,
) -> Any:
    """One ``POST`` that changes nothing (a vCenter ``?action=`` read)."""
    mounted = await connector.mount_op_path(target, path, operator)
    try:
        payload = await connector._post_json(target, mounted, operator=operator, json=body)
    except httpx.HTTPStatusError as exc:
        if missing is None or exc.response.status_code != 404:
            raise
        error = ConnectorResourceNotFoundError([missing], f"vCenter has no {missing}")
    else:
        return _unwrap(payload)
    raise error


# ---------------------------------------------------------------------------
# vmware.content_library.get
# ---------------------------------------------------------------------------


async def _library_id(
    connector: VmwareRestConnector,
    operator: Operator,
    target: VsphereTargetLike,
    params: Mapping[str, Any],
) -> str:
    """The library id from ``library_id``, or the one library ``library_name`` names."""
    from meho_backplane.connectors.vmware_rest.composites._library_checks import (
        resolve_library_id,
    )

    if params.get("library_id") is not None:
        return _checked_id(params, "library_id")
    library_id, refusal = await resolve_library_id(connector, target, operator, dict(params))
    if refusal is not None:
        raise ValueError(f"{GET_OP_ID}: {refusal[1]}")
    if library_id is None:
        name = str(params.get("library_name"))
        raise ConnectorResourceNotFoundError([name], f"vCenter has no library named {name!r}")
    return library_id


def _publish_row(info: Any) -> dict[str, Any] | None:
    data = _mapping(info)
    if not data:
        return None
    return _pick(data, ("published", "publish_url", "authentication_method", "user_name"))


def _subscription_row(info: Any) -> dict[str, Any] | None:
    data = _mapping(info)
    if not data:
        return None
    row = _pick(
        data,
        (
            "subscription_url",
            "authentication_method",
            "automatic_sync_enabled",
            "on_demand",
            "ssl_thumbprint",
            "user_name",
        ),
    )
    row["source_library"] = _mapping(data.get("source_info")).get("source_library")
    return row


def library_row(model: Any, *, library_id: str, item_count: int | None) -> dict[str, Any]:
    """Project a vendor ``LibraryModel`` onto the result. No password is copied."""
    data = _mapping(model)
    backings = [
        _pick(backing, ("type", "datastore_id", "storage_uri"))
        for backing in _rows(data.get("storage_backings"))
    ]
    return {
        "library_id": data.get("id") or library_id,
        "name": data.get("name"),
        "type": data.get("type"),
        "description": data.get("description"),
        "creation_time": data.get("creation_time"),
        "last_modified_time": data.get("last_modified_time"),
        "last_sync_time": data.get("last_sync_time"),
        "version": data.get("version"),
        "server_guid": data.get("server_guid"),
        "state": _mapping(data.get("state_info")).get("state"),
        "storage_backings": backings,
        "publish": _publish_row(data.get("publish_info")),
        "subscription": _subscription_row(data.get("subscription_info")),
        "item_count": item_count,
    }


async def content_library_get_impl(
    connector: VmwareRestConnector,
    operator: Operator,
    target: VsphereTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Implementation of ``vmware.content_library.get``."""
    library_id = await _library_id(connector, operator, target, params)
    missing = f"content library {library_id}"
    model = _mapping(
        await _get(
            connector, operator, target, _LIBRARY_PATH.format(id=library_id), missing=missing
        )
    )
    typed_path = {
        _LIBRARY_TYPE_LOCAL: _LOCAL_LIBRARY_PATH,
        _LIBRARY_TYPE_SUBSCRIBED: _SUBSCRIBED_LIBRARY_PATH,
    }.get(str(model.get("type")))
    if typed_path is not None:
        typed = _mapping(
            await _get(
                connector, operator, target, typed_path.format(id=library_id), missing=missing
            )
        )
        model = {**model, **typed}
    item_ids = await _get(
        connector, operator, target, _ITEMS_PATH, missing=missing, query={"library_id": library_id}
    )
    item_count = len(item_ids) if isinstance(item_ids, list) else None
    _log.info("vmware_content_library_read", target=target.name, item_count=item_count)
    return library_row(model, library_id=library_id, item_count=item_count)


# ---------------------------------------------------------------------------
# vmware.content_library.item.get / item.files.list
# ---------------------------------------------------------------------------


def item_row(model: Any, *, item_id: str) -> dict[str, Any]:
    """Project a vendor ``ItemModel`` onto the result."""
    data = _mapping(model)
    return {
        "library_item_id": data.get("id") or item_id,
        "library_id": data.get("library_id"),
        "name": data.get("name"),
        "type": data.get("type"),
        "description": data.get("description"),
        "size": data.get("size"),
        "cached": data.get("cached"),
        "content_version": data.get("content_version"),
        "metadata_version": data.get("metadata_version"),
        "version": data.get("version"),
        "creation_time": data.get("creation_time"),
        "last_modified_time": data.get("last_modified_time"),
        "last_sync_time": data.get("last_sync_time"),
        "source_id": data.get("source_id"),
        "security_compliance": data.get("security_compliance"),
    }


async def item_get_impl(
    connector: VmwareRestConnector,
    operator: Operator,
    target: VsphereTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Implementation of ``vmware.content_library.item.get``."""
    item_id = _checked_id(params, "library_item_id")
    model = await _get(
        connector,
        operator,
        target,
        _ITEM_PATH.format(id=item_id),
        missing=f"content library item {item_id}",
    )
    return item_row(model, item_id=item_id)


def file_rows(files: Any, storage: Any) -> list[dict[str, Any]]:
    """One row per file of an item, with the datastores and URIs where it is stored."""
    places: dict[str, dict[str, list[str]]] = {}
    for entry in _rows(storage):
        name = entry.get("name")
        if not isinstance(name, str):
            continue
        place = places.setdefault(name, {"datastore_ids": [], "storage_uris": []})
        datastore = _mapping(entry.get("storage_backing")).get("datastore_id")
        if isinstance(datastore, str) and datastore not in place["datastore_ids"]:
            place["datastore_ids"].append(datastore)
        for uri in _strings(entry.get("storage_uris")):
            if uri not in place["storage_uris"]:
                place["storage_uris"].append(uri)
    rows: list[dict[str, Any]] = []
    for entry in _rows(files):
        name = entry.get("name")
        checksum = _mapping(entry.get("checksum_info"))
        place = places.get(name, {}) if isinstance(name, str) else {}
        rows.append(
            {
                "name": name,
                "size": entry.get("size"),
                "cached": entry.get("cached"),
                "version": entry.get("version"),
                "checksum_algorithm": checksum.get("algorithm"),
                "checksum": checksum.get("checksum"),
                "datastore_ids": place.get("datastore_ids", []),
                "storage_uris": place.get("storage_uris", []),
            }
        )
    return rows


async def item_files_list_impl(
    connector: VmwareRestConnector,
    operator: Operator,
    target: VsphereTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Implementation of ``vmware.content_library.item.files.list``."""
    item_id = _checked_id(params, "library_item_id")
    missing = f"content library item {item_id}"
    files = await _get(
        connector, operator, target, _ITEM_FILES_PATH.format(id=item_id), missing=missing
    )
    storage = await _get(
        connector, operator, target, _ITEM_STORAGE_PATH.format(id=item_id), missing=missing
    )
    rows = file_rows(files, storage)
    return {"library_item_id": item_id, "file_count": len(rows), "files": rows}


# ---------------------------------------------------------------------------
# vmware.content_library.item.ovf_options
# ---------------------------------------------------------------------------


def _deploy_target(params: Mapping[str, Any]) -> dict[str, Any]:
    """The vendor ``DeploymentTarget``: the resource pool, and the host and folder when given."""
    deploy_target: dict[str, Any] = {"resource_pool_id": params["resource_pool"]}
    for param, field in (("host", "host_id"), ("folder", "folder_id")):
        value = params.get(param)
        if isinstance(value, str) and value:
            deploy_target[field] = value
    return deploy_target


def _property_row(prop: Mapping[str, Any]) -> dict[str, Any]:
    """One OVF property. The value of a ``password`` property is never returned."""
    row = _pick(prop, _PROPERTY_FIELDS)
    kind = prop.get("type")
    is_password = isinstance(kind, str) and kind.lower().startswith("password")
    row["value"] = None if is_password else prop.get("value")
    return row


def ovf_options_row(summary: Any, *, item_id: str) -> dict[str, Any]:
    """Project a vendor ``OvfSummary`` onto the result."""
    data = _mapping(summary)
    row: dict[str, Any] = {
        "library_item_id": item_id,
        "name": data.get("name"),
        "annotation": data.get("annotation"),
        "eulas": _strings(data.get("eulas")),
        "networks": _strings(data.get("networks")),
        "storage_groups": _strings(data.get("storage_groups")),
        "deployment_options": [],
        "default_deployment_option": None,
        "properties": [],
        "disk_size": None,
        "ip_allocation": None,
    }
    for params in _rows(data.get("additional_params")):
        kind = params.get("type")
        if kind == _DEPLOYMENT_OPTION_PARAMS:
            options = _rows(params.get("deployment_options"))
            row["deployment_options"] = [
                _pick(option, ("key", "label", "description", "default_choice"))
                for option in options
            ]
            defaults = [o.get("key") for o in options if o.get("default_choice") is True]
            row["default_deployment_option"] = params.get("selected_key") or next(
                iter(defaults), None
            )
        elif kind == _PROPERTY_PARAMS:
            row["properties"] = [_property_row(p) for p in _rows(params.get("properties"))]
        elif kind == _SIZE_PARAMS:
            row["disk_size"] = _pick(params, _SIZE_FIELDS)
        elif kind == _IP_ALLOCATION_PARAMS:
            row["ip_allocation"] = _pick(params, _IP_ALLOCATION_FIELDS)
    return row


async def item_ovf_options_impl(
    connector: VmwareRestConnector,
    operator: Operator,
    target: VsphereTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Implementation of ``vmware.content_library.item.ovf_options``.

    vCenter wants the deployment target inside ``target`` (``{"target":
    {"resource_pool_id": ...}}``), on vCenter 8 and 9 alike.
    """
    item_id = _checked_id(params, "library_item_id")
    summary = await _post(
        connector,
        operator,
        target,
        _OVF_FILTER_PATH.format(id=item_id),
        {"target": _deploy_target(params)},
        missing=f"content library item {item_id}",
    )
    return ovf_options_row(summary, item_id=item_id)


# ---------------------------------------------------------------------------
# vmware.content_library.subscribed.probe
# ---------------------------------------------------------------------------


def _message_text(message: Any) -> str | None:
    data = _mapping(message)
    text = data.get("default_message") or data.get("id")
    return text if isinstance(text, str) else None


async def subscribed_probe_impl(
    connector: VmwareRestConnector,
    operator: Operator,
    target: VsphereTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Implementation of ``vmware.content_library.subscribed.probe``.

    Sends the URL with no login (``authentication_method: NONE``). A source
    that needs a login answers ``INVALID_CREDENTIALS``, which still shows that
    vCenter reaches it.
    """
    url = params["subscription_url"]
    body = {"subscription_info": {"subscription_url": url, "authentication_method": "NONE"}}
    result = _mapping(await _post(connector, operator, target, _PROBE_PATH, body))
    status = result.get("status")
    messages = [text for m in _rows(result.get("error_messages")) if (text := _message_text(m))]
    return {
        "subscription_url": url,
        "status": status,
        "reachable": status == _PROBE_SUCCESS,
        "ssl_thumbprint": result.get("ssl_thumbprint"),
        "error_messages": messages,
    }


# ---------------------------------------------------------------------------
# vmware.supervisor.content_library.list
# ---------------------------------------------------------------------------


async def supervisor_content_library_list_impl(
    connector: VmwareRestConnector,
    operator: Operator,
    target: VsphereTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Implementation of ``vmware.supervisor.content_library.list`` (vCenter 9.0 and newer)."""
    del params  # the schema declares no params
    require_vcenter_version(
        target,
        op_id=SUPERVISOR_LIST_OP_ID,
        minimum=SUPERVISOR_MINIMUM_VERSION,
        feature="the list of Supervisor content libraries",
    )
    answer = _mapping(
        await _get(
            connector, operator, target, _SUPERVISOR_LIBRARIES_PATH, missing="Supervisor libraries"
        )
    )
    rows = [
        {"library_id": _supervisor_library_id(entry.get("library")), "status": entry.get("status")}
        for entry in _rows(answer.get("libraries"))
    ]
    return {"library_count": len(rows), "libraries": rows}


def _supervisor_library_id(value: Any) -> str | None:
    """Read the library id from a Supervisor library row.

    vCenter 9.0 sends ``{"library": {"id": "<uuid>"}, "status": ...}``. Keep a
    plain string as is, in case a build sends the id directly.
    """
    if isinstance(value, Mapping):
        library_id = value.get("id")
        return library_id if isinstance(library_id, str) else None
    return value if isinstance(value, str) else None
