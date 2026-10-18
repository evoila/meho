# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Blast-radius previews for the ingested vCenter tagging removals (#3827).

The VMware floor (``ingest_safety.py``) puts five ingested tagging ops in the
``destructive`` risk level: the category and tag ``DELETE``s and the three
tag-association ``detach`` actions. A destructive op can only wait for
approval when its preview names what is destroyed, the objects that go with
it, and whether the change can be undone (``blast_radius_missing_reason``).
This module computes that statement with read-only vCenter calls:

* **category delete** -- the category, every tag in it, and the objects each
  tag is attached to (a category delete removes all of that);
* **tag delete** -- the tag and every object it is attached to;
* **detach** -- the tag and the objects named in the request. A detach can
  be undone by attaching the tag again, so its class says so.

The builders are keyed by ``op_id``. The vendor spec spells the path
parameter ``{categoryId}`` / ``{tagId}`` and an appliance-served spec can
spell it ``{category_id}`` / ``{tag_id}``, with or without the ``/api``
mount; every spelling is registered so the stored row finds its builder. A
stored row can also spell a tag detach without the path parameter
(``POST /cis/tagging/tag-association?action=detach``); then the tag id
comes from the request body.

Every id that enters a read path is escaped as one path segment (the same
rule the dispatcher applies to the write itself) and a ``..`` traversal
segment is refused, because a detach request names tag ids in its body and
the preview reads run before anyone approves the request.

Child lists are complete: every tag or object that goes with the removal is
listed, with the exact count next to it. Only the per-child detail reads
(tag name, attached objects) stop at :data:`CHILD_DETAIL_CAP`; a child past
the cap is marked ``detail_omitted`` and a cut attached-object list carries
the number of omitted entries.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from itertools import product
from typing import Any, Final
from urllib.parse import quote

import httpx

from meho_backplane.connectors.adapters import HttpConnector
from meho_backplane.operations._preview import PreviewContext, register_preview_builder
from meho_backplane.operations._rfc6570 import has_dot_segment

__all__ = ["CHILD_DETAIL_CAP", "TAGGING_REMOVAL_OP_IDS", "register_tagging_removal_previews"]

#: Spec-relative tagging paths. ``{id}`` stands for the path parameter.
_CATEGORY_PATH: Final = "/cis/tagging/category/{id}"
_TAG_PATH: Final = "/cis/tagging/tag/{id}"
_ASSOCIATION_PATH: Final = "/cis/tagging/tag-association/{id}"
_LIST_TAGS_FOR_CATEGORY_PATH: Final = "/cis/tagging/tag?action=list-tags-for-category"
_LIST_ATTACHED_OBJECTS_PATH: Final = _ASSOCIATION_PATH + "?action=list-attached-objects"

_CATEGORY_PARAM_NAMES: Final = ("categoryId", "category_id")
_TAG_PARAM_NAMES: Final = ("tagId", "tag_id")
_MOUNT_PREFIXES: Final = ("", "/api")

#: How many children get the per-child detail reads (tag name, attached
#: objects). Every child is still listed and every count stays exact; a
#: child past the cap is marked ``detail_omitted``. The attached-object
#: list of one tag stops at the same bound and says how many it left out.
CHILD_DETAIL_CAP: Final = 50

#: The read-only op that lists every object a tag is attached to. Named on a
#: cut attached-object list so the approver can fetch the full list.
_LIST_ATTACHED_OBJECTS_OP: Final = (
    "POST:/cis/tagging/tag-association/{tagId}?action=list-attached-objects"
)

#: Irreversibility classes. The first two match the teardown composites.
IRREVERSIBLE: Final = "permanent"
ALREADY_ABSENT: Final = "none-object-already-absent"
REVERSIBLE_BY_REATTACH: Final = "reversible-attach-the-tag-again"


# ---------------------------------------------------------------------------
# Read helpers (no governance gate: these are the safe reads the preview needs)
# ---------------------------------------------------------------------------


def _unwrap(payload: Any) -> Any:
    """Return the inner ``value`` of a legacy ``/rest`` envelope, else *payload*."""
    if isinstance(payload, dict) and set(payload.keys()) == {"value"}:
        return payload["value"]
    return payload


async def _get(connector: HttpConnector, ctx: PreviewContext, path: str) -> Any | None:
    """One GET on the target; ``None`` on HTTP 404 (absent). Other faults raise."""
    mounted = await connector.mount_op_path(ctx.target, path, ctx.operator)
    try:
        payload = await connector._get_json(ctx.target, mounted, operator=ctx.operator)
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 404:
            return None
        raise
    return _unwrap(payload)


async def _post_read(
    connector: HttpConnector, ctx: PreviewContext, path: str, body: dict[str, Any] | None
) -> Any | None:
    """One read-only ``?action=list-*`` POST; ``None`` on HTTP 404."""
    mounted = await connector.mount_op_path(ctx.target, path, ctx.operator)
    try:
        payload = await connector._post_json(ctx.target, mounted, operator=ctx.operator, json=body)
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 404:
            return None
        raise
    return _unwrap(payload)


def _connector(ctx: PreviewContext) -> HttpConnector | None:
    connector = ctx.connector_instance
    return connector if isinstance(connector, HttpConnector) else None


def _path_param(params: Mapping[str, Any], names: Sequence[str]) -> str | None:
    """Return the path parameter under the first of *names* that is present."""
    for name in names:
        value = params.get(name)
        if isinstance(value, str) and value:
            return value
    return None


def _body(params: Mapping[str, Any]) -> Mapping[str, Any]:
    """Return the request body bucket of an ingested dispatch (``params["body"]``)."""
    body = params.get("body")
    return body if isinstance(body, Mapping) else {}


def _tag_id(params: Mapping[str, Any]) -> str | None:
    """The tag of a detach: the path parameter, else ``tag_id`` in the body.

    A stored row can spell the detach without the path parameter
    (``POST /cis/tagging/tag-association?action=detach``); its request then
    carries the tag id in the body next to the object.
    """
    return _path_param(params, _TAG_PARAM_NAMES) or _path_param(_body(params), _TAG_PARAM_NAMES)


def _segment(kind: str, value: Any) -> str:
    """Escape *value* as one path segment, or raise ``ValueError``.

    Mirrors the dispatcher's path renderer: a ``..`` traversal segment is
    refused, and every reserved character (``/``, ``?``, ``#``, ...) is
    percent-encoded so the id can never open another path or action. A
    builder that raises refuses the removal before it waits for approval;
    the refusal names this reason.
    """
    if not isinstance(value, str) or not value:
        raise ValueError(f"the {kind} id must be a non-empty string, got {value!r}")
    if has_dot_segment(value):
        raise ValueError(f"the {kind} id {value!r} contains a '..' path-traversal segment")
    return quote(value, safe="")


def _in_path(template: str, kind: str, value: Any) -> str:
    return template.replace("{id}", _segment(kind, value))


def _object_ref(value: Any) -> dict[str, Any]:
    """Render a vCenter ``DynamicID`` (``{type, id}``) as a child entry."""
    if isinstance(value, Mapping):
        return {"kind": value.get("type"), "id": value.get("id")}
    return {"kind": None, "id": value}


def _block(
    obj: dict[str, Any],
    *,
    present: bool,
    children: list[dict[str, Any]],
    irreversibility: str,
    children_count: int | None = None,
) -> dict[str, Any]:
    """The ``blast_radius`` block the destructive park gate requires.

    ``children_count`` is the exact number of children; it defaults to the
    length of *children* and is set explicitly when the list is cut. A cut
    list also carries ``children_omitted`` and the read op that lists the
    rest, so a shortened list is never mistaken for a complete one.
    """
    total = len(children) if children_count is None else children_count
    block: dict[str, Any] = {
        "object": {**obj, "present": present},
        "children": children,
        "children_count": total,
        "irreversibility": irreversibility,
    }
    if total > len(children):
        block["children_omitted"] = total - len(children)
        block["children_full_list_op"] = _LIST_ATTACHED_OBJECTS_OP
    return {"blast_radius": block}


# ---------------------------------------------------------------------------
# Tag and object lookups
# ---------------------------------------------------------------------------


async def _attached_objects(
    connector: HttpConnector, ctx: PreviewContext, tag_id: str
) -> list[dict[str, Any]]:
    path = _in_path(_LIST_ATTACHED_OBJECTS_PATH, "tag", tag_id)
    rows = await _post_read(connector, ctx, path, None)
    return [_object_ref(row) for row in rows] if isinstance(rows, list) else []


async def _tag_entry(connector: HttpConnector, ctx: PreviewContext, tag_id: str) -> dict[str, Any]:
    """One tag as a child entry: name, category, and the objects it is attached to.

    ``attached_object_count`` is exact. ``attached_objects`` stops at
    :data:`CHILD_DETAIL_CAP`; a cut list carries ``attached_objects_omitted``.
    """
    entry: dict[str, Any] = {"kind": "tag", "id": tag_id}
    detail = await _get(connector, ctx, _in_path(_TAG_PATH, "tag", tag_id))
    if isinstance(detail, dict):
        entry["name"] = detail.get("name")
        entry["category_id"] = detail.get("category_id")
    attached = await _attached_objects(connector, ctx, tag_id)
    entry["attached_object_count"] = len(attached)
    entry["attached_objects"] = attached[:CHILD_DETAIL_CAP]
    if len(attached) > CHILD_DETAIL_CAP:
        entry["attached_objects_omitted"] = len(attached) - CHILD_DETAIL_CAP
    return entry


async def _tag_entries(
    connector: HttpConnector, ctx: PreviewContext, tag_ids: Iterable[Any]
) -> list[dict[str, Any]]:
    """Every tag in *tag_ids* as a child entry; detail reads stop at the cap.

    Every id is validated first, so a bad id refuses the whole preview before
    any read runs. A tag past :data:`CHILD_DETAIL_CAP` is listed by id with
    ``detail_omitted`` set instead of its name and attached objects.
    """
    ids = [tag_id for tag_id in tag_ids if tag_id is not None]
    for tag_id in ids:
        _segment("tag", tag_id)
    entries = [await _tag_entry(connector, ctx, tag_id) for tag_id in ids[:CHILD_DETAIL_CAP]]
    entries.extend(
        {"kind": "tag", "id": tag_id, "detail_omitted": True} for tag_id in ids[CHILD_DETAIL_CAP:]
    )
    return entries


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


async def category_delete_preview(ctx: PreviewContext) -> dict[str, Any] | None:
    """Blast radius for ``DELETE /cis/tagging/category/{id}``: the category and its tags."""
    connector = _connector(ctx)
    category_id = _path_param(ctx.params, _CATEGORY_PARAM_NAMES)
    if connector is None or category_id is None:
        return None
    obj: dict[str, Any] = {"kind": "tag_category", "id": category_id}
    detail = await _get(connector, ctx, _in_path(_CATEGORY_PATH, "category", category_id))
    if not isinstance(detail, dict):
        return _block(obj, present=False, children=[], irreversibility=ALREADY_ABSENT)
    obj.update(
        name=detail.get("name"),
        description=detail.get("description"),
        cardinality=detail.get("cardinality"),
        associable_types=detail.get("associable_types"),
    )
    tag_ids = await _post_read(
        connector, ctx, _LIST_TAGS_FOR_CATEGORY_PATH, {"category_id": category_id}
    )
    tag_ids = tag_ids if isinstance(tag_ids, list) else []
    obj["tag_count"] = len(tag_ids)
    children = await _tag_entries(connector, ctx, tag_ids)
    return _block(obj, present=True, children=children, irreversibility=IRREVERSIBLE)


async def tag_delete_preview(ctx: PreviewContext) -> dict[str, Any] | None:
    """Blast radius for ``DELETE /cis/tagging/tag/{id}``: the tag and its attached objects."""
    connector = _connector(ctx)
    tag_id = _path_param(ctx.params, _TAG_PARAM_NAMES)
    if connector is None or tag_id is None:
        return None
    entry = await _tag_entry(connector, ctx, tag_id)
    present = "name" in entry
    attached = entry.pop("attached_objects")
    entry.pop("attached_objects_omitted", None)
    return _block(
        entry,
        present=present,
        children=attached if present else [],
        children_count=entry["attached_object_count"] if present else 0,
        irreversibility=IRREVERSIBLE if present else ALREADY_ABSENT,
    )


async def _detach_from_tag_preview(
    ctx: PreviewContext, objects: list[Any]
) -> dict[str, Any] | None:
    """Shared shape for the two detach actions keyed by a tag (path or body)."""
    connector = _connector(ctx)
    tag_id = _tag_id(ctx.params)
    if connector is None or tag_id is None:
        return None
    obj: dict[str, Any] = {"kind": "tag", "id": tag_id}
    detail = await _get(connector, ctx, _in_path(_TAG_PATH, "tag", tag_id))
    present = isinstance(detail, dict)
    if isinstance(detail, dict):
        obj["name"] = detail.get("name")
        obj["category_id"] = detail.get("category_id")
    return _block(
        obj,
        present=present,
        children=[_object_ref(value) for value in objects] if present else [],
        irreversibility=REVERSIBLE_BY_REATTACH if present else ALREADY_ABSENT,
    )


async def tag_detach_preview(ctx: PreviewContext) -> dict[str, Any] | None:
    """Blast radius for ``?action=detach``: the tag and the one object it leaves."""
    object_id = _body(ctx.params).get("object_id")
    return await _detach_from_tag_preview(ctx, [object_id] if object_id is not None else [])


async def detach_tag_from_objects_preview(ctx: PreviewContext) -> dict[str, Any] | None:
    """Blast radius for ``?action=detach-tag-from-multiple-objects``."""
    object_ids = _body(ctx.params).get("object_ids")
    return await _detach_from_tag_preview(
        ctx, list(object_ids) if isinstance(object_ids, list) else []
    )


async def detach_tags_from_object_preview(ctx: PreviewContext) -> dict[str, Any] | None:
    """Blast radius for ``?action=detach-multiple-tags-from-object``: the object and its tags."""
    connector = _connector(ctx)
    body = _body(ctx.params)
    if connector is None or "object_id" not in body:
        return None
    obj = _object_ref(body.get("object_id"))
    tag_ids = body.get("tag_ids")
    children = await _tag_entries(connector, ctx, tag_ids if isinstance(tag_ids, list) else [])
    return _block(obj, present=True, children=children, irreversibility=REVERSIBLE_BY_REATTACH)


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

_ASSOCIATION_ROOT: Final = "/cis/tagging/tag-association"

#: ``(method, path template, path parameter spellings, builder)``. A ``None``
#: spelling tuple means the path has no parameter. The two tag-keyed detach
#: actions are registered twice: with the tag in the path (vendor spec) and
#: without it (a stored row such as ``POST
#: /cis/tagging/tag-association?action=detach``, where the tag id is in the
#: request body).
_TAGGING_REMOVALS: Final[tuple[tuple[str, str, tuple[str, ...] | None, Any], ...]] = (
    ("DELETE", _CATEGORY_PATH, _CATEGORY_PARAM_NAMES, category_delete_preview),
    ("DELETE", _TAG_PATH, _TAG_PARAM_NAMES, tag_delete_preview),
    ("POST", _ASSOCIATION_PATH + "?action=detach", _TAG_PARAM_NAMES, tag_detach_preview),
    ("POST", _ASSOCIATION_ROOT + "?action=detach", None, tag_detach_preview),
    (
        "POST",
        _ASSOCIATION_PATH + "?action=detach-tag-from-multiple-objects",
        _TAG_PARAM_NAMES,
        detach_tag_from_objects_preview,
    ),
    (
        "POST",
        _ASSOCIATION_ROOT + "?action=detach-tag-from-multiple-objects",
        None,
        detach_tag_from_objects_preview,
    ),
    (
        "POST",
        _ASSOCIATION_ROOT + "?action=detach-multiple-tags-from-object",
        None,
        detach_tags_from_object_preview,
    ),
)


def _op_ids(method: str, template: str, names: tuple[str, ...] | None) -> list[str]:
    spellings = names or ("",)
    return [
        f"{method}:{prefix}{template.replace('{id}', '{' + name + '}')}"
        for prefix, name in product(_MOUNT_PREFIXES, spellings)
    ]


def _builders_by_op_id() -> dict[str, Any]:
    return {
        op_id: builder
        for method, template, names, builder in _TAGGING_REMOVALS
        for op_id in _op_ids(method, template, names)
    }


#: Every op_id spelling that carries a tagging-removal builder.
TAGGING_REMOVAL_OP_IDS: Final[frozenset[str]] = frozenset(_builders_by_op_id())


def register_tagging_removal_previews() -> None:
    """Register the five builders under every op_id spelling. Import-time."""
    for op_id, builder in _builders_by_op_id().items():
        register_preview_builder(op_id, builder)


register_tagging_removal_previews()
