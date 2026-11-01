# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Change the CPU and memory settings of one resource pool (REST).

``vmware.composite.resource_pool.update`` (``dangerous``) --
``PATCH /vcenter/resource-pool/{resourcePool}``. vCenter resets every field
of an allocation the request leaves out (no reservation means 0, no limit
means -1, no shares means NORMAL). So MEHO reads the pool first, puts the
asked changes on top of the current CPU or memory allocation, and sends the
whole allocation. An allocation the agent does not name is not sent and stays
as it is. After the change MEHO reads the pool again and returns both states.

The pool is named by its moid. It waits for approval and runs the same way on
vCenter 8 and 9 (the path is in both pinned API files).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final

import httpx

from meho_backplane.connectors.vmware_rest.composite_issues import issue
from meho_backplane.connectors.vmware_rest.composites._spec import CompositeSpec
from meho_backplane.connectors.vmware_rest.composites._write import (
    _parse_vsphere_error,
    _read_sub_op,
    _unwrap_value,
    _vsphere_fault_detail,
    _write_sub_op,
)
from meho_backplane.operations._preview import preview_failure_reason

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors import OperationResult
    from meho_backplane.connectors.vmware_rest.connector import VmwareRestConnector
    from meho_backplane.operations._preview import PreviewContext

__all__ = ["COMPOSITES", "OP_ID_RESOURCE_POOL_UPDATE"]

OP_ID_RESOURCE_POOL_UPDATE: Final = "vmware.composite.resource_pool.update"

_OP_GET_RESOURCE_POOL: Final = "GET:/vcenter/resource-pool/{resourcePool}"
_OP_UPDATE_RESOURCE_POOL: Final = "PATCH:/vcenter/resource-pool/{resourcePool}"

#: The REST calls of the change; the reconcile test checks them against the
#: pinned vCenter 8.0 and 9.0 API files.
_SUB_OPS_RESOURCE_POOL_UPDATE: Final[tuple[str, ...]] = (
    _OP_GET_RESOURCE_POOL,
    _OP_UPDATE_RESOURCE_POOL,
)

_ALLOCATIONS: Final = ("cpu_allocation", "memory_allocation")
_ALLOCATION_FIELDS: Final = ("reservation", "expandable_reservation", "limit", "shares")


def _allocation(info: Any, key: str) -> dict[str, Any] | None:
    """The ``{reservation, expandable_reservation, limit, shares}`` of one allocation."""
    alloc = info.get(key) if isinstance(info, dict) else None
    if not isinstance(alloc, dict):
        return None
    return {k: alloc[k] for k in _ALLOCATION_FIELDS if k in alloc}


def _state(pool: str, info: Any) -> dict[str, Any]:
    """The pool as the answers show it."""
    return {
        "resource_pool": pool,
        "name": info.get("name") if isinstance(info, dict) else None,
        **{key: _allocation(info, key) for key in _ALLOCATIONS},
    }


def _normalized(alloc: dict[str, Any] | None) -> dict[str, Any]:
    """An allocation as MEHO sends it: the shares number only with level CUSTOM.

    vCenter reports the computed shares number for every level, but uses it
    only for CUSTOM. Dropping it for the other levels lets MEHO compare the
    current allocation with the asked one.
    """
    out = dict(alloc or {})
    shares = out.get("shares")
    if isinstance(shares, dict) and shares.get("level") != "CUSTOM":
        out["shares"] = {"level": shares.get("level")}
    return out


def _merged_update(current: dict[str, Any], params: dict[str, Any]) -> dict[str, Any]:
    """The PATCH body: each asked allocation, current values with the changes on top."""
    body: dict[str, Any] = {}
    for key in _ALLOCATIONS:
        change = params.get(key)
        if not isinstance(change, dict):
            continue
        merged = dict(current.get(key) or {})
        merged.update({k: v for k, v in change.items() if v is not None})
        body[key] = _normalized(merged)
    return body


async def _read_pool(
    connector: VmwareRestConnector, target: Any, operator: Operator, pool: str
) -> dict[str, Any] | None:
    """The pool info, or ``None`` when vCenter answers 404."""
    try:
        info = await _read_sub_op(
            connector, target, operator, _OP_GET_RESOURCE_POOL, {"resourcePool": pool}
        )
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 404:
            return None
        raise
    info = _unwrap_value(info)
    return info if isinstance(info, dict) else {}


async def resource_pool_update_composite(
    *,
    operator: Operator,
    target: Any,
    params: dict[str, Any],
    connector: VmwareRestConnector,
) -> dict[str, Any] | OperationResult:
    """Change a pool's CPU / memory allocation. Op-id: ``resource_pool.update``."""
    pool = params["resource_pool"]
    envelope: dict[str, Any] = {
        "resource_pool": pool,
        "before": None,
        "after": None,
        "sent": None,
        "error_type": None,
        "issues": [],
        "guidance": None,
    }
    info = await _read_pool(connector, target, operator, pool)
    if info is None:
        return {
            **envelope,
            "status": "not_found",
            "issues": [issue("input", "error", f"resource pool {pool} does not exist")],
            "guidance": "Nothing changed. Find the id with vmware.resource_pool.list.",
        }
    before = _state(pool, info)
    sent = _merged_update(before, params)
    envelope.update(before=before, sent=sent)
    if all(sent[key] == _normalized(before[key]) for key in sent):
        return {**envelope, "status": "unchanged", "after": before, "guidance": "Nothing sent."}
    try:
        gate, _ = await _write_sub_op(
            connector, target, operator, _OP_UPDATE_RESOURCE_POOL, {"resourcePool": pool, **sent}
        )
    except httpx.HTTPStatusError as exc:
        status, error_type = _parse_vsphere_error(exc)
        if status != 400:
            raise
        return {
            **envelope,
            "status": "refused",
            "error_type": error_type,
            "issues": [issue("input", "error", "vCenter refused the change")],
            "guidance": f"Nothing changed: {_vsphere_fault_detail(exc)}",
        }
    if gate is not None:
        return gate
    after = await _read_pool(connector, target, operator, pool)
    state = None if after is None else _state(pool, after)
    return {**envelope, "status": "updated", "after": state}


async def _resource_pool_update_preview(ctx: PreviewContext) -> dict[str, Any] | None:
    """Show the approver the current allocation and what MEHO will send."""
    pool = ctx.params.get("resource_pool")
    if not isinstance(pool, str) or ctx.connector_instance is None:
        return None
    connector: VmwareRestConnector = ctx.connector_instance  # type: ignore[assignment]
    try:
        info = await _read_pool(connector, ctx.target, ctx.operator, pool)
    except Exception as exc:
        reason = preview_failure_reason(exc)
        return {"summary": "MEHO could not read the pool.", "read_error": reason}
    if info is None:
        return {"summary": f"Resource pool {pool} does not exist; the call will fail."}
    before = _state(pool, info)
    return {
        "summary": f"Change the allocation of resource pool {before['name']!r} ({pool}).",
        "current": before,
        "will_send": _merged_update(before, ctx.params),
    }


# ---------------------------------------------------------------------------
# Schemas, AI guidance and the registration row
# ---------------------------------------------------------------------------

_ALLOCATION_CHANGE_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "reservation": {
            "type": "integer",
            "minimum": 0,
            "description": "Guaranteed amount (MHz for CPU, MB for memory). Omit to keep it.",
        },
        "expandable_reservation": {
            "type": "boolean",
            "description": "Let the reservation grow into the parent's free resources. "
            "Omit to keep it.",
        },
        "limit": {
            "type": "integer",
            "minimum": -1,
            "description": "Upper bound (MHz for CPU, MB for memory); -1 means no limit. "
            "Omit to keep it.",
        },
        "shares": {
            "type": "object",
            "properties": {
                "level": {
                    "type": "string",
                    "enum": ["LOW", "NORMAL", "HIGH", "CUSTOM"],
                    "description": "Shares level; CUSTOM uses the 'shares' number.",
                },
                "shares": {
                    "type": "integer",
                    "minimum": 0,
                    "description": "Number of shares; only used with level CUSTOM.",
                },
            },
            "required": ["level"],
            "if": {"properties": {"level": {"const": "CUSTOM"}}},
            "then": {"required": ["shares"]},
            "additionalProperties": False,
            "description": "Weight under contention. Level CUSTOM needs the 'shares' "
            "number. Omit to keep the current shares.",
        },
    },
    "minProperties": 1,
    "additionalProperties": False,
}

RESOURCE_POOL_UPDATE_PARAMETER_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "resource_pool": {
            "type": "string",
            "pattern": r"^resgroup-[0-9]+$",
            "description": "Resource pool id, such as 'resgroup-42' (from "
            "vmware.resource_pool.list). Not a cluster's root pool.",
        },
        "cpu_allocation": {
            **_ALLOCATION_CHANGE_SCHEMA,
            "description": "CPU changes. Fields left out keep their current value.",
        },
        "memory_allocation": {
            **_ALLOCATION_CHANGE_SCHEMA,
            "description": "Memory changes. Fields left out keep their current value.",
        },
    },
    "required": ["resource_pool"],
    "anyOf": [{"required": ["cpu_allocation"]}, {"required": ["memory_allocation"]}],
    "additionalProperties": False,
    "examples": [
        {
            "resource_pool": "resgroup-42",
            "memory_allocation": {"limit": 16384, "shares": {"level": "HIGH"}},
        }
    ],
}

_NULLABLE_OBJECT: Final = {"type": ["object", "null"]}
_POOL_STATE_SCHEMA: Final[dict[str, Any]] = {
    "type": ["object", "null"],
    "properties": {
        "resource_pool": {"type": "string", "description": "Pool id."},
        "name": {"type": ["string", "null"], "description": "Pool name."},
        "cpu_allocation": {**_NULLABLE_OBJECT, "description": "CPU allocation."},
        "memory_allocation": {**_NULLABLE_OBJECT, "description": "Memory allocation."},
    },
}

RESOURCE_POOL_UPDATE_RESPONSE_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "status": {
            "type": "string",
            "enum": ["updated", "unchanged", "not_found", "refused"],
            "description": "updated: sent and read back; unchanged: already so, nothing "
            "sent; not_found: no such pool; refused: vCenter said no, nothing changed.",
        },
        "resource_pool": {"type": "string", "description": "Pool id."},
        "before": {**_POOL_STATE_SCHEMA, "description": "The pool before the change."},
        "after": {**_POOL_STATE_SCHEMA, "description": "The pool as read back after."},
        "sent": {**_NULLABLE_OBJECT, "description": "The PATCH body MEHO sent."},
        "error_type": {"type": ["string", "null"], "description": "vCenter error type."},
        "issues": {"type": "array", "items": {"type": "object"}, "description": "Problems."},
        "guidance": {"type": ["string", "null"], "description": "What to do next."},
    },
    "required": ["status", "resource_pool"],
}

RESOURCE_POOL_UPDATE_LLM_INSTRUCTIONS: Final[dict[str, Any]] = {
    "when_to_call": (
        "Call to change the CPU or memory reservation, limit or shares of an existing "
        "resource pool. To rename it, use vmware.composite.object.rename."
    ),
    "preconditions": (
        "Get the pool id from vmware.resource_pool.list. Name only the fields to change; "
        "MEHO keeps the others. A reservation must fit in the parent pool. The change "
        "waits for approval."
    ),
    "output_shape": (
        "{status: updated|unchanged|not_found|refused, resource_pool, before: {name, "
        "cpu_allocation, memory_allocation}, after: {...}, sent, error_type, issues, guidance}."
    ),
    "next_step": (
        "Safe to run twice: the same values send nothing. On refused, read guidance (often "
        "a reservation larger than the parent can give) and try smaller values."
    ),
}

#: The registration row; ``_register.py`` collects it at start-up.
COMPOSITES: Final[tuple[CompositeSpec, ...]] = (
    CompositeSpec(
        op_id=OP_ID_RESOURCE_POOL_UPDATE,
        handler=resource_pool_update_composite,
        summary="Change the CPU and memory shares, limits and reservations of a resource pool.",
        description=(
            "Changes the CPU or memory allocation of one resource pool, named by its id. Reads "
            "the pool first and keeps every field you leave out. Returns the pool before and "
            "after. Waits for approval."
        ),
        parameter_schema=RESOURCE_POOL_UPDATE_PARAMETER_SCHEMA,
        response_schema=RESOURCE_POOL_UPDATE_RESPONSE_SCHEMA,
        group_key="cluster",
        tags=["composite", "write", "cluster", "resource-pool"],
        safety_level="dangerous",
        requires_approval=True,
        llm_instructions=RESOURCE_POOL_UPDATE_LLM_INSTRUCTIONS,
        preview=_resource_pool_update_preview,
    ),
)
