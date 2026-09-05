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

from pydantic import ValidationError

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.mcp.registry import ToolDefinition, ToolSurface, register_mcp_tool
from meho_backplane.mcp.server import McpInvalidParamsError
from meho_backplane.operations.result_query import (
    MAX_LIMIT,
    QueryContractError,
    ResultHandleNotFoundError,
    ResultQueryOutputTooLargeError,
    ResultQuerySpec,
    ResultQueryTimeoutError,
    read_result_window,
    run_result_query,
)

__all__: list[str] = []

#: Mirror of the inline-sample upper bound's sibling list tools: a single
#: page returns at most this many rows so one read-back can't pull an
#: unbounded slice in one call. Sourced from the transport-neutral core so
#: the MCP ``inputSchema`` and the REST body share one ceiling (#3179).
_MAX_LIMIT = MAX_LIMIT


async def _result_query_handler(
    operator: Operator,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    """Read back from a spilled handle: page a window, or run a bounded query.

    The MCP dispatcher has already validated ``arguments`` against the
    tool's ``inputSchema``, so the body parses the UUID and branches on the
    optional ``query`` argument:

    * absent → paging mode: delegate the ``[offset : offset+limit]`` window
      to the shared :func:`read_result_window` core (unchanged, #3179).
    * present → query mode: validate it into a
      :class:`~meho_backplane.jsonflux.query.contract.ResultQuerySpec` and
      delegate to :func:`run_result_query`, which compiles it to one bounded
      read-only ``SELECT`` over the handle (#3366).

    Both branches wrap the same tenant/principal scoping and not-found
    semantics. The tenant comes from ``operator.tenant_id`` — the arguments
    carry no tenant, by design.
    """
    raw_handle = arguments["handle_id"]
    try:
        handle_id = UUID(str(raw_handle))
    except (ValueError, TypeError) as exc:
        raise McpInvalidParamsError(
            f"handle_id is not a valid UUID: {raw_handle!r}",
            data={"reason": "invalid_handle_id", "handle_id": str(raw_handle)},
        ) from exc

    raw_query = arguments.get("query")
    if raw_query is not None:
        return await _run_query(operator, handle_id, raw_query)

    offset = int(arguments.get("offset", 0))
    limit = int(arguments.get("limit", 50))

    try:
        return await read_result_window(operator, handle_id, offset, limit)
    except ResultHandleNotFoundError as exc:
        raise _handle_not_found(handle_id) from exc


async def _run_query(
    operator: Operator,
    handle_id: UUID,
    raw_query: Any,
) -> dict[str, Any]:
    """Validate the ``query`` argument and run the bounded structured query.

    Model-shape violations (caps, operator/aggregate allow-lists, unknown
    sub-keys) surface as a pydantic ``ValidationError``; field-vs-schema,
    time-budget, and output-byte violations surface from the core. All map
    to a recoverable ``-32602`` with a machine-readable ``data.reason`` so
    the agent can narrow and retry.
    """
    try:
        spec = ResultQuerySpec.model_validate(raw_query)
    except ValidationError as exc:
        raise McpInvalidParamsError(
            f"invalid query spec: {exc.error_count()} validation error(s); "
            f"first: {exc.errors()[0].get('msg', 'invalid')}",
            data={"reason": "invalid_query"},
        ) from exc
    try:
        return await run_result_query(operator, handle_id, spec)
    except ResultHandleNotFoundError as exc:
        raise _handle_not_found(handle_id) from exc
    except QueryContractError as exc:
        raise McpInvalidParamsError(str(exc), data={"reason": "invalid_query"}) from exc
    except ResultQueryTimeoutError as exc:
        raise McpInvalidParamsError(str(exc), data={"reason": "query_timeout"}) from exc
    except ResultQueryOutputTooLargeError as exc:
        raise McpInvalidParamsError(str(exc), data={"reason": "output_too_large"}) from exc


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
        feature="typed_connector_reads",
        name="result_query",
        surface=ToolSurface.WORKING,
        description=(
            "Read rows back from a JSONFlux result handle. After "
            "`call_operation` reduces a large list response, you get an "
            "inline sample plus a handle (`result.handle.handle_id`); call "
            "this tool to read the FULL set beyond that sample, either by "
            "paging or by running a bounded server-side query. "
            "Paging: pass `handle_id` (required, the UUID from the reduced "
            "response's `handle.handle_id` / the `fetch_more.drill_in."
            "example_call`) plus `offset` (default 0) and `limit` (default "
            "50, max 500); page by re-calling with a higher `offset`, and an "
            "empty `rows` with `offset >= stored_rows` is the end. "
            "Query: pass `handle_id` plus a `query` object to filter, "
            "project (`select`), `group_by`, and `aggregate` "
            "(COUNT/SUM/MIN/MAX/AVG) server-side — so you fetch just the one "
            "row you need or the counts by group, not the whole set. The "
            "query runs as one bounded read-only SELECT over the handle "
            "(no raw SQL); every referenced field must be a column on the "
            "handle. Results carry `total_rows`, `stored_rows` (retrievable "
            "rows — may be below `total_rows` if the spill was capped), "
            "`truncated` (more rows existed than returned), and, for a "
            "query, `coverage` (`complete`/`partial`): a `partial` result "
            "aggregated only the stored subset, not the whole inventory. "
            "A handle that does not exist, has expired (TTL elapsed), or "
            "belongs to another operator is a recoverable error (`-32602`, "
            "`data.reason=handle_not_found`) — re-run the original operation "
            "to get a fresh handle. Only use this when "
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
                        f"Page size (paging mode). Default 50; max {_MAX_LIMIT}. "
                        "Matches the sibling list tools' upper bound."
                    ),
                },
                "query": {
                    "type": "object",
                    "description": (
                        "Optional structured query (query mode). When present, "
                        "`offset`/`limit` are ignored and the handle is queried "
                        "server-side as one bounded read-only SELECT. Every "
                        "referenced field must be a column on the handle."
                    ),
                    "additionalProperties": False,
                    "properties": {
                        "filter": {
                            "type": "array",
                            "maxItems": 10,
                            "description": "Predicates AND-ed into the WHERE clause (max 10).",
                            "items": {
                                "type": "object",
                                "additionalProperties": False,
                                "properties": {
                                    "field": {"type": "string", "minLength": 1},
                                    "op": {
                                        "type": "string",
                                        "enum": ["=", "!=", "<", "<=", ">", ">=", "IN", "IS NULL"],
                                    },
                                    "value": {
                                        "description": (
                                            "Bound as a parameter. Omit for `IS NULL`; "
                                            "a list for `IN`; a scalar otherwise."
                                        ),
                                    },
                                },
                                "required": ["field", "op"],
                            },
                        },
                        "select": {
                            "type": "array",
                            "items": {"type": "string", "minLength": 1},
                            "description": (
                                "Columns to return. Omit for all columns. Not allowed "
                                "with `aggregate`."
                            ),
                        },
                        "group_by": {
                            "type": "array",
                            "maxItems": 4,
                            "items": {"type": "string", "minLength": 1},
                            "description": "Columns to group by (max 4).",
                        },
                        "aggregate": {
                            "type": "array",
                            "description": "Aggregate output columns (COUNT/SUM/MIN/MAX/AVG).",
                            "items": {
                                "type": "object",
                                "additionalProperties": False,
                                "properties": {
                                    "func": {
                                        "type": "string",
                                        "enum": ["COUNT", "SUM", "MIN", "MAX", "AVG"],
                                    },
                                    "field": {
                                        "type": "string",
                                        "minLength": 1,
                                        "description": (
                                            "Column to aggregate; omit only for COUNT (COUNT(*))."
                                        ),
                                    },
                                },
                                "required": ["func"],
                            },
                        },
                        "order_by": {
                            "type": "array",
                            "maxItems": 4,
                            "description": "Sort terms (max 4).",
                            "items": {
                                "type": "object",
                                "additionalProperties": False,
                                "properties": {
                                    "field": {"type": "string", "minLength": 1},
                                    "direction": {
                                        "type": "string",
                                        "enum": ["asc", "desc"],
                                        "default": "asc",
                                    },
                                },
                                "required": ["field"],
                            },
                        },
                        "limit": {
                            "type": "integer",
                            "minimum": 1,
                            "description": (
                                f"Max output rows; clamps to {_MAX_LIMIT}. "
                                "The result flags `truncated` when more rows existed."
                            ),
                        },
                    },
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
                "coverage": {
                    "type": "string",
                    "enum": ["complete", "partial"],
                    "description": (
                        "Query mode only. `partial` when the spill was capped "
                        "(`stored_rows < total_rows`) so the query covered only the "
                        "stored subset — a count/aggregate is not a whole-inventory total."
                    ),
                },
                "coverage_note": {
                    "type": ["string", "null"],
                    "description": "Query mode only. Human-readable coverage caveat when partial.",
                },
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
