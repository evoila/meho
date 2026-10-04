# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Governed content-library deletes -- whole library + single item (#3339 / #3331).

* ``vmware.composite.content_library.delete`` -- delete a LOCAL
  (``DELETE /content/local-library/{id}``) or SUBSCRIBED
  (``DELETE /content/subscribed-library/{id}``) library; the type is read
  from ``GET /content/library/{id}`` first.
* ``vmware.composite.content_library.item.delete`` -- delete one item of a
  LOCAL library (``DELETE /content/library/item/{id}``).

The delete half of #3331 (create / upload stay there) and the item delete of
#3339. Both are plain vCenter REST: the content-library ``/api`` surface is
served by vCenter 8.0 and 9.0 alike (the same find / item reads
``vm.deploy_from_library`` already runs on 8.0.x), so no VI-JSON is needed.
Reads go through the un-gated ``_read_sub_op`` / find seam, the DELETE
through ``_write_sub_op`` (the #2254 sub-op gate). Both are
``safety_level="destructive"`` + ``requires_approval=True`` and follow the
plan-then-act seam of :mod:`._teardown` (absent -> ``unchanged``, blockers ->
``precondition_failed`` before any write, read-back -> ``deleted`` /
``still_present``; a REST fault raises -> ``connector_error``).

Refusals (before any write):

* library: another library **on this vCenter** subscribes to it (a SUBSCRIBED
  library whose ``subscription_url`` names this library's id) -- delete the
  subscribers first; a non-empty library unless ``delete_items=true``
  (#3331's explicit-flag shape; the items are listed in the blast radius);
* item: the item belongs to a SUBSCRIBED library (its content is the
  publisher's -- delete / evict through the subscribed library instead).

Not detectable through the vCenter API, so not checked: subscribers on
*other* vCenters, and a VM CD-ROM mounting an ISO item (vSphere's own file
lock refuses that delete while the VM runs; the fault raises).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final

import httpx

from meho_backplane.connectors import OperationResult
from meho_backplane.connectors.vmware_rest.composites._teardown import (
    STATUS_DELETED,
    STATUS_INVALID_REQUEST,
    STATUS_PRECONDITION_FAILED,
    STATUS_STILL_PRESENT,
    TeardownPlan,
    blast_radius,
    capped,
    envelope,
    pre_write_outcome,
)
from meho_backplane.connectors.vmware_rest.composites._write import (
    _OP_FIND_LIBRARY,
    _OP_FIND_LIBRARY_ITEM,
    _find_content_library_ids,
    _read_sub_op,
    _unwrap_value,
    _write_sub_op,
)

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors.vmware_rest.connector import VmwareRestConnector
    from meho_backplane.operations._preview import PreviewContext

__all__ = [
    "content_library_delete_composite",
    "content_library_delete_preview",
    "content_library_item_delete_composite",
    "content_library_item_delete_preview",
]

_OP_GET_LIBRARY: Final = "GET:/content/library/{libraryId}"
_OP_LIST_SUBSCRIBED_LIBRARIES: Final = "GET:/content/subscribed-library"
_OP_GET_SUBSCRIBED_LIBRARY: Final = "GET:/content/subscribed-library/{libraryId}"
_OP_DELETE_LOCAL_LIBRARY: Final = "DELETE:/content/local-library/{libraryId}"
_OP_DELETE_SUBSCRIBED_LIBRARY: Final = "DELETE:/content/subscribed-library/{libraryId}"
_OP_GET_LIBRARY_ITEM: Final = "GET:/content/library/item/{libraryItemId}"
_OP_LIST_ITEM_FILES: Final = "GET:/content/library/item/{libraryItemId}/file"
_OP_DELETE_LIBRARY_ITEM: Final = "DELETE:/content/library/item/{libraryItemId}"

#: Sub-op manifests (reconciled against the pinned vcenter.yaml; also the
#: governed-subop discovery source -- the DELETE children are delete-shaped,
#: so the discovery surface flags them un-grantable).
_SUB_OPS_CONTENT_LIBRARY_DELETE: Final[tuple[str, ...]] = (
    _OP_FIND_LIBRARY,
    _OP_GET_LIBRARY,
    _OP_FIND_LIBRARY_ITEM,
    _OP_GET_LIBRARY_ITEM,
    _OP_LIST_SUBSCRIBED_LIBRARIES,
    _OP_GET_SUBSCRIBED_LIBRARY,
    _OP_DELETE_LOCAL_LIBRARY,
    _OP_DELETE_SUBSCRIBED_LIBRARY,
)
_SUB_OPS_CONTENT_LIBRARY_ITEM_DELETE: Final[tuple[str, ...]] = (
    _OP_FIND_LIBRARY,
    _OP_FIND_LIBRARY_ITEM,
    _OP_GET_LIBRARY_ITEM,
    _OP_GET_LIBRARY,
    _OP_LIST_ITEM_FILES,
    _OP_DELETE_LIBRARY_ITEM,
)

_LOCAL: Final = "LOCAL"
_SUBSCRIBED: Final = "SUBSCRIBED"
_DELETE_OP_BY_TYPE: Final[dict[str, str]] = {
    _LOCAL: _OP_DELETE_LOCAL_LIBRARY,
    _SUBSCRIBED: _OP_DELETE_SUBSCRIBED_LIBRARY,
}
#: Per-item reads made for the blast radius (names + sizes); the item COUNT
#: is always exact.
_ITEM_DETAIL_CAP: Final = 50


async def _get_or_none(
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


async def _resolve_library_id(
    connector: VmwareRestConnector, target: Any, operator: Operator, params: dict[str, Any]
) -> tuple[str | None, tuple[str, str] | None]:
    """``(id, None)``, ``(None, None)`` when the name matches nothing, or a refusal."""
    library_id = params.get("library_id")
    if isinstance(library_id, str) and library_id:
        return library_id, None
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
    return (ids[0] if ids else None), None


def _int_or_none(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


# ===========================================================================
# content_library.delete
# ===========================================================================


async def _local_subscribers(
    connector: VmwareRestConnector, target: Any, operator: Operator, library_id: str
) -> list[dict[str, Any]]:
    """SUBSCRIBED libraries on this vCenter whose subscription URL names *library_id*."""
    listing = _unwrap_value(
        await _read_sub_op(connector, target, operator, _OP_LIST_SUBSCRIBED_LIBRARIES)
    )
    rows: list[dict[str, Any]] = []
    for sub_id in listing if isinstance(listing, list) else []:
        if not isinstance(sub_id, str) or sub_id == library_id:
            continue
        model = await _get_or_none(
            connector, target, operator, _OP_GET_SUBSCRIBED_LIBRARY, {"libraryId": sub_id}
        )
        if model is None:
            continue
        info = model.get("subscription_info")
        url = info.get("subscription_url") if isinstance(info, dict) else None
        if isinstance(url, str) and library_id in url:
            rows.append({"kind": "subscribed_library", "id": sub_id, "name": model.get("name")})
    return rows


async def _item_rows(
    connector: VmwareRestConnector, target: Any, operator: Operator, item_ids: list[str]
) -> tuple[list[dict[str, Any]], int]:
    """``(item rows, summed size)`` for the first :data:`_ITEM_DETAIL_CAP` items."""
    rows: list[dict[str, Any]] = []
    total = 0
    for item_id in item_ids[:_ITEM_DETAIL_CAP]:
        model = await _get_or_none(
            connector, target, operator, _OP_GET_LIBRARY_ITEM, {"libraryItemId": item_id}
        )
        size = _int_or_none(model.get("size")) if model else None
        total += size or 0
        rows.append(
            {
                "kind": "library_item",
                "id": item_id,
                "name": model.get("name") if model else None,
                "type": model.get("type") if model else None,
                "size_bytes": size,
            }
        )
    return rows, total


def _library_refusal(
    plan: TeardownPlan, *, library_id: str, library_type: Any, delete_items: bool
) -> tuple[str, str] | None:
    if library_type not in _DELETE_OP_BY_TYPE:
        return (
            STATUS_PRECONDITION_FAILED,
            f"library {library_id!r} has unsupported type {library_type!r}",
        )
    if plan.blockers:
        names = ", ".join(str(row.get("name") or row["id"]) for row in plan.blockers[:10])
        return (
            STATUS_PRECONDITION_FAILED,
            f"library {library_id!r} is published to {len(plan.blockers)} subscribed "
            f"library(ies) on this vCenter [{names}]; delete the subscribers first "
            "(see 'blockers')",
        )
    count = plan.object.get("item_count") or 0
    if count and not delete_items:
        return (
            STATUS_PRECONDITION_FAILED,
            f"library {library_id!r} holds {count} item(s); pass delete_items=true to delete "
            "the library together with its items (the blast radius lists them)",
        )
    return None


async def plan_content_library_delete(
    connector: VmwareRestConnector,
    target: Any,
    operator: Operator,
    params: dict[str, Any],
    *,
    enumerate_items: bool,
) -> TeardownPlan:
    """Plan a whole-library delete (read-only)."""
    obj: dict[str, Any] = {
        "kind": "content_library",
        "id": params.get("library_id"),
        "name": params.get("library_name"),
    }
    library_id, refusal = await _resolve_library_id(connector, target, operator, params)
    if refusal is not None or library_id is None:
        return TeardownPlan(obj, False, refusal=refusal)
    model = await _get_or_none(
        connector, target, operator, _OP_GET_LIBRARY, {"libraryId": library_id}
    )
    if model is None:
        return TeardownPlan({**obj, "id": library_id}, False)
    publish = model.get("publish_info")
    backings = model.get("storage_backings")
    obj.update(
        id=library_id,
        name=model.get("name"),
        type=model.get("type"),
        published=bool(publish.get("published")) if isinstance(publish, dict) else False,
        datastores=[
            b.get("datastore_id")
            for b in (backings if isinstance(backings, list) else [])
            if isinstance(b, dict)
        ],
    )
    item_ids = await _find_content_library_ids(
        connector, target, operator, op_id=_OP_FIND_LIBRARY_ITEM, spec={"library_id": library_id}
    )
    obj["item_count"] = len(item_ids)
    plan = TeardownPlan(obj, True, context={"library_id": library_id})
    if enumerate_items:
        plan.children, obj["total_size_bytes"] = await _item_rows(
            connector, target, operator, item_ids
        )
    if obj["type"] == _LOCAL and obj["published"]:
        plan.blockers = capped(await _local_subscribers(connector, target, operator, library_id))
    plan.refusal = _library_refusal(
        plan,
        library_id=library_id,
        library_type=obj["type"],
        delete_items=bool(params.get("delete_items", False)),
    )
    return plan


async def content_library_delete_composite(
    *,
    operator: Operator,
    target: Any,
    params: dict[str, Any],
    connector: VmwareRestConnector,
) -> dict[str, Any] | OperationResult:
    """Delete a LOCAL or SUBSCRIBED content library (#3331 delete half).

    Op-id: ``vmware.composite.content_library.delete``.
    """
    plan = await plan_content_library_delete(
        connector, target, operator, params, enumerate_items=False
    )
    early = pre_write_outcome(
        plan, absent_guidance="the library does not exist; nothing was deleted"
    )
    if early is not None:
        return early
    library_id = plan.context["library_id"]
    gate, _payload = await _write_sub_op(
        connector,
        target,
        operator,
        _DELETE_OP_BY_TYPE[plan.object["type"]],
        {"libraryId": library_id},
    )
    if gate is not None:
        return gate
    after = await _get_or_none(
        connector, target, operator, _OP_GET_LIBRARY, {"libraryId": library_id}
    )
    if after is not None:
        return envelope(
            plan,
            STATUS_STILL_PRESENT,
            guidance="DELETE returned but the library still reads back; re-check vCenter",
        )
    return envelope(plan, STATUS_DELETED)


async def content_library_delete_preview(ctx: PreviewContext) -> dict[str, Any] | None:
    """Blast radius for ``content_library.delete``: the library + every item that dies."""
    if ctx.connector_instance is None:
        return None
    plan = await plan_content_library_delete(
        ctx.connector_instance,  # type: ignore[arg-type]
        ctx.target,
        ctx.operator,
        dict(ctx.params),
        enumerate_items=True,
    )
    return blast_radius(plan)


# ===========================================================================
# content_library.item.delete
# ===========================================================================


async def _resolve_item_id(
    connector: VmwareRestConnector, target: Any, operator: Operator, params: dict[str, Any]
) -> tuple[str | None, tuple[str, str] | None]:
    """``(id, None)``, ``(None, None)`` when nothing matches, or a refusal."""
    item_id = params.get("item_id")
    if isinstance(item_id, str) and item_id:
        return item_id, None
    item_name = params.get("item_name")
    if not isinstance(item_name, str) or not item_name:
        return None, (STATUS_INVALID_REQUEST, "pass item_id, or item_name with a library")
    library_id, refusal = await _resolve_library_id(connector, target, operator, params)
    if refusal is not None or library_id is None:
        return None, refusal
    ids = await _find_content_library_ids(
        connector,
        target,
        operator,
        op_id=_OP_FIND_LIBRARY_ITEM,
        spec={"library_id": library_id, "name": item_name},
    )
    if len(ids) > 1:
        return None, (
            STATUS_INVALID_REQUEST,
            f"item_name {item_name!r} matched {len(ids)} items; pass item_id",
        )
    return (ids[0] if ids else None), None


async def _item_files(
    connector: VmwareRestConnector, target: Any, operator: Operator, item_id: str
) -> list[dict[str, Any]]:
    """Best-effort ``[{kind: file, name, size_bytes}]`` for the blast radius."""
    try:
        payload = await _read_sub_op(
            connector, target, operator, _OP_LIST_ITEM_FILES, {"libraryItemId": item_id}
        )
    except httpx.HTTPError:
        return []
    files = _unwrap_value(payload)
    return capped(
        [
            {"kind": "file", "name": f.get("name"), "size_bytes": _int_or_none(f.get("size"))}
            for f in (files if isinstance(files, list) else [])
            if isinstance(f, dict)
        ]
    )


async def plan_content_library_item_delete(
    connector: VmwareRestConnector,
    target: Any,
    operator: Operator,
    params: dict[str, Any],
    *,
    enumerate_files: bool,
) -> TeardownPlan:
    """Plan a single library-item delete (read-only)."""
    obj: dict[str, Any] = {
        "kind": "content_library_item",
        "id": params.get("item_id"),
        "name": params.get("item_name"),
    }
    item_id, refusal = await _resolve_item_id(connector, target, operator, params)
    if refusal is not None or item_id is None:
        return TeardownPlan(obj, False, refusal=refusal)
    model = await _get_or_none(
        connector, target, operator, _OP_GET_LIBRARY_ITEM, {"libraryItemId": item_id}
    )
    if model is None:
        return TeardownPlan({**obj, "id": item_id}, False)
    library_id = model.get("library_id")
    library = (
        await _get_or_none(connector, target, operator, _OP_GET_LIBRARY, {"libraryId": library_id})
        if isinstance(library_id, str)
        else None
    )
    obj.update(
        id=item_id,
        name=model.get("name"),
        type=model.get("type"),
        size_bytes=_int_or_none(model.get("size")),
        library_id=library_id,
        library_name=library.get("name") if library else None,
        library_type=library.get("type") if library else None,
    )
    plan = TeardownPlan(obj, True, context={"item_id": item_id})
    if enumerate_files:
        plan.children = await _item_files(connector, target, operator, item_id)
    if obj["library_type"] == _SUBSCRIBED:
        plan.refusal = (
            STATUS_PRECONDITION_FAILED,
            f"item {item_id!r} belongs to SUBSCRIBED library {library_id!r}; its content is "
            "synchronised from the publisher -- delete the item at the publisher or delete "
            "the subscribed library (vmware.composite.content_library.delete)",
        )
    return plan


async def content_library_item_delete_composite(
    *,
    operator: Operator,
    target: Any,
    params: dict[str, Any],
    connector: VmwareRestConnector,
) -> dict[str, Any] | OperationResult:
    """Delete one content-library item of a LOCAL library (#3339).

    Op-id: ``vmware.composite.content_library.item.delete``.
    """
    plan = await plan_content_library_item_delete(
        connector, target, operator, params, enumerate_files=False
    )
    early = pre_write_outcome(plan, absent_guidance="the item does not exist; nothing was deleted")
    if early is not None:
        return early
    item_id = plan.context["item_id"]
    gate, _payload = await _write_sub_op(
        connector, target, operator, _OP_DELETE_LIBRARY_ITEM, {"libraryItemId": item_id}
    )
    if gate is not None:
        return gate
    after = await _get_or_none(
        connector, target, operator, _OP_GET_LIBRARY_ITEM, {"libraryItemId": item_id}
    )
    if after is not None:
        return envelope(
            plan,
            STATUS_STILL_PRESENT,
            guidance="DELETE returned but the item still reads back; re-check the library",
        )
    return envelope(plan, STATUS_DELETED)


async def content_library_item_delete_preview(ctx: PreviewContext) -> dict[str, Any] | None:
    """Blast radius for ``content_library.item.delete``: the item + its files."""
    if ctx.connector_instance is None:
        return None
    plan = await plan_content_library_item_delete(
        ctx.connector_instance,  # type: ignore[arg-type]
        ctx.target,
        ctx.operator,
        dict(ctx.params),
        enumerate_files=True,
    )
    return blast_radius(plan)
