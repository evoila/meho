# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Read-only checks for the content-library deletes (#3339 / #3331).

* **Strict ids.** A library or item id goes into a REST URL path. vCenter ids
  are UUIDs, so anything else is refused before any request
  (:func:`id_problem`) -- an id like ``../../vcenter/vm/vm-42`` can never point
  a request at another object.
* **Subscribers.** Libraries that subscribe to a library: subscribed libraries
  on this vCenter whose ``subscription_url`` names the library id, plus the
  subscriptions the publisher itself knows (``GET
  /content/library/{library}/subscriptions``, which also lists other vCenters).
* **Mounted items.** VMs whose CD-ROM or floppy drive uses a file of an item
  (:func:`media_check`), read through the library's datastores
  (``Datastore.vm``) and the VMs' device backings. A partial answer refuses.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any, Final

import httpx

from meho_backplane.connectors.vmware_rest.composites._datastore_claims import media_users
from meho_backplane.connectors.vmware_rest.composites._teardown import STATUS_INVALID_REQUEST
from meho_backplane.connectors.vmware_rest.composites._teardown_reads import (
    moref_values,
    read_object,
)
from meho_backplane.connectors.vmware_rest.composites._write import (
    _DATASTORE_MO_TYPE,
    _OP_FIND_LIBRARY,
    _find_content_library_ids,
    _read_sub_op,
    _unwrap_value,
)
from meho_backplane.connectors.vmware_rest.composites.schemas import CONTENT_LIBRARY_ID_PATTERN

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors.vmware_rest.connector import VmwareRestConnector

__all__ = [
    "OP_GET_LIBRARY",
    "OP_GET_LIBRARY_ITEM",
    "OP_GET_SUBSCRIBED_LIBRARY",
    "OP_LIST_LIBRARY_SUBSCRIPTIONS",
    "OP_LIST_SUBSCRIBED_LIBRARIES",
    "get_or_none",
    "id_problem",
    "int_or_none",
    "media_check",
    "resolve_library_id",
    "subscribers",
]

OP_GET_LIBRARY: Final = "GET:/content/library/{libraryId}"
OP_LIST_SUBSCRIBED_LIBRARIES: Final = "GET:/content/subscribed-library"
OP_GET_SUBSCRIBED_LIBRARY: Final = "GET:/content/subscribed-library/{libraryId}"
#: Subscriptions the publisher created (also on other vCenters).
OP_LIST_LIBRARY_SUBSCRIPTIONS: Final = "GET:/content/library/{library}/subscriptions"
OP_GET_LIBRARY_ITEM: Final = "GET:/content/library/item/{libraryItemId}"

_ID_RE: Final = re.compile(CONTENT_LIBRARY_ID_PATTERN)
_DATASTORE_BACKING: Final = "DATASTORE"


def id_problem(value: Any, label: str) -> str | None:
    """Why *value* is not a content-library / item id (a UUID), or ``None``."""
    if isinstance(value, str) and _ID_RE.fullmatch(value):
        return None
    return f"{label} must be a vCenter content-library id (a UUID); got {value!r}"


def int_or_none(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


async def get_or_none(
    connector: VmwareRestConnector,
    target: Any,
    operator: Operator,
    op_id: str,
    params: dict[str, Any],
) -> dict[str, Any] | None:
    """One un-gated GET; ``None`` on HTTP 404 (absent). Other faults raise."""
    try:
        payload = await _read_sub_op(connector, target, operator, op_id, params)
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 404:
            return None
        raise
    model = _unwrap_value(payload)
    return model if isinstance(model, dict) else None


async def resolve_library_id(
    connector: VmwareRestConnector, target: Any, operator: Operator, params: dict[str, Any]
) -> tuple[str | None, tuple[str, str] | None]:
    """``(id, None)``, ``(None, None)`` when the name matches nothing, or a refusal.

    A given ``library_id`` and an id vCenter returns for a name must both be
    UUIDs; anything else is refused before it can reach a URL path.
    """
    if params.get("library_id") is not None:
        library_id = params.get("library_id")
        problem = id_problem(library_id, "library_id")
        if problem is not None:
            return None, (STATUS_INVALID_REQUEST, problem)
        return str(library_id), None
    name = params.get("library_name")
    if not isinstance(name, str) or not name:
        return None, (STATUS_INVALID_REQUEST, "pass library_id or library_name")
    ids = await _find_content_library_ids(
        connector, target, operator, op_id=_OP_FIND_LIBRARY, spec={"name": name}
    )
    if len(ids) > 1:
        return None, (
            STATUS_INVALID_REQUEST,
            f"library_name {name!r} matched {len(ids)} libraries ({', '.join(ids)}); "
            "pass library_id",
        )
    if ids and id_problem(ids[0], "library id") is not None:
        return None, (STATUS_INVALID_REQUEST, f"vCenter returned an unexpected id {ids[0]!r}")
    return (ids[0] if ids else None), None


async def subscribers(
    connector: VmwareRestConnector, target: Any, operator: Operator, library_id: str
) -> list[dict[str, Any]]:
    """Libraries that subscribe to *library_id* (this vCenter + publisher-known ones)."""
    rows: dict[str, dict[str, Any]] = {}
    listing = _unwrap_value(
        await _read_sub_op(connector, target, operator, OP_LIST_SUBSCRIBED_LIBRARIES)
    )
    for sub_id in listing if isinstance(listing, list) else []:
        if not isinstance(sub_id, str) or id_problem(sub_id, "id") or sub_id == library_id:
            continue
        model = await get_or_none(
            connector, target, operator, OP_GET_SUBSCRIBED_LIBRARY, {"libraryId": sub_id}
        )
        info = model.get("subscription_info") if model else None
        url = info.get("subscription_url") if isinstance(info, dict) else None
        if model is not None and isinstance(url, str) and library_id in url:
            rows[sub_id] = {"kind": "subscribed_library", "id": sub_id, "name": model.get("name")}
    known = _unwrap_value(
        await _read_sub_op(
            connector, target, operator, OP_LIST_LIBRARY_SUBSCRIPTIONS, {"library": library_id}
        )
    )
    for index, sub in enumerate(known if isinstance(known, list) else []):
        if not isinstance(sub, dict):
            continue
        sub_id = str(sub.get("subscribed_library") or sub.get("subscription") or "")
        rows.setdefault(
            sub_id or f"subscription-{index}",
            {
                "kind": "subscribed_library",
                "id": sub_id or None,
                "name": sub.get("subscribed_library_name"),
                "vcenter": sub.get("subscribed_library_vcenter_hostname"),
            },
        )
    return list(rows.values())


async def media_check(
    connector: VmwareRestConnector,
    target: Any,
    operator: Operator,
    *,
    library: dict[str, Any],
    item_ids: list[str],
) -> tuple[list[dict[str, Any]], str | None]:
    """VMs that mount one of *item_ids*: ``(blocker rows, problem)``.

    ``problem`` is set when the check cannot be completed (the library is not
    stored only on datastores, a datastore is gone, or vCenter answered only
    part of the question) -- the caller refuses.
    """
    if not item_ids:
        return [], None
    backings = library.get("storage_backings")
    backings = backings if isinstance(backings, list) else []
    ds_ids = [
        b.get("datastore_id")
        for b in backings
        if isinstance(b, dict) and b.get("type") == _DATASTORE_BACKING
    ]
    if not backings or len(ds_ids) != len(backings) or not all(isinstance(d, str) for d in ds_ids):
        return [], (
            "the library is not stored only on datastores, so MEHO cannot check whether a VM "
            "mounts one of its items"
        )
    vm_moids: list[str] = []
    for ds_id in ds_ids:
        props = await read_object(
            connector, target, operator, mo_type=_DATASTORE_MO_TYPE, moid=str(ds_id), props=["vm"]
        )
        if props is None:
            return [], f"datastore {ds_id!r} of the library was not found; cannot check mounts"
        vm_moids += [m for _t, m in moref_values(props.get("vm")) if m not in vm_moids]
    rows, complete = await media_users(
        connector, target, operator, vm_moids=vm_moids, needles=[f"/{i}/" for i in item_ids]
    )
    if not complete:
        return rows, (
            "vCenter answered only part of the question 'which VMs mount these items?'; "
            "refusing rather than guess"
        )
    return rows, None
