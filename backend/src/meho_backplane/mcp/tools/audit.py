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

import uuid
from datetime import UTC, datetime
from typing import Any, Final

import sqlalchemy as sa
import structlog
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.audit_query import (
    AuditQueryFilters,
    DurationParseError,
    InvalidCursorError,
    UnsupportedFilterError,
    parse_duration,
    query_audit,
    replay_session,
)
from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AuditLog
from meho_backplane.mcp.registry import ToolDefinition, register_mcp_tool
from meho_backplane.mcp.server import McpInvalidParamsError

__all__: list[str] = []

_log = structlog.get_logger(__name__)

_TOOL_NAME: Final[str] = "query_audit"

#: Hard cap on the number of audit rows a single replay may reconstruct.
#: A pathological session (a long-running agent that fanned out thousands
#: of operations) would otherwise build a multi-megabyte ``ReplayNode``
#: tree and blow the agent's context window — exactly the failure the
#: JSONFlux result-handle discipline exists to prevent. The MCP transport
#: has no streaming body, so the guard is count-first: the closure size is
#: counted before the (expensive) tree assembly runs, and an over-cap
#: session is rejected with a JSON-RPC ``-32602`` carrying the
#: ``session_too_large`` token (the MCP analogue of T4's REST 413). The
#: cap matches T4's REST route so an operator sees the same boundary on
#: both surfaces.
_REPLAY_MAX_ROWS: Final[int] = 10_000

_REPLAY_DEFAULT_MAX_DEPTH: Final[int] = 20

#: Name of the structlog contextvar the MCP transport binds the inbound
#: ``Mcp-Session-Id`` header into (G8.2-T2 #1010, via
#: :func:`~meho_backplane.mcp.server._bind_mcp_session_id`). The
#: ``shape="tree"`` self-session check reads this back to prove the
#: caller is replaying their *own* session — the same value that lands on
#: ``audit_log.agent_session_id`` for rows this request writes.
_MCP_SESSION_CONTEXTVAR: Final[str] = "mcp_session_id"


_INPUT_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "target": {
            "type": ["string", "null"],
            "description": (
                "Target name or alias. Substrate matches against "
                "`targets.name` scoped to the operator's tenant; an "
                "unknown name returns zero rows, not an error. "
                "NOTE: this tool's `target` is a bare string (read "
                "tools only need the name); `call_operation` wraps "
                "the same concept in a `{name: ...}` dict. See "
                "`docs/architecture/mcp.md` ('Target-reference shape "
                "convention')."
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
                "Filter to one agent session's audit trail "
                "(`audit_log.agent_session_id`, the MCP-session "
                "correlation id). Supported as a flat filter. Also "
                'REQUIRED when `shape="tree"` — see `shape`.'
            ),
        },
        "shape": {
            "type": ["string", "null"],
            "enum": ["flat", "tree", None],
            "description": (
                "Result shape. `flat` (default) returns the paginated "
                "`{rows, next_cursor}` list. `tree` reconstructs the "
                "agent session as a parent/child `ReplayNode` forest "
                "(every operation plus the lineage graph between them) "
                "and returns `{root, session_id, tenant_id, row_count}`. "
                "`tree` is SELF-SESSION ONLY: `agent_session_id` must be "
                "present and equal to the caller's own MCP session id; "
                "any other value (or absence) is rejected with -32602. "
                "To replay a DIFFERENT session, a tenant_admin uses the "
                "`meho.audit.replay` tool."
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
    "the current filter.\n\n"
    'REPLAY YOUR OWN SESSION: pass `shape="tree"` together with '
    "`agent_session_id=<your own session id>` to reconstruct the full "
    "parent/child trace of your own agent session instead of a flat list. "
    "The tree path is self-session-only; replaying another session needs "
    "the tenant_admin `meho.audit.replay` tool."
)


def _coerce_max_depth(raw: Any) -> int:
    """Validate the optional ``max_depth`` argument, defaulting to 20.

    The inputSchema constrains ``max_depth`` to a bounded integer, so a
    well-formed client never reaches the rejection branch; the explicit
    check is defence-in-depth against a future schema drift. ``None`` /
    absent → the substrate default.
    """
    if raw is None:
        return _REPLAY_DEFAULT_MAX_DEPTH
    if not isinstance(raw, int) or isinstance(raw, bool):
        raise McpInvalidParamsError("max_depth must be a positive integer")
    if raw < 1:
        raise McpInvalidParamsError("max_depth must be a positive integer")
    return raw


async def _count_session_rows(
    session_id: uuid.UUID,
    *,
    tenant_id: uuid.UUID,
    session: AsyncSession,
) -> int:
    """Count the anchor rows for one tenant-scoped session.

    Count-first guard for the replay path: the recursive-CTE closure can
    only be *larger* than the anchor set (it adds lineage descendants),
    so counting the cheap anchor predicate first is a sound lower bound —
    if even the anchors exceed :data:`_REPLAY_MAX_ROWS` the full tree is
    certainly over-cap, and we reject before paying for the recursive
    walk + Python tree assembly. The tenant predicate mirrors
    :func:`~meho_backplane.audit_query.replay.replay_session`'s anchor so
    a cross-tenant session id counts zero.
    """
    stmt = sa.select(sa.func.count(AuditLog.id)).where(
        AuditLog.agent_session_id == session_id,
        AuditLog.tenant_id == tenant_id,
    )
    return int((await session.execute(stmt)).scalar_one())


async def _build_replay_response(
    session_id: uuid.UUID,
    *,
    tenant_id: uuid.UUID,
    max_depth: int,
) -> dict[str, Any]:
    """Count-guard, reconstruct, and JSON-serialise one session's replay tree.

    Shared by the ``meho.audit.replay`` admin tool and the
    ``query_audit`` ``shape="tree"`` self-session branch. Opens a
    transient session, applies the :data:`_REPLAY_MAX_ROWS` count-first
    guard (surfaced as a JSON-RPC ``-32602`` with the
    ``session_too_large`` token — the MCP analogue of T4's REST 413, the
    transport has no streaming body for a 413-style partial), dispatches
    through :func:`replay_session`, and returns the
    ``{root, session_id, tenant_id, row_count}`` envelope. ``row_count``
    is the assembled-node count (post depth-cap), so it reflects what the
    caller actually receives.
    """
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        anchor_count = await _count_session_rows(
            session_id,
            tenant_id=tenant_id,
            session=session,
        )
        if anchor_count > _REPLAY_MAX_ROWS:
            _log.warning(
                "audit_replay_session_too_large",
                session_id=str(session_id),
                anchor_count=anchor_count,
                cap=_REPLAY_MAX_ROWS,
            )
            raise McpInvalidParamsError(
                f"session_too_large: session has {anchor_count} audit rows, "
                f"exceeding the {_REPLAY_MAX_ROWS}-row replay cap",
            )
        root = await replay_session(
            session_id,
            tenant_id=tenant_id,
            session=session,
            max_depth=max_depth,
        )

    serialized_root = [node.model_dump(mode="json") for node in root]
    return {
        "root": serialized_root,
        "session_id": str(session_id),
        "tenant_id": str(tenant_id),
        "row_count": _count_tree_nodes(serialized_root),
    }


def _count_tree_nodes(nodes: list[dict[str, Any]]) -> int:
    """Total node count across the serialised replay forest (recursive)."""
    total = 0
    for node in nodes:
        total += 1
        total += _count_tree_nodes(node.get("children", []))
    return total


def _resolve_self_session_id(arguments: dict[str, Any]) -> uuid.UUID:
    """Resolve + authorise the ``shape="tree"`` self-session contract.

    The ``tree`` shape replays ONLY the caller's own session. This is
    intentionally STRICTER than ``query_audit``'s flat path, which
    already returns other in-tenant principals' rows: cross-session
    forensic replay is reserved for the ``tenant_admin``-gated
    ``meho.audit.replay`` tool, so the operator-role tree path is locked
    to "replay your own trace". Two conditions must hold:

    1. ``agent_session_id`` is present in the arguments. Absence → -32602.
    2. It equals the caller's bound MCP session id (the
       :data:`_MCP_SESSION_CONTEXTVAR` the transport binds from the
       inbound ``Mcp-Session-Id`` header). Any mismatch → -32602 —
       including the case where the request carried no session header at
       all, since the transport then binds a fresh per-call uuid4 that
       the client cannot have predicted.
    """
    requested = arguments.get("agent_session_id")
    if requested is None:
        raise McpInvalidParamsError(
            'shape="tree" requires agent_session_id (your own session id)',
        )
    try:
        requested_id = uuid.UUID(str(requested))
    except (TypeError, ValueError) as exc:
        raise McpInvalidParamsError(
            f"agent_session_id is not a valid UUID: {requested!r}",
        ) from exc

    bound_raw = structlog.contextvars.get_contextvars().get(_MCP_SESSION_CONTEXTVAR)
    bound_id: uuid.UUID | None = None
    if isinstance(bound_raw, str):
        try:
            bound_id = uuid.UUID(bound_raw)
        except ValueError:
            bound_id = None
    if bound_id is None or bound_id != requested_id:
        raise McpInvalidParamsError(
            'shape="tree" replays only your own session: agent_session_id '
            "must equal the caller's own MCP session id",
        )
    return requested_id


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

    ``shape="tree"`` short-circuits steps 1-4: it reconstructs the
    caller's own session as a :class:`ReplayNode` forest via
    :func:`_build_replay_response` after the self-session check in
    :func:`_resolve_self_session_id`. ``shape`` is a tool-boundary
    control, not a substrate filter, so it is stripped before
    :class:`AuditQueryFilters` is constructed on the flat path. The tree
    path uses the substrate's default depth cap — ``query_audit`` does
    not expose a ``max_depth`` knob (that belongs to the dedicated
    ``meho.audit.replay`` admin tool).
    """
    materialized: dict[str, Any] = dict(arguments)
    shape = materialized.pop("shape", None)
    if shape == "tree":
        session_id = _resolve_self_session_id(arguments)
        return await _build_replay_response(
            session_id,
            tenant_id=operator.tenant_id,
            max_depth=_REPLAY_DEFAULT_MAX_DEPTH,
        )

    now = datetime.now(UTC)
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


# ---------------------------------------------------------------------------
# meho.audit.replay — tenant_admin cross-session forensic replay
# ---------------------------------------------------------------------------


_REPLAY_TOOL_NAME: Final[str] = "meho.audit.replay"


_REPLAY_INPUT_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "session_id": {
            "type": "string",
            "format": "uuid",
            "description": (
                "The agent_session_id to reconstruct. Matched against "
                "`audit_log.agent_session_id` scoped to the operator's "
                "tenant; a session id from another tenant returns an "
                "empty root, never another tenant's rows."
            ),
        },
        "max_depth": {
            "type": "integer",
            "minimum": 1,
            "maximum": 100,
            "default": _REPLAY_DEFAULT_MAX_DEPTH,
            "description": (
                "Defensive cap on tree depth (cycle / runaway defence). "
                "A node at the cap keeps its own row but its children are "
                "truncated. Default 20."
            ),
        },
    },
    "required": ["session_id"],
    "additionalProperties": False,
}


_REPLAY_TOOL_DESCRIPTION: Final[str] = (
    "Reconstruct the full trace of one agent session — every operation, "
    "its result, and the parent/child graph between them — as a "
    "chronological ReplayNode forest. tenant_admin forensic tool for "
    "cross-session investigation (G8.2).\n\n"
    "WHEN TO CALL: an admin investigates one agent's whole run after the "
    "fact — 'show me everything session <id> did, in order, with the "
    "lineage between composite operations'. The session id comes from a "
    "prior `query_audit` row's `agent_session_id`, from the broadcast "
    "feed, or from the agent's own logs.\n\n"
    "WHEN NOT TO CALL: to replay YOUR OWN session, use `query_audit` with "
    '`shape="tree"` (operator role, self-session-only) — this tool is '
    "the admin escalation for replaying SOMEONE ELSE'S session. For a "
    "flat filtered list rather than the tree, use `query_audit`.\n\n"
    "Tenant scoping is automatic — the operator's JWT determines the "
    "tenant boundary; a session id outside the tenant yields an empty "
    "root. A session larger than the replay cap is rejected with -32602 "
    "(`session_too_large`) rather than returning a context-blowing tree. "
    "Filter/result contents are NEVER broadcast on the SSE feed — only "
    "the `{op_class, result_status, row_count}` aggregate appears.\n\n"
    "Returns `{root: [ReplayNode, ...], session_id, tenant_id, "
    "row_count}`. `root` is the forest of session roots ascending by "
    "timestamp; each node carries its full audit row plus `depth` and "
    "`children`. `row_count` is the total node count in the returned "
    "tree."
)


async def _replay_handler(
    operator: Operator,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    """Dispatch a ``meho.audit.replay`` admin call through the T3 substrate.

    The dispatcher has jsonschema-validated *arguments* (``session_id``
    present + UUID-shaped, ``max_depth`` an in-range integer when given)
    and RBAC-gated the call to ``tenant_admin`` at both the list-time
    filter and the call-time re-check. This handler resolves the session
    id, validates ``max_depth`` defensively, and delegates to
    :func:`_build_replay_response`, which applies the count-first cap and
    serialises the tree. Tenant scope is taken from the validated JWT —
    never from the arguments dict — so an admin cannot replay another
    tenant's session.
    """
    raw_id = arguments.get("session_id")
    if not isinstance(raw_id, str):
        raise McpInvalidParamsError("session_id must be a string UUID")
    try:
        session_id = uuid.UUID(raw_id)
    except (TypeError, ValueError) as exc:
        raise McpInvalidParamsError(
            f"session_id is not a valid UUID: {raw_id!r}",
        ) from exc

    max_depth = _coerce_max_depth(arguments.get("max_depth"))
    return await _build_replay_response(
        session_id,
        tenant_id=operator.tenant_id,
        max_depth=max_depth,
    )


register_mcp_tool(
    definition=ToolDefinition(
        name=_REPLAY_TOOL_NAME,
        description=_REPLAY_TOOL_DESCRIPTION,
        inputSchema=_REPLAY_INPUT_SCHEMA,
        outputSchema={
            "type": "object",
            "properties": {
                "root": {
                    "type": "array",
                    "items": {"type": "object"},
                    "description": (
                        "Session-root ReplayNode forest ascending by "
                        "timestamp. See "
                        "`meho_backplane.audit_query.schemas.ReplayNode` "
                        "for the per-node field set (AuditEntry + depth + "
                        "children)."
                    ),
                },
                "session_id": {
                    "type": "string",
                    "format": "uuid",
                    "description": "The replayed agent_session_id.",
                },
                "tenant_id": {
                    "type": "string",
                    "format": "uuid",
                    "description": "The tenant boundary the replay ran under.",
                },
                "row_count": {
                    "type": "integer",
                    "description": "Total node count in the returned tree.",
                },
            },
            "required": ["root", "session_id", "tenant_id", "row_count"],
        },
        required_role=TenantRole.TENANT_ADMIN,
        op_class="audit_query",
    ),
    handler=_replay_handler,
)
