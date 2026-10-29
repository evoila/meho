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
from meho_backplane.jsonflux.query.json_pointer import MAX_POINTER_LENGTH
from meho_backplane.mcp.registry import ToolDefinition, ToolSurface, register_mcp_tool
from meho_backplane.mcp.server import McpInvalidParamsError, McpRateLimitedError
from meho_backplane.operations.result_query import (
    DEFAULT_LIMIT,
    MAX_LIMIT,
    QueryContractError,
    ResultHandleNotFoundError,
    ResultQueryOutputTooLargeError,
    ResultQuerySpec,
    ResultQueryTimeoutError,
    ResultReadRejectedError,
    read_result_window,
    run_result_query,
)

__all__: list[str] = []

#: Mirror of the inline-sample upper bound's sibling list tools: a single
#: page returns at most this many rows so one read-back can't pull an
#: unbounded slice in one call. Sourced from the transport-neutral core,
#: which checks it, so the MCP tool and the REST body share one ceiling
#: (#3179). The schema names it but does not pin it: a text value read by
#: ``path`` counts characters, and one part may hold more (#4994).
_MAX_LIMIT = MAX_LIMIT
_DEFAULT_LIMIT = DEFAULT_LIMIT


async def _result_query_handler(
    operator: Operator,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    """Read back from a spilled handle: page a window, or run a bounded query.

    The MCP dispatcher has already validated ``arguments`` against the
    tool's ``inputSchema``, so the body parses the UUID and branches on the
    optional ``query`` argument:

    * absent → paging mode: delegate the ``[offset : offset+limit]`` window
      to the shared :func:`read_result_window` core (#3179). A page that
      runs past the time budget (#3640) is the same recoverable
      ``query_timeout`` as a query.
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
            f"handle_id is not a valid UUID: {raw_handle!r}. "
            "`result_query` reads stored results only. To read a knowledge "
            "entry, call `search_knowledge` with `slug`.",
            data={"reason": "invalid_handle_id", "handle_id": str(raw_handle)},
        ) from exc

    offset = int(arguments.get("offset", 0))
    raw_limit = arguments.get("limit")
    limit = int(raw_limit) if raw_limit is not None else None

    raw_query = arguments.get("query")
    if raw_query is not None:
        return await _run_query(operator, handle_id, raw_query, offset, limit)

    try:
        return await read_result_window(operator, handle_id, offset, limit)
    except ResultHandleNotFoundError as exc:
        raise _handle_not_found(handle_id) from exc
    except QueryContractError as exc:
        raise McpInvalidParamsError(str(exc), data={"reason": "invalid_query"}) from exc
    except ResultQueryTimeoutError as exc:
        raise McpInvalidParamsError(str(exc), data={"reason": "query_timeout"}) from exc
    except ResultQueryOutputTooLargeError as exc:
        raise McpInvalidParamsError(
            str(exc), data={"reason": "output_too_large", "delivery": "unavailable"}
        ) from exc
    except ResultReadRejectedError as exc:
        raise _read_rejected(exc) from exc


async def _run_query(
    operator: Operator,
    handle_id: UUID,
    raw_query: Any,
    offset: int,
    limit: int | None,
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
        return await run_result_query(operator, handle_id, spec, offset=offset, limit=limit)
    except ResultHandleNotFoundError as exc:
        raise _handle_not_found(handle_id) from exc
    except QueryContractError as exc:
        raise McpInvalidParamsError(str(exc), data={"reason": "invalid_query"}) from exc
    except ResultQueryTimeoutError as exc:
        raise McpInvalidParamsError(str(exc), data={"reason": "query_timeout"}) from exc
    except ResultQueryOutputTooLargeError as exc:
        raise McpInvalidParamsError(
            str(exc), data={"reason": "output_too_large", "delivery": "unavailable"}
        ) from exc
    except ResultReadRejectedError as exc:
        raise _read_rejected(exc) from exc


def _read_rejected(exc: ResultReadRejectedError) -> McpRateLimitedError:
    """Build the recoverable ``-32000`` error for a refused read (#3640)."""
    return McpRateLimitedError(
        str(exc),
        data={"reason": exc.reason, "retry_after_seconds": exc.retry_after_seconds},
    )


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
            "Read the full data behind a result handle. When "
            "`call_operation` (or `list_targets`) returns a large list, you "
            "get a sample plus `handle.handle_id`; use this tool to read "
            "the rest. Only when `fetch_more.drill_in.available` is `true`; "
            "when it is `false`, call the operation again with narrower "
            "params.\n\n"
            "PAGING: `handle_id` plus `offset` (default 0) and `limit`. An "
            "empty `rows` with `offset >= stored_rows` is the end.\n\n"
            "QUERY: `handle_id` plus a `query` object to filter, `select`, "
            "`group_by` and `aggregate` on the server, so you fetch only "
            "the rows or counts you need. Every field must be a column on "
            "the handle. If `coverage` is `partial`, only the stored rows "
            "were counted, not the whole set.\n\n"
            'DOCUMENT: when `result_record` is `"document"` (one large '
            'object), read one part with `query={"path": "/key/0/name"}`. '
            "A text value comes back in parts: continue with `offset` = "
            "`offset` + `returned_chars` while `truncated` is true. Also use "
            "`path` for fields a rows handle left out of its sample "
            "(`omitted_scalar_count`).\n\n"
            "`data.reason=handle_not_found` means the handle expired or is "
            "not yours: run the operation again for a new one."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "handle_id": {
                    "type": "string",
                    "description": "The handle's UUID (`handle.handle_id`).",
                    "minLength": 1,
                },
                "offset": {
                    "type": "integer",
                    "minimum": 0,
                    "default": 0,
                    "description": "Index of the first row (or character) to return.",
                },
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "description": (
                        f"Page size in rows (default {_DEFAULT_LIMIT}, max "
                        f"{_MAX_LIMIT}), or in characters for a text value read by `path`."
                    ),
                },
                "query": {
                    "type": "object",
                    "description": (
                        "Optional query. Then `offset`/`limit` are ignored, except with `path`."
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
                                            "a list for `IN` (max 1000 elements); a "
                                            "scalar otherwise."
                                        ),
                                    },
                                },
                                "required": ["field", "op"],
                            },
                        },
                        "select": {
                            "type": "array",
                            "maxItems": 64,
                            "items": {"type": "string", "minLength": 1},
                            "description": (
                                "Columns to return (max 64). Omit for all columns. Not "
                                "allowed with `aggregate`."
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
                            "maxItems": 8,
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
                        "path": {
                            "type": "string",
                            "maxLength": MAX_POINTER_LENGTH,
                            "description": (
                                "RFC 6901 JSON Pointer to the value to read, e.g. "
                                '`/status/conditions/0`; `""` is the whole '
                                "document. Cannot be combined with the other "
                                "query fields."
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
                "path": {
                    "type": "string",
                    "description": "Path reads only. The JSON Pointer that was read.",
                },
                "value_type": {
                    "type": "string",
                    "enum": ["object", "array", "string", "number", "boolean", "null"],
                    "description": (
                        "Path reads only. The JSON type of the value at `path`. "
                        "An `array` pages in `rows`; a `string` comes back in "
                        "parts in `value`; any other type is whole in `value`."
                    ),
                },
                "value": {
                    "description": (
                        "Path reads only, when `value_type` is not `array`: the value "
                        "at `path`. For a `string`, the part from character `offset`."
                    ),
                },
                "returned_chars": {
                    "type": "integer",
                    "minimum": 0,
                    "description": (
                        "Path reads of a text value only. How many characters "
                        "`value` holds. Continue with `offset` + `returned_chars`."
                    ),
                },
                "total_chars": {
                    "type": "integer",
                    "minimum": 0,
                    "description": (
                        "Path reads of a text value only. The full length of the "
                        "text, in characters."
                    ),
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
