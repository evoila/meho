# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``query_audit`` — single MCP meta-tool for forensic audit-log queries (G8.1-T4).

The CLAUDE.md "narrow-waist agent surface" postulate (#5) says the agent
sees ONE tool for audit data, not a per-shape family. The pre-canned
shortcuts (``audit.show`` / ``audit.who_touched`` / ``audit.my_recent``)
live in the CLI (T3) and the REST router (T2) only; on MCP the agent
expresses every shape as a filter combination on ``query_audit``.

Dispatches through the T1 :func:`~meho_backplane.audit_query.query_audit`
substrate; tenant scope comes from the validated :class:`Operator`'s
JWT — never from the arguments dict — so a cross-tenant agent probe is
structurally impossible.

``since`` / ``until`` accept either ISO-8601 absolute strings or the
duration shorthand (``"24h"`` / ``"7d"`` / ``"30m"`` / ``"2w"``) the T2
router introduced; parsing happens at the tool boundary via
:func:`~meho_backplane.audit_query.parse_duration`, matching the REST
surface's contract.

Audit-on-audit-query
====================

The MCP dispatcher (``mcp/handlers.py``) writes one ``audit_log`` row
per ``tools/call`` invocation. For this tool the row carries:

* ``op_id = "query_audit"`` — the tool name verbatim (dispatcher
  convention). Operators forensically searching ``audit_log`` for
  "everyone who queried the audit log via MCP" filter on
  ``payload->>'op_id' = 'query_audit'``. Note the REST surface emits
  ``op_id = "meho.audit.query"`` — same logical operation, different
  identifier per dispatch path; a v0.2.next unification surface.
* ``op_class = "audit_query"`` — the declarative contract on the
  :class:`ToolDefinition` below. The chassis broadcast classifier's
  aggregate-only policy keys off this field per
  ``broadcast/events.py::redact_payload``.

inputSchema
===========

Hand-built JSON Schema 2020-12 rather than
``AuditQueryFilters.model_json_schema()``: the substrate filter uses
``datetime`` for ``since`` / ``until``, but agents pass duration
shorthand (``"24h"``) which would fail jsonschema validation against a
``format: date-time`` constraint. The hand-built schema accepts
``string`` for both fields and the handler parses inside. Field
``max_length`` mirrors :class:`AuditQueryRequest` in
:mod:`~meho_backplane.api.v1.audit_models` so REST and MCP enforce the
same per-field bounds.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Final

from pydantic import ValidationError

from meho_backplane.audit_query import (
    AuditQueryFilters,
    DurationParseError,
    InvalidCursorError,
    UnsupportedFilterError,
    parse_duration,
    query_audit,
)
from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.mcp.registry import ToolDefinition, register_mcp_tool
from meho_backplane.mcp.server import McpInvalidParamsError

__all__: list[str] = []


_TOOL_NAME: Final[str] = "query_audit"


_INPUT_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "target": {
            "type": ["string", "null"],
            "description": (
                "Target name or alias. Substrate matches against "
                "`targets.name` scoped to the operator's tenant; an "
                "unknown name returns zero rows, not an error."
            ),
            "maxLength": 256,
        },
        "principal": {
            "type": ["string", "null"],
            "description": (
                "Operator sub or partial-name substring. Matches "
                "`audit_log.operator_sub` via case-insensitive LIKE."
            ),
            "maxLength": 256,
        },
        "op_id": {
            "type": ["string", "null"],
            "description": (
                "Glob pattern matched against the op_id (e.g. "
                '"vsphere.vm.*"). Translates to SQL LIKE with `*` as the '
                "wildcard."
            ),
            "maxLength": 256,
        },
        "op_class": {
            "type": ["string", "null"],
            "description": (
                'One of "read" / "write" / "credential_read" / '
                '"audit_query" / "other". Exact match.'
            ),
            "maxLength": 64,
        },
        "result_status": {
            "type": ["string", "null"],
            "description": (
                'One of "ok" / "error" / "denied". Maps to status-code '
                "ranges on the substrate side."
            ),
            "maxLength": 16,
        },
        "since": {
            "type": ["string", "null"],
            "description": (
                "Window start. Either ISO-8601 absolute "
                '("2026-05-14T00:00:00Z") or duration shorthand '
                '("30s" / "5m" / "24h" / "7d" / "2w"). Resolved at '
                "the tool boundary; substrate sees an absolute datetime."
            ),
            "maxLength": 32,
        },
        "until": {
            "type": ["string", "null"],
            "description": ("Window end. Same grammar as `since`."),
            "maxLength": 32,
        },
        "audit_id": {
            "type": ["string", "null"],
            "format": "uuid",
            "description": (
                "Exact-id lookup. Combined with the tenant boundary, produces 0 or 1 row."
            ),
        },
        "parent_audit_id": {
            "type": ["string", "null"],
            "format": "uuid",
            "description": (
                "Filter to one composite-operation subtree. NOT supported "
                "in v0.2 — the substrate raises UnsupportedFilterError "
                "(returned as -32602) until the column lands with G0.6-T7."
            ),
        },
        "agent_session_id": {
            "type": ["string", "null"],
            "format": "uuid",
            "description": (
                "Filter to one agent session's audit trail. NOT supported "
                "in v0.2 — returned as -32602 until a schema column lands."
            ),
        },
        "limit": {
            "type": "integer",
            "minimum": 1,
            "maximum": 1000,
            "default": 100,
            "description": "Page size. Default 100; max 1000.",
        },
        "cursor": {
            "type": ["string", "null"],
            "description": (
                "Opaque forward-pagination cursor from a previous result's "
                "`next_cursor`. Tampered cursors return -32602."
            ),
            "maxLength": 512,
        },
    },
    "additionalProperties": False,
}


_TOOL_DESCRIPTION: Final[str] = (
    "Query the audit log for forensic reconstruction. The canonical answer "
    'to "who did X to Y and when?" — every authenticated operation against '
    "the backplane (HTTP route, MCP tool call) writes one audit row, and "
    "this tool is how an agent reads them back.\n\n"
    "WHEN TO CALL: investigation flows — the operator asks 'who patched "
    "rdc-vcenter on Tuesday', 'did someone read vault.kv before the outage', "
    "'show me every denied request in the last 24h'. Pre-canned shortcut "
    "shapes (`audit_id=<uuid>` for show, `target=<name>` for who-touched, "
    "`principal=<operator.sub>` for my-recent) are filter combinations, not "
    "separate tools.\n\n"
    "WHEN NOT TO CALL: this is forensic reconstruction over the rolling "
    "audit log, not live telemetry. For 'what is the system doing right "
    "now' use the broadcast feed; for 'how often does retrieval happen' use "
    "`meho.retrieval.usage`. Calling `query_audit` with no filters returns "
    "the most recent 100 rows of the operator's tenant — not unbounded.\n\n"
    "Tenant scoping is automatic — the operator's JWT determines the tenant "
    "boundary; cross-tenant probes are impossible. Filter contents are "
    "NEVER broadcast on the SSE feed (decision #3); only "
    "`{op_id, result_status, row_count}` aggregate appears.\n\n"
    "Returns `{rows: [AuditEntry, ...], next_cursor: <opaque|null>}` sorted "
    "by timestamp descending. Use `next_cursor` (when non-null) to page "
    "forward; `null` means the page is the end of the matching set under "
    "the current filter."
)


async def _query_audit_handler(
    operator: Operator,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    """Dispatch a ``query_audit`` MCP call through the T1 substrate.

    The dispatcher has already jsonschema-validated *arguments* against
    :data:`_INPUT_SCHEMA`, so every field is the right wire-shape (string
    or null for the parser-bound ones, integer-in-range for ``limit``).
    What still has to happen here:

    1. Parse ``since`` / ``until`` shorthand via :func:`parse_duration`.
       The schema accepts any string; runtime parsing surfaces the
       parser's structured rejection message via
       :class:`McpInvalidParamsError` (→ JSON-RPC ``-32602``).
    2. Construct :class:`AuditQueryFilters` via
       :meth:`pydantic.BaseModel.model_validate`. Pydantic coerces UUID
       strings to :class:`uuid.UUID` automatically. A residual
       :class:`pydantic.ValidationError` (e.g. malformed UUID slipping
       past ``format: uuid``) surfaces as ``-32602``.
    3. Open a session, call :func:`query_audit`. Substrate errors
       (:class:`InvalidCursorError`, :class:`UnsupportedFilterError`)
       are operator-actionable and surface as ``-32602``; anything else
       propagates and the dispatcher turns it into ``-32603``.
    4. Return the result as a JSON-safe dict via
       :meth:`AuditQueryResult.model_dump`.
    """
    now = datetime.now(UTC)
    materialized: dict[str, Any] = dict(arguments)
    try:
        if materialized.get("since") is not None:
            materialized["since"] = parse_duration(materialized["since"], now=now)
        if materialized.get("until") is not None:
            materialized["until"] = parse_duration(materialized["until"], now=now)
    except DurationParseError as exc:
        raise McpInvalidParamsError(str(exc)) from exc

    try:
        filters = AuditQueryFilters.model_validate(materialized)
    except ValidationError as exc:
        raise McpInvalidParamsError(
            f"query_audit: filter validation failed: {exc.error_count()} error(s)",
        ) from exc

    sessionmaker = get_sessionmaker()
    try:
        async with sessionmaker() as session:
            result = await query_audit(
                filters,
                tenant_id=operator.tenant_id,
                session=session,
            )
    except (InvalidCursorError, UnsupportedFilterError) as exc:
        raise McpInvalidParamsError(str(exc)) from exc

    return result.model_dump(mode="json")


register_mcp_tool(
    definition=ToolDefinition(
        name=_TOOL_NAME,
        description=_TOOL_DESCRIPTION,
        inputSchema=_INPUT_SCHEMA,
        outputSchema={
            "type": "object",
            "properties": {
                "rows": {
                    "type": "array",
                    "items": {"type": "object"},
                    "description": (
                        "AuditEntry rows sorted by timestamp descending. "
                        "See `meho_backplane.audit_query.schemas.AuditEntry` "
                        "for the per-row field set."
                    ),
                },
                "next_cursor": {
                    "type": ["string", "null"],
                    "description": (
                        "Opaque forward-pagination cursor. Null when the "
                        "page is the end of the matching set."
                    ),
                },
            },
            "required": ["rows", "next_cursor"],
        },
        required_role=TenantRole.OPERATOR,
        op_class="audit_query",
    ),
    handler=_query_audit_handler,
)
