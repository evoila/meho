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
``vm.deploy_from_library`` already runs on 8.0.x). Reads go through the
un-gated ``_read_sub_op`` / find seam, the DELETE through ``_write_sub_op``
(the #2254 sub-op gate). Both are ``safety_level="destructive"`` +
``requires_approval=True`` and follow the plan-then-act seam of
:mod:`._teardown` (absent -> ``unchanged``, blockers -> ``precondition_failed``
before any write, read-back -> ``deleted`` / ``still_present``; a REST fault
raises -> ``connector_error``).

Refusals (before any write; checks in :mod:`._library_checks`):

* any library / item id that is not a UUID (so no id can point a REST path
  at another object);
* library: another library subscribes to it (on this vCenter, or known to the
  publisher); a VM mounts one of its items; it holds items and
  ``delete_items`` is not true (#3331's explicit-flag shape);
* item: its library cannot be read back as a LOCAL library on this vCenter
  (a SUBSCRIBED library's content belongs to the publisher); a VM mounts it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final

import httpx

from meho_backplane.connectors import OperationResult
from meho_backplane.connectors.vmware_rest.composites._library_checks import (
    OP_GET_LIBRARY,
    OP_GET_LIBRARY_ITEM,
    OP_GET_SUBSCRIBED_LIBRARY,
    OP_LIST_LIBRARY_SUBSCRIPTIONS,
    OP_LIST_SUBSCRIBED_LIBRARIES,
    get_or_none,
    id_problem,
    int_or_none,
    media_check,
    resolve_library_id,
    subscribers,
)
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
    _OP_RETRIEVE_PROPERTIES,
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

_OP_DELETE_LOCAL_LIBRARY: Final = "DELETE:/content/local-library/{libraryId}"
_OP_DELETE_SUBSCRIBED_LIBRARY: Final = "DELETE:/content/subscribed-library/{libraryId}"
_OP_LIST_ITEM_FILES: Final = "GET:/content/library/item/{libraryItemId}/file"
_OP_DELETE_LIBRARY_ITEM: Final = "DELETE:/content/library/item/{libraryItemId}"

#: REST sub-op manifests (reconciled against the pinned vcenter.yaml; also the
#: governed-subop discovery source -- the DELETE children are delete-shaped,
#: so the discovery surface flags them un-grantable).
_SUB_OPS_CONTENT_LIBRARY_DELETE: Final[tuple[str, ...]] = (
    _OP_FIND_LIBRARY,
    OP_GET_LIBRARY,
    _OP_FIND_LIBRARY_ITEM,
    OP_GET_LIBRARY_ITEM,
    OP_LIST_SUBSCRIBED_LIBRARIES,
    OP_GET_SUBSCRIBED_LIBRARY,
    OP_LIST_LIBRARY_SUBSCRIPTIONS,
    _OP_DELETE_LOCAL_LIBRARY,
    _OP_DELETE_SUBSCRIBED_LIBRARY,
)
_SUB_OPS_CONTENT_LIBRARY_ITEM_DELETE: Final[tuple[str, ...]] = (
    _OP_FIND_LIBRARY,
    _OP_FIND_LIBRARY_ITEM,
    OP_GET_LIBRARY_ITEM,
    OP_GET_LIBRARY,
    _OP_LIST_ITEM_FILES,
    _OP_DELETE_LIBRARY_ITEM,
)
#: The mounted-item check reads ``Datastore.vm`` + the VMs' devices (vim).
_VIM_SUB_OPS_CONTENT_LIBRARY_MEDIA_CHECK: Final[tuple[str, ...]] = (_OP_RETRIEVE_PROPERTIES,)

_LOCAL: Final = "LOCAL"
_SUBSCRIBED: Final = "SUBSCRIBED"
_DELETE_OP_BY_TYPE: Final[dict[str, str]] = {
    _LOCAL: _OP_DELETE_LOCAL_LIBRARY,
    _SUBSCRIBED: _OP_DELETE_SUBSCRIBED_LIBRARY,
}
#: Per-item reads made for the blast radius (names + sizes); the item COUNT
#: is always exact.
_ITEM_DETAIL_CAP: Final = 50


def _names(rows: list[dict[str, Any]]) -> str:
    return ", ".join(str(row.get("name") or row.get("id") or row.get("moid")) for row in rows[:10])


# ===========================================================================
# content_library.delete
# ===========================================================================


async def _item_rows(
    connector: VmwareRestConnector, target: Any, operator: Operator, item_ids: list[str]
) -> tuple[list[dict[str, Any]], int]:
    """``(item rows, summed size)`` for the first :data:`_ITEM_DETAIL_CAP` items."""
    rows: list[dict[str, Any]] = []
    total = 0
    for item_id in item_ids[:_ITEM_DETAIL_CAP]:
        if id_problem(item_id, "item id") is not None:
            continue
        model = await get_or_none(
            connector, target, operator, OP_GET_LIBRARY_ITEM, {"libraryItemId": item_id}
        )
        size = int_or_none(model.get("size")) if model else None
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


async def _library_blockers(
    connector: VmwareRestConnector,
    target: Any,
    operator: Operator,
    *,
    plan: TeardownPlan,
    model: dict[str, Any],
    item_ids: list[str],
    delete_items: bool,
) -> None:
    """Fill ``plan.blockers`` / ``plan.refusal`` for a library delete."""
    library_id = plan.context["library_id"]
    if plan.object["type"] not in _DELETE_OP_BY_TYPE:
        detail = f"library {library_id!r} has unsupported type {plan.object['type']!r}"
        plan.refusal = (STATUS_PRECONDITION_FAILED, detail)
        return
    subs: list[dict[str, Any]] = []
    if plan.object["type"] == _LOCAL and plan.object["published"]:
        subs = await subscribers(connector, target, operator, library_id)
    mounts, problem = await media_check(
        connector, target, operator, library=model, item_ids=item_ids
    )
    plan.blockers = capped(subs + mounts)
    if subs:
        plan.refusal = (
            STATUS_PRECONDITION_FAILED,
            f"{len(subs)} library(ies) subscribe to library {library_id!r} [{_names(subs)}]; "
            "delete the subscribers first (see 'blockers')",
        )
    elif mounts:
        plan.refusal = (
            STATUS_PRECONDITION_FAILED,
            f"{len(mounts)} VM(s) mount an item of library {library_id!r} [{_names(mounts)}]; "
            "unmount it first (see 'blockers')",
        )
    elif problem is not None:
        plan.refusal = (STATUS_PRECONDITION_FAILED, problem)
    elif item_ids and not delete_items:
        plan.refusal = (
            STATUS_PRECONDITION_FAILED,
            f"library {library_id!r} holds {len(item_ids)} item(s); pass delete_items=true to "
            "delete the library together with its items (the preview lists them)",
        )


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
    library_id, refusal = await resolve_library_id(connector, target, operator, params)
    if refusal is not None or library_id is None:
        return TeardownPlan(obj, False, refusal=refusal)
    model = await get_or_none(
        connector, target, operator, OP_GET_LIBRARY, {"libraryId": library_id}
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
    await _library_blockers(
        connector,
        target,
        operator,
        plan=plan,
        model=model,
        item_ids=item_ids,
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
    after = await get_or_none(
        connector, target, operator, OP_GET_LIBRARY, {"libraryId": library_id}
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
    if params.get("item_id") is not None:
        problem = id_problem(params.get("item_id"), "item_id")
        if problem is not None:
            return None, (STATUS_INVALID_REQUEST, problem)
        return str(params["item_id"]), None
    item_name = params.get("item_name")
    if not isinstance(item_name, str) or not item_name:
        return None, (STATUS_INVALID_REQUEST, "pass item_id, or item_name with a library")
    library_id, refusal = await resolve_library_id(connector, target, operator, params)
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
    if ids and id_problem(ids[0], "item id") is not None:
        return None, (STATUS_INVALID_REQUEST, f"vCenter returned an unexpected id {ids[0]!r}")
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
            {"kind": "file", "name": f.get("name"), "size_bytes": int_or_none(f.get("size"))}
            for f in (files if isinstance(files, list) else [])
            if isinstance(f, dict)
        ]
    )


async def _item_library_refusal(
    connector: VmwareRestConnector,
    target: Any,
    operator: Operator,
    *,
    plan: TeardownPlan,
    library: dict[str, Any] | None,
) -> None:
    """Refuse unless the item's library reads back LOCAL and no VM mounts the item."""
    item_id = plan.context["item_id"]
    if library is None or plan.object["library_type"] != _LOCAL:
        plan.refusal = (
            STATUS_PRECONDITION_FAILED,
            f"item {item_id!r} is not in a local library that MEHO could read back on this "
            f"vCenter (library {plan.object['library_id']!r}, type "
            f"{plan.object['library_type']!r}). A subscribed library's items come from the "
            "publisher: delete the item there, or delete the subscribed library",
        )
        return
    mounts, problem = await media_check(
        connector, target, operator, library=library, item_ids=[item_id]
    )
    plan.blockers = capped(mounts)
    if mounts:
        plan.refusal = (
            STATUS_PRECONDITION_FAILED,
            f"{len(mounts)} VM(s) mount item {item_id!r} [{_names(mounts)}]; unmount it first "
            "(see 'blockers')",
        )
    elif problem is not None:
        plan.refusal = (STATUS_PRECONDITION_FAILED, problem)


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
    model = await get_or_none(
        connector, target, operator, OP_GET_LIBRARY_ITEM, {"libraryItemId": item_id}
    )
    if model is None:
        return TeardownPlan({**obj, "id": item_id}, False)
    library_id = model.get("library_id")
    library = (
        await get_or_none(connector, target, operator, OP_GET_LIBRARY, {"libraryId": library_id})
        if id_problem(library_id, "library id") is None
        else None
    )
    obj.update(
        id=item_id,
        name=model.get("name"),
        type=model.get("type"),
        size_bytes=int_or_none(model.get("size")),
        library_id=library_id,
        library_name=library.get("name") if library else None,
        library_type=library.get("type") if library else None,
    )
    plan = TeardownPlan(obj, True, context={"item_id": item_id})
    if enumerate_files:
        plan.children = await _item_files(connector, target, operator, item_id)
    await _item_library_refusal(connector, target, operator, plan=plan, library=library)
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
    after = await get_or_none(
        connector, target, operator, OP_GET_LIBRARY_ITEM, {"libraryItemId": item_id}
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
