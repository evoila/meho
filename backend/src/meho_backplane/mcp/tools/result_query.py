# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``result_query`` -- the JSONFlux handle read-back MCP meta-tool (G0.20-T7).

When ``call_operation`` reduces a large set-shaped response, the agent
receives an inline sample of a few rows plus a
:class:`~meho_backplane.connectors.schemas.ResultHandle`. The full set is
spilled to the
:class:`~meho_backplane.connectors.result_handle_store.ResultHandleStore`
(Valkey) keyed by ``(tenant_id, handle_id)`` with a bounded TTL. This tool
is the read surface over that store: an agent that needs rows beyond the
inline sample calls ``result_query(handle_id, offset, limit)`` and gets the
requested window.

Isolation
=========

The tenant is taken from the operator's authenticated identity (the MCP
dispatcher resolves it from the JWT), **never** from the arguments — a
cross-tenant probe cannot read another tenant's handle. The store
additionally checks the spilling operator's ``sub`` so another operator in
the same tenant gets the same "not found" miss as a stranger, leaking no
existence signal across the operator boundary.

Not-found is recoverable
========================

A handle that is unknown, expired (TTL elapsed), or belongs to a different
operator surfaces as a typed ``-32602`` with
``data.reason=handle_not_found`` — the same recoverable-error taxonomy the
operation meta-tools use (#1482). The agent learns the handle is gone (re-run
the operation) rather than getting an opaque internal error.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors.result_handle_store import get_result_handle_store
from meho_backplane.mcp.registry import ToolDefinition, register_mcp_tool
from meho_backplane.mcp.server import McpInvalidParamsError

__all__: list[str] = []

#: Mirror of the inline-sample upper bound's sibling list tools: a single
#: page returns at most this many rows so one read-back can't pull an
#: unbounded slice in one call. Matches the ``search_operations`` /
#: ``list_targets`` max-page convention.
_MAX_LIMIT = 500


async def _result_query_handler(
    operator: Operator,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    """Return a ``[offset : offset+limit]`` window of a spilled handle.

    The MCP dispatcher has already validated ``arguments`` against the
    tool's ``inputSchema`` (``handle_id`` required, ``offset`` / ``limit``
    bounded), so the body parses the UUID and fetches the window. The
    tenant comes from ``operator.tenant_id`` — the arguments carry no
    tenant, by design.
    """
    raw_handle = arguments["handle_id"]
    try:
        handle_id = UUID(str(raw_handle))
    except (ValueError, TypeError) as exc:
        raise McpInvalidParamsError(
            f"handle_id is not a valid UUID: {raw_handle!r}",
            data={"reason": "invalid_handle_id", "handle_id": str(raw_handle)},
        ) from exc

    if operator.tenant_id is None:
        # An operator with no tenant can never own a spilled handle
        # (the reducer keys the spill on tenant_id). Treat as not-found
        # rather than leaking a distinct "no tenant" signal.
        raise _handle_not_found(handle_id)

    offset = int(arguments.get("offset", 0))
    limit = int(arguments.get("limit", 50))

    window = await get_result_handle_store().fetch_window(
        tenant_id=operator.tenant_id,
        operator_sub=operator.sub,
        handle_id=handle_id,
        offset=offset,
        limit=limit,
    )
    if window is None:
        raise _handle_not_found(handle_id)

    return {
        "handle_id": str(handle_id),
        "rows": window.rows,
        "offset": offset,
        "limit": limit,
        "returned_rows": len(window.rows),
        "total_rows": window.total_rows,
        "stored_rows": window.stored_rows,
        "truncated": window.truncated,
    }


def _handle_not_found(handle_id: UUID) -> McpInvalidParamsError:
    """Build the typed not-found / expired error for an unreadable handle."""
    return McpInvalidParamsError(
        (
            f"handle {handle_id} is not readable: it does not exist, has "
            "expired, or belongs to a different operator. Re-run the "
            "operation to get a fresh handle."
        ),
        data={"reason": "handle_not_found", "handle_id": str(handle_id)},
    )


register_mcp_tool(
    definition=ToolDefinition(
        name="result_query",
        description=(
            "Read rows back from a JSONFlux result handle. After "
            "`call_operation` reduces a large list response, you get an "
            "inline sample plus a handle (`result.handle.handle_id`); call "
            "this tool to page through the FULL set beyond that sample. "
            "Arguments: `handle_id` (required, the UUID from the reduced "
            "response's `handle.handle_id` / the `fetch_more.drill_in."
            "example_call`), `offset` (default 0), `limit` (default 50, "
            "max 500). Returns the requested window plus `total_rows` "
            "(the full collection size), `stored_rows` (how many rows are "
            "retrievable — may be less than `total_rows` if the spill was "
            "capped), and `truncated`. Page by re-calling with a higher "
            "`offset`; an empty `rows` with `offset >= stored_rows` is the "
            "end. A handle that does not exist, has expired (TTL elapsed), "
            "or belongs to another operator is a recoverable error "
            "(`-32602`, `data.reason=handle_not_found`) — re-run the "
            "original operation to get a fresh handle. Only use this when "
            "`fetch_more.drill_in.available` is `true` on the handle; when "
            "it is `false` the full set was not spilled and you must "
            "re-call the operation with narrower params instead."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "handle_id": {
                    "type": "string",
                    "description": (
                        "The result handle's UUID, taken from the reduced "
                        "response's `handle.handle_id` or "
                        "`fetch_more.drill_in.example_call.args.handle_id`."
                    ),
                    "minLength": 1,
                },
                "offset": {
                    "type": "integer",
                    "minimum": 0,
                    "default": 0,
                    "description": (
                        "Zero-based index of the first row to return. Page "
                        "by advancing this by the previous `limit`."
                    ),
                },
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": _MAX_LIMIT,
                    "default": 50,
                    "description": (
                        f"Page size. Default 50; max {_MAX_LIMIT}. Matches "
                        "the sibling list tools' upper bound."
                    ),
                },
            },
            "required": ["handle_id"],
            "additionalProperties": False,
        },
        outputSchema={
            "type": "object",
            "properties": {
                "handle_id": {"type": "string"},
                "rows": {"type": "array", "items": {"type": "object"}},
                "offset": {"type": "integer", "minimum": 0},
                "limit": {"type": "integer", "minimum": 1},
                "returned_rows": {"type": "integer", "minimum": 0},
                "total_rows": {"type": "integer", "minimum": 0},
                "stored_rows": {"type": "integer", "minimum": 0},
                "truncated": {"type": "boolean"},
            },
            "required": [
                "handle_id",
                "rows",
                "offset",
                "limit",
                "returned_rows",
                "total_rows",
                "stored_rows",
                "truncated",
            ],
        },
        required_role=TenantRole.OPERATOR,
        op_class="read",
    ),
    handler=_result_query_handler,
)
