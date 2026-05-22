# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``query_topology`` + ``list_targets`` + admin annotate/unannotate — the G9 MCP family.

Tasks #455 (G9.1-T7) and #598 (G9.2-T7). Two daily-surface meta-tools
plus two admin-namespace meta-tools register here, matching the
CLAUDE.md narrow-waist agent surface (postulate 5):

* ``query_topology`` — *parametric*. One ``kind`` argument
  (``dependents`` / ``dependencies`` / ``path`` / ``edges``) selects
  between the three T4 (#451) recursive-CTE traversal shapes and the
  G9.2-T4 (#596) flat edge listing. The per-shape verbs are **not**
  registered as separate MCP tools — that would be the per-op-tool
  anti-pattern CLAUDE.md's "What MEHO is NOT" bullet 1 forbids.
  ``topology.refresh`` is deliberately absent from the agent surface:
  it is the operator CLI verb ``meho topology refresh <target>``
  (Initiative #363 item 10 amendment, 2026-05-14). The
  ``kind="edges"`` facet replaces what would otherwise be a fifth
  ``list_edges`` meta-tool — the curated-edge inventory survey collapses
  into the same parametric tool (Initiative #364 §9 CLAUDE.md naming
  alignment).
* ``list_targets`` — enumerate the operator's accessible infrastructure
  targets so an agent can pick a target before a ``call_operation`` or
  a ``query_topology`` call.
* ``meho.topology.annotate`` / ``meho.topology.unannotate`` — admin
  meta-tools (``tenant_admin`` only) for the curated-edge write half
  (G9.2-T3 #595). Live in the ``meho.*`` admin namespace per Initiative
  #364 §9 — not on the daily ~17 meta-tool agent surface. Visible only
  to a ``tenant_admin``-scoped session; an ``operator``-role caller
  sees neither in ``tools/list`` and a direct ``tools/call`` is
  rejected at the dispatcher's call-time RBAC re-check
  (``handlers._operator_meets_required_role`` → JSON-RPC ``-32602``
  ``forbidden``; there is no HTTP-403 on the MCP transport).

Why direct substrate calls, not REST wrappers
=============================================

CLAUDE.md "What MEHO is NOT" bullet 2: CLI, MCP, and REST are sibling
fronts on one backplane — none is a thin wrapper of another.
``query_topology`` calls the T4 :mod:`meho_backplane.topology.query`
service functions directly; ``list_targets`` runs the same
tenant-scoped ``select(TargetORM)`` the T5 REST route runs. This
mirrors the established ``query_audit`` (#468) precedent which dispatches
straight through the T1 audit substrate rather than the REST router.

Audit + broadcast
=================

The MCP dispatcher (:func:`~meho_backplane.mcp.handlers.handle_tools_call`)
writes exactly one ``audit_log`` row per ``tools/call`` invocation,
keyed ``op_id = <tool-name>`` with the declared ``op_class``. Both
tools declare ``op_class="read"`` so the broadcast classifier's
read-shaped (aggregate-only) policy applies — no per-resource payload
leak. No tool-side audit code is needed: registration alone satisfies
the "each tools/call writes an audit row" acceptance criterion (same
as ``query_audit``).

Tenant scoping
==============

``query_topology`` is tenant-scoped automatically — the T4 service
filters ``graph_node.tenant_id`` / ``graph_edge.tenant_id`` against
``operator.tenant_id`` (lifted from the validated JWT) in both the
anchor and the recursive term; no ``tenant_id`` argument exists on the
tool so a cross-tenant probe is structurally impossible. ``list_targets``
defaults to the operator's own tenant; the optional ``tenant`` argument
selects another tenant's targets and is gated to ``tenant_admin`` —
an ``operator``-role caller passing ``tenant`` gets ``-32602``.

inputSchema conditionals
========================

The MCP dispatcher validates ``arguments`` with
:class:`jsonschema.Draft202012Validator`, which honours JSON Schema
2020-12 ``allOf`` / ``if`` / ``then``. ``query_topology``'s schema
encodes the per-``kind`` requirement declaratively: ``dependents`` /
``dependencies`` require ``target``; ``path`` requires ``from_name`` +
``to_name``. A call that omits the conditionally-required field is
rejected at the schema layer before the handler runs.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Final

from sqlalchemy import select

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import GraphEdgeKind, Tenant
from meho_backplane.db.models import Target as TargetORM
from meho_backplane.mcp.registry import ToolDefinition, register_mcp_tool
from meho_backplane.mcp.server import McpInvalidParamsError
from meho_backplane.operations._lookup import parse_connector_id
from meho_backplane.topology.annotate import (
    AutoEdgeDeletionError,
    InvalidEdgeKindError,
    NodeRef,
    UnannotateSelectorError,
    annotate_edge,
    unannotate_edge,
)
from meho_backplane.topology.query import (
    AmbiguousNodeError,
    find_dependencies,
    find_dependents,
    find_path,
    list_edges,
    query_timeline,
)
from meho_backplane.topology.resolvers import NodeNotFoundError
from meho_backplane.topology.timeline_cursor import InvalidTimelineCursorError

__all__: list[str] = []


# ---------------------------------------------------------------------------
# query_topology
# ---------------------------------------------------------------------------


_QUERY_TOPOLOGY_NAME: Final[str] = "query_topology"

#: Bounds mirror the T4 service defaults (``query._DEFAULT_DEPTH`` /
#: ``query._DEFAULT_MAX_HOPS``) and the T5 REST ceilings
#: (``api.v1.topology._DEPTH_MAX`` / ``_MAX_HOPS_MAX``) so the three
#: fronts cap a pathological traversal identically (#363 performance
#: discipline).
_DEPTH_DEFAULT: Final[int] = 16
_DEPTH_MAX: Final[int] = 64
_MAX_HOPS_DEFAULT: Final[int] = 8
_MAX_HOPS_MAX: Final[int] = 32

#: ``edges`` facet defaults / ceilings. Mirror the T4 service substrate
#: bounds (``query._DEFAULT_EDGE_LIMIT`` = 200, ``query._MAX_EDGE_LIMIT``
#: = 1000) and the T5 REST cap so the four fronts (REST / CLI / MCP /
#: REPL) clamp the inventory survey identically.
_EDGES_LIMIT_DEFAULT: Final[int] = 200
_EDGES_LIMIT_MAX: Final[int] = 1000

#: ``timeline`` facet defaults / ceilings. Mirror the T5 substrate
#: bounds (``query._DEFAULT_TIMELINE_LIMIT`` = 50,
#: ``query._MAX_TIMELINE_LIMIT`` = 1000). Default 50 per the Task
#: #861 acceptance criterion ("default ``--limit 50``").
_TIMELINE_LIMIT_DEFAULT: Final[int] = 50
_TIMELINE_LIMIT_MAX: Final[int] = 1000

#: Canonical ``GraphEdgeKind`` values, materialised once at module load
#: so the inputSchema enum + the kind_filter description stay in lock-step
#: with :class:`~meho_backplane.db.models.GraphEdgeKind` without
#: duplicating the ten-string list. A future widening of the enum
#: surfaces in both the schema and the description automatically.
_EDGE_KIND_VALUES: Final[list[str]] = sorted(k.value for k in GraphEdgeKind)


_QUERY_TOPOLOGY_INPUT_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "kind": {
            "type": "string",
            "enum": ["dependents", "dependencies", "path", "edges", "timeline"],
            "description": (
                "Which read shape to run. `dependents` = reverse closure "
                "(what depends on `target`); `dependencies` = forward "
                "closure (what `target` depends on); `path` = shortest "
                "unweighted route between `from_name` and `to_name`; "
                "`edges` = flat tenant-scoped listing of `graph_edge` "
                "rows (the inventory-survey shape, replaces a "
                "standalone `list_edges` meta-tool); `timeline` = "
                "tenant-wide chronological feed of graph changes from "
                "the G9.3 `graph_node_history` + `graph_edge_history` "
                "tables, cursor-paginated -- 'what's been happening in "
                "the graph in the last hour?' without rooting at a "
                "specific resource."
            ),
        },
        "target": {
            "type": ["string", "null"],
            "description": (
                "Root node name for `dependents` / `dependencies`. "
                "Resolved against `graph_node.name` scoped to the "
                "operator's tenant. Required when `kind` is `dependents` "
                "or `dependencies`; ignored for `path`. "
                "NOTE: this tool's `target` is a bare string because "
                "the read path only needs the name; `call_operation` "
                "wraps the same concept in a `{name: ...}` dict to "
                "leave room for future selector fields. See "
                "`docs/architecture/mcp.md` ('Target-reference shape "
                "convention')."
            ),
            "maxLength": 256,
        },
        "from_name": {
            "type": ["string", "null"],
            "description": (
                "Path start node name. Required when `kind` is `path`. "
                "For `kind=edges`, optional filter restricting the "
                "listing to edges whose `from` endpoint resolves to "
                "this node. Ignored for the closure kinds."
            ),
            "maxLength": 256,
        },
        "to_name": {
            "type": ["string", "null"],
            "description": (
                "Path end node name. Required when `kind` is `path`. "
                "For `kind=edges`, optional filter restricting the "
                "listing to edges whose `to` endpoint resolves to this "
                "node. Ignored for the closure kinds."
            ),
            "maxLength": 256,
        },
        "kind_filter": {
            "type": ["string", "null"],
            "description": (
                "Optional `graph_edge.kind` filter. For the closure "
                "kinds, restricts the walk to edges of that kind "
                "(e.g. `runs-on`, `mounts`, `routes-through`, "
                "`belongs-to`). For `kind=edges`, restricts the flat "
                "listing to edges of that kind. Closed v0.2 vocabulary: "
                f"one of {_EDGE_KIND_VALUES}. Ignored for `path`."
            ),
            "maxLength": 64,
        },
        "source": {
            "type": ["string", "null"],
            "enum": [None, "auto", "curated"],
            "description": (
                "Optional `graph_edge.source` filter for `kind=edges`: "
                "`auto` for probe-derived edges (G9.1 refresh service), "
                "`curated` for operator-asserted ones "
                "(`meho.topology.annotate`). Omit to list both. "
                "Ignored for the closure kinds and `path`."
            ),
        },
        "conflicts": {
            "type": "boolean",
            "default": False,
            "description": (
                "When `true` with `kind=edges`, restrict the listing to "
                "edges carrying a non-empty `properties.conflicts_with` "
                "marker — the recoverability view for §6 conflicts "
                "(annotations contradicting probe-derived auto edges). "
                "Ignored for the closure kinds and `path`."
            ),
        },
        "limit": {
            "type": "integer",
            "minimum": 1,
            "maximum": _EDGES_LIMIT_MAX,
            "default": _EDGES_LIMIT_DEFAULT,
            "description": (
                f"Page size for `kind=edges` (default "
                f"{_EDGES_LIMIT_DEFAULT}; ceiling {_EDGES_LIMIT_MAX}, "
                "matching the substrate `list_edges` cap). Ignored for "
                "the closure kinds and `path`."
            ),
        },
        "offset": {
            "type": "integer",
            "minimum": 0,
            "default": 0,
            "description": (
                "Rows to skip before the first returned edge "
                "(`kind=edges` only). Combined with the substrate's "
                "stable `(last_seen DESC NULLS LAST, id)` order, a "
                "paged sweep reassembles to the unpaged result with no "
                "gaps. Ignored for the closure kinds and `path`."
            ),
        },
        "node_kind": {
            "type": ["string", "null"],
            "description": (
                "Disambiguates the anchor when a name resolves to more "
                "than one node kind in the tenant (e.g. a `target` and a "
                "`vm` both named `app`). Pins the root to "
                "`(tenant, node_kind, name)`. For `path` this pins the "
                "`from_name` endpoint; pass `to_node_kind` for the other."
            ),
            "maxLength": 64,
        },
        "to_node_kind": {
            "type": ["string", "null"],
            "description": (
                "Like `node_kind` but pins the `to_name` endpoint. Only "
                "meaningful when `kind` is `path`."
            ),
            "maxLength": 64,
        },
        "depth": {
            "type": "integer",
            "minimum": 1,
            "maximum": _DEPTH_MAX,
            "default": _DEPTH_DEFAULT,
            "description": (
                "Max traversal depth for `dependents` / `dependencies`. "
                f"Default {_DEPTH_DEFAULT}; hard ceiling {_DEPTH_MAX}. "
                "Ignored for `path`."
            ),
        },
        "max_hops": {
            "type": "integer",
            "minimum": 1,
            "maximum": _MAX_HOPS_MAX,
            "default": _MAX_HOPS_DEFAULT,
            "description": (
                "Max hops for `path`. Default "
                f"{_MAX_HOPS_DEFAULT}; hard ceiling {_MAX_HOPS_MAX}. "
                "Ignored for the closure kinds."
            ),
        },
        "since": {
            "type": ["string", "null"],
            "description": (
                "ISO-8601 absolute lower bound on `valid_from` for "
                "`kind=timeline`. Inclusive. Pair with `until` to "
                "scope the timeline to one window. Ignored for the "
                "closure / path / edges kinds."
            ),
            "format": "date-time",
        },
        "until": {
            "type": ["string", "null"],
            "description": (
                "ISO-8601 absolute upper bound on `valid_from` for "
                "`kind=timeline`. Inclusive. Ignored for the closure "
                "/ path / edges kinds."
            ),
            "format": "date-time",
        },
        "cursor": {
            "type": ["string", "null"],
            "description": (
                "Opaque forward-pagination token from a prior "
                "`kind=timeline` page's `next_cursor`. Encodes "
                "`(valid_from, history_id, source)`; stable under "
                "concurrent inserts from the G9.3 diff-on-write hook. "
                "Ignored for non-timeline kinds."
            ),
            "maxLength": 1024,
        },
    },
    "required": ["kind"],
    "additionalProperties": False,
    # Conditionally require the kind-specific arguments. Draft 2020-12
    # `allOf` + `if`/`then` is honoured by the dispatcher's
    # `jsonschema.Draft202012Validator`, so an agent that asks for
    # `kind=path` without `from_name`/`to_name` is rejected at the
    # schema layer before the handler runs.
    "allOf": [
        {
            "if": {"properties": {"kind": {"const": "dependents"}}},
            "then": {"required": ["target"]},
        },
        {
            "if": {"properties": {"kind": {"const": "dependencies"}}},
            "then": {"required": ["target"]},
        },
        {
            "if": {"properties": {"kind": {"const": "path"}}},
            "then": {"required": ["from_name", "to_name"]},
        },
    ],
}


_QUERY_TOPOLOGY_DESCRIPTION: Final[str] = (
    "Query the topology graph. Use `kind=dependents` BEFORE recommending "
    "a destructive op on a resource — it answers 'what depends on this "
    "resource that I'd break?' (the blast-radius check: call this "
    "*before* recommending a destructive op). Use `kind=dependencies` to "
    "understand what a resource needs. Use `kind=path` to trace "
    "connectivity between two specific resources. Use `kind=edges` for "
    "the flat inventory survey — list curated / auto edges with "
    "optional filters; pair with `conflicts=true` to surface §6 "
    "conflicts that need operator review. Tenant-scoped automatically.\n\n"
    "WHEN TO CALL: before suggesting any delete/shutdown/detach — "
    "'is it safe to delete namespace customer-a-prod-foo?' → "
    "`query_topology {kind: dependents, target: customer-a-prod-foo}` "
    "returns every service / ingress / database that would break. Also "
    "for impact reasoning ('what does this VM run on?') and reachability "
    "('is there any route from this ingress to that datastore?'). For "
    "`kind=edges`: 'show the curated edges in this tenant' → "
    "`{kind: edges, source: curated}`; 'are any annotations in conflict?'"
    " → `{kind: edges, conflicts: true}`.\n\n"
    "PARAMETRIC: `kind` is the discriminator — `dependents` / "
    "`dependencies` need `target`; `path` needs `from_name` + "
    "`to_name`; `edges` has no required field (every filter is "
    "optional). The four shapes are one tool, not four: there is no "
    "separate `topology.dependents` / `list_edges` tool. If a name is "
    "ambiguous in the tenant (same name as both, e.g., a target and a "
    "vm) pass `node_kind` to pin the anchor; an ambiguous bare name "
    "returns -32602 naming the candidate kinds.\n\n"
    "Returns `{kind, nodes: [TopologyNode, ...]}` for the closure kinds "
    "(root at depth 0, so a one-element list means 'exists but nothing "
    "depends on it' and an empty list means 'no such node in this "
    'tenant\'); `{kind: "path", path: TopologyPath|null}` for `path` '
    "(null = unreachable within `max_hops`, a valid answer, not an "
    'error); `{kind: "edges", edges: [TopologyEdge, ...]}` for `edges` '
    "(flat list, ordered by `last_seen DESC NULLS LAST, id`).\n\n"
    "For `kind=timeline`: tenant-wide chronological feed of graph "
    "changes -- 'what changed in the graph since 9am?' → "
    "`query_topology {kind: timeline, since: 2026-05-22T09:00:00Z}`. "
    "Use `since` / `until` to bound the window. Cursor-paginated "
    "(default `limit: 50`); a non-null `next_cursor` in the response "
    "means more rows exist -- pass it back as `cursor` on the next "
    "call to walk forward. Returns "
    '`{kind: "timeline", rows: [TopologyTimelineEntry, ...], '
    "next_cursor: <token|null>}`."
)


async def _query_topology_handler(
    operator: Operator,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    """Dispatch a ``query_topology`` call to the matching T4 verb.

    The dispatcher has already jsonschema-validated *arguments* against
    :data:`_QUERY_TOPOLOGY_INPUT_SCHEMA`, including the per-``kind``
    conditional ``required`` clauses, so the conditionally-required
    field is guaranteed present for its ``kind``. Tenant scope comes
    from *operator* inside the T4 service — never from *arguments*.

    :class:`~meho_backplane.topology.query.AmbiguousNodeError` (a bare
    name resolving to multiple kinds with no ``node_kind`` pin) is an
    operator-actionable input problem, so it surfaces as JSON-RPC
    ``-32602`` with the candidate kinds named — the same recovery the
    REST front offers as a 409.
    """
    kind: str = arguments["kind"]
    try:
        if kind == "dependents":
            nodes = await find_dependents(
                operator,
                arguments["target"],
                kind=arguments.get("node_kind"),
                depth=int(arguments.get("depth", _DEPTH_DEFAULT)),
                kind_filter=arguments.get("kind_filter"),
            )
            return {"kind": kind, "nodes": [n.model_dump(mode="json") for n in nodes]}
        if kind == "dependencies":
            nodes = await find_dependencies(
                operator,
                arguments["target"],
                kind=arguments.get("node_kind"),
                depth=int(arguments.get("depth", _DEPTH_DEFAULT)),
                kind_filter=arguments.get("kind_filter"),
            )
            return {"kind": kind, "nodes": [n.model_dump(mode="json") for n in nodes]}
        if kind == "edges":
            edges = await _list_edges_facet(operator, arguments)
            return {"kind": kind, "edges": [e.model_dump(mode="json") for e in edges]}
        if kind == "timeline":
            return await _timeline_facet(operator, arguments)
        # kind == "path" — the enum + schema guarantee no other value.
        result = await find_path(
            operator,
            arguments["from_name"],
            arguments["to_name"],
            from_kind=arguments.get("node_kind"),
            to_kind=arguments.get("to_node_kind"),
            max_hops=int(arguments.get("max_hops", _MAX_HOPS_DEFAULT)),
        )
    except AmbiguousNodeError as exc:
        raise McpInvalidParamsError(str(exc)) from exc
    return {
        "kind": kind,
        "path": None if result is None else result.model_dump(mode="json"),
    }


async def _timeline_facet(
    operator: Operator,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    """Dispatch the ``kind="timeline"`` facet to G9.3-T5's substrate.

    Parses ISO-8601 strings from ``since`` / ``until`` (the MCP layer
    accepts absolute ISO-8601 only; the CLI layer adds the
    ``"24h"`` / ``"7d"`` duration-shorthand convenience and resolves
    it to an absolute timestamp before crossing the wire). Forwards
    the optional cursor verbatim.

    :class:`InvalidTimelineCursorError` from the substrate surfaces
    as JSON-RPC ``-32602`` -- the cursor came from a prior
    ``next_cursor`` of this same tool, so a tampered / hand-crafted
    token is an operator-actionable input problem.

    ``target`` is intentionally **not** wired in here: the MCP
    surface for ``kind=timeline`` does not accept a per-target
    narrowing because the closure / path kinds already use ``target``
    for a different concept (the anchor node name). Operators
    wanting the per-target slice use the CLI ``meho topology
    timeline --target ...`` which resolves the name to a target id
    before calling the substrate. A future MCP widening can add a
    ``target_id`` argument without conflicting with the closure
    semantics.
    """
    since_str = arguments.get("since")
    until_str = arguments.get("until")
    cursor = arguments.get("cursor")
    since_dt: datetime | None = None
    until_dt: datetime | None = None
    if isinstance(since_str, str):
        try:
            since_dt = datetime.fromisoformat(since_str)
        except ValueError as exc:
            raise McpInvalidParamsError(
                f"query_topology(kind=timeline): 'since' is not ISO-8601: {since_str!r}",
            ) from exc
    if isinstance(until_str, str):
        try:
            until_dt = datetime.fromisoformat(until_str)
        except ValueError as exc:
            raise McpInvalidParamsError(
                f"query_topology(kind=timeline): 'until' is not ISO-8601: {until_str!r}",
            ) from exc

    try:
        result = await query_timeline(
            operator,
            target_id=None,
            since=since_dt,
            until=until_dt,
            limit=int(arguments.get("limit", _TIMELINE_LIMIT_DEFAULT)),
            cursor=cursor if isinstance(cursor, str) else None,
        )
    except InvalidTimelineCursorError as exc:
        raise McpInvalidParamsError(str(exc)) from exc

    return {
        "kind": "timeline",
        "rows": [r.model_dump(mode="json") for r in result.rows],
        "next_cursor": result.next_cursor,
    }


async def _list_edges_facet(
    operator: Operator,
    arguments: dict[str, Any],
) -> list[Any]:
    """Dispatch the ``kind="edges"`` facet to the G9.2-T4 substrate.

    Opens a session and forwards the optional filters
    (``kind_filter``/``source``/``from_name``/``to_name``/``conflicts``/
    ``limit``/``offset``) to :func:`list_edges`. The tenant scope is the
    operator's tenant — never lifted from *arguments* — so there is no
    cross-tenant probe via this surface even with a smuggled
    ``tenant_id`` (``additionalProperties: false`` already rejects that
    at the schema layer).

    :class:`AmbiguousNodeError` from ``list_edges``'s endpoint resolver
    is caught by the outer handler's ``try/except`` and surfaces as
    JSON-RPC ``-32602`` with the candidate kinds named — the same
    contract the closure kinds use.
    """
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        return await list_edges(
            session,
            operator.tenant_id,
            kind=arguments.get("kind_filter"),
            source=arguments.get("source"),
            from_ref=arguments.get("from_name"),
            to_ref=arguments.get("to_name"),
            conflicts_only=bool(arguments.get("conflicts", False)),
            limit=int(arguments.get("limit", _EDGES_LIMIT_DEFAULT)),
            offset=int(arguments.get("offset", 0)),
        )


register_mcp_tool(
    definition=ToolDefinition(
        name=_QUERY_TOPOLOGY_NAME,
        description=_QUERY_TOPOLOGY_DESCRIPTION,
        inputSchema=_QUERY_TOPOLOGY_INPUT_SCHEMA,
        outputSchema={
            "type": "object",
            "properties": {
                "kind": {
                    "type": "string",
                    "enum": [
                        "dependents",
                        "dependencies",
                        "path",
                        "edges",
                        "timeline",
                    ],
                },
                "nodes": {
                    "type": "array",
                    "items": {"type": "object"},
                    "description": (
                        "Present for the closure kinds. TopologyNode rows "
                        "ordered (depth, name); root at depth 0. See "
                        "`meho_backplane.topology.schemas.TopologyNode`."
                    ),
                },
                "path": {
                    "type": ["object", "null"],
                    "description": (
                        "Present for `kind=path`. A TopologyPath, or null "
                        "when the target is unreachable within `max_hops`."
                    ),
                },
                "edges": {
                    "type": "array",
                    "items": {"type": "object"},
                    "description": (
                        "Present for `kind=edges`. TopologyEdge rows "
                        "ordered (last_seen DESC NULLS LAST, id). See "
                        "`meho_backplane.topology.schemas.TopologyEdge`."
                    ),
                },
                "rows": {
                    "type": "array",
                    "items": {"type": "object"},
                    "description": (
                        "Present for `kind=timeline`. TopologyTimelineEntry "
                        "rows ordered (valid_from DESC, history_id DESC). "
                        "See `meho_backplane.topology.schemas."
                        "TopologyTimelineEntry`."
                    ),
                },
                "next_cursor": {
                    "type": ["string", "null"],
                    "description": (
                        "Present for `kind=timeline`. Opaque next-page "
                        "token, or null when the page is the end of the "
                        "matching set."
                    ),
                },
            },
            "required": ["kind"],
        },
        required_role=TenantRole.OPERATOR,
        op_class="read",
    ),
    handler=_query_topology_handler,
)


# ---------------------------------------------------------------------------
# list_targets
# ---------------------------------------------------------------------------


_LIST_TARGETS_NAME: Final[str] = "list_targets"


_LIST_TARGETS_INPUT_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "connector_id": {
            "type": ["string", "null"],
            "description": (
                "Optional connector filter in the form "
                '`<impl_id>-<version>` (e.g. "vmware-rest-9.0") or a '
                'v1-style single-product slug (e.g. "vault"). Only the '
                "product component is used: the result is narrowed to "
                "targets whose `product` matches that connector's "
                "product. Omit to list every target."
            ),
            "maxLength": 256,
        },
        "tenant": {
            "type": ["string", "null"],
            "description": (
                "Cross-tenant scope. Omit / null → the operator's own "
                "tenant (the only choice for the `operator` role). A "
                "tenant slug or UUID selects another tenant's targets "
                "and REQUIRES the `tenant_admin` role; an `operator`-role "
                "caller passing this gets -32602."
            ),
            "maxLength": 256,
        },
        "limit": {
            "type": "integer",
            "minimum": 1,
            "maximum": 500,
            "default": 100,
            "description": "Page size. Default 100; max 500.",
        },
        "cursor": {
            "type": ["string", "null"],
            "description": (
                "Keyset-pagination cursor: pass the last `name` from the "
                "previous page to fetch the next. Results are ordered by "
                "`name` ascending."
            ),
            "maxLength": 256,
        },
    },
    "additionalProperties": False,
}


_LIST_TARGETS_DESCRIPTION: Final[str] = (
    "List the operator's accessible infrastructure targets, optionally "
    "filtered by connector. Use to enumerate available infrastructure "
    "before picking a target for `call_operation` or `query_topology` — "
    "the `name` of a row here is what those tools expect as their "
    "`target`.\n\n"
    "WHEN TO CALL: the operator asks 'what can I act on?', or you need a "
    "concrete target name and only have a product in mind ('list the "
    "vCenters' → `connector_id=vmware-rest-9.0`). Tenant scope is the "
    "operator's own tenant unless a `tenant_admin` passes `tenant`.\n\n"
    "Returns `{targets: [{id, name, aliases, product, host}, ...], "
    "next_cursor: <name|null>}` ordered by name; `next_cursor` is the "
    "last name on the page when more rows may exist, else null."
)


async def _resolve_tenant_scope(operator: Operator, tenant_arg: str | None) -> Any:
    """Resolve the tenant id to scope the listing to.

    No ``tenant`` argument → the operator's own tenant (the only path
    open to an ``operator``-role caller). A ``tenant`` argument is a
    cross-tenant request: it requires ``tenant_admin`` and is resolved
    by slug first then UUID. An unknown tenant, or a non-admin passing
    ``tenant``, surfaces as ``-32602`` (operator-actionable input
    problem) rather than silently falling back to the own-tenant scope
    — a silent fallback would make a typo'd cross-tenant query look
    like an empty tenant.
    """
    if tenant_arg is None:
        return operator.tenant_id
    if operator.tenant_role != TenantRole.TENANT_ADMIN:
        raise McpInvalidParamsError(
            "list_targets: the `tenant` argument (cross-tenant scope) "
            "requires the tenant_admin role",
        )
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        by_slug = await session.execute(
            select(Tenant.id).where(Tenant.slug == tenant_arg),
        )
        tenant_id = by_slug.scalar_one_or_none()
        if tenant_id is not None:
            return tenant_id
        # Slug miss — try the argument as a tenant UUID.
        try:
            from uuid import UUID

            candidate = UUID(tenant_arg)
        except ValueError:
            candidate = None
        if candidate is not None:
            by_id = await session.execute(
                select(Tenant.id).where(Tenant.id == candidate),
            )
            tenant_id = by_id.scalar_one_or_none()
            if tenant_id is not None:
                return tenant_id
    raise McpInvalidParamsError(
        f"list_targets: no tenant matches {tenant_arg!r} (tried slug then UUID)",
    )


async def _list_targets_handler(
    operator: Operator,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    """Enumerate targets for the resolved tenant, optionally product-filtered.

    Runs the same tenant-scoped, name-keyset-paginated
    ``select(TargetORM)`` the T5 REST ``GET /api/v1/targets`` route runs
    — CLI / MCP / REST are sibling fronts on one backplane, so this is a
    direct substrate query, not a REST-route wrapper. The optional
    ``connector_id`` is canonicalised through
    :func:`~meho_backplane.operations._lookup.parse_connector_id` and
    only its product component drives a ``TargetORM.product`` exact-match
    filter (targets carry a product slug, not a connector id).
    """
    scope_tenant_id = await _resolve_tenant_scope(operator, arguments.get("tenant"))

    stmt = select(TargetORM).where(TargetORM.tenant_id == scope_tenant_id)

    connector_id = arguments.get("connector_id")
    if connector_id is not None:
        product, _version, _impl_id = parse_connector_id(connector_id)
        stmt = stmt.where(TargetORM.product == product)

    cursor = arguments.get("cursor")
    if cursor is not None:
        stmt = stmt.where(TargetORM.name > cursor)

    limit = int(arguments.get("limit", 100))
    stmt = stmt.order_by(TargetORM.name).limit(limit)

    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        result = await session.execute(stmt)
        rows = list(result.scalars().all())

    targets = [
        {
            "id": str(t.id),
            "name": t.name,
            "aliases": list(t.aliases),
            "product": t.product,
            "host": t.host,
        }
        for t in rows
    ]
    # A full page implies there *may* be more rows; surface the last
    # name as the keyset cursor. A short page is definitively the end.
    next_cursor = targets[-1]["name"] if len(targets) == limit else None
    return {"targets": targets, "next_cursor": next_cursor}


register_mcp_tool(
    definition=ToolDefinition(
        name=_LIST_TARGETS_NAME,
        description=_LIST_TARGETS_DESCRIPTION,
        inputSchema=_LIST_TARGETS_INPUT_SCHEMA,
        outputSchema={
            "type": "object",
            "properties": {
                "targets": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "id": {"type": "string"},
                            "name": {"type": "string"},
                            "aliases": {
                                "type": "array",
                                "items": {"type": "string"},
                            },
                            "product": {"type": "string"},
                            "host": {"type": "string"},
                        },
                        "required": ["id", "name", "aliases", "product", "host"],
                    },
                },
                "next_cursor": {
                    "type": ["string", "null"],
                    "description": (
                        "Last name on the page when more rows may exist; "
                        "null when the page is the end of the set."
                    ),
                },
            },
            "required": ["targets", "next_cursor"],
        },
        required_role=TenantRole.OPERATOR,
        op_class="read",
    ),
    handler=_list_targets_handler,
)


# ---------------------------------------------------------------------------
# meho.topology.annotate / meho.topology.unannotate — admin namespace
# ---------------------------------------------------------------------------
#
# Task #598 (G9.2-T7). Two admin meta-tools in the ``meho.*`` namespace
# expose the curated-edge write half (#595) to a ``tenant_admin``-scoped
# MCP session. The handlers call :func:`annotate_edge` /
# :func:`unannotate_edge` directly — the service primitive owns its own
# resolve / validate / upsert / §6 conflict scan / audit / broadcast — so
# the MCP front is a thin parameter shim, not a re-derivation of the
# write path. CLAUDE.md "What MEHO is NOT" bullet 2: REST / CLI / MCP are
# sibling fronts on one backplane; none is a thin wrapper of another.
#
# Naming: ``from_name`` / ``to_name`` (not ``from`` / ``to``) — ``from``
# is a Python keyword the wider topology module already aliases (see
# ``query_topology``'s schema for the same convention). Keeping the
# names consistent across all four MCP topology tools lets an agent
# carry a node-pair through ``query_topology(path)`` →
# ``meho.topology.annotate`` without renaming.


_ANNOTATE_TOOL_NAME: Final[str] = "meho.topology.annotate"
_UNANNOTATE_TOOL_NAME: Final[str] = "meho.topology.unannotate"


_ANNOTATE_INPUT_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "from_name": {
            "type": "string",
            "minLength": 1,
            "maxLength": 256,
            "description": (
                "`graph_node.name` of the edge's `from` endpoint. "
                "Resolved against the operator's tenant (cross-tenant "
                "is structurally impossible — no `tenant_id` argument)."
            ),
        },
        "kind": {
            "type": "string",
            "enum": _EDGE_KIND_VALUES,
            "description": (
                "Closed v0.2 edge-kind vocabulary. Operator-curated "
                "kinds (`authenticates-via`, `depends-on`, "
                "`replicates-to`, `backed-up-by`, `routes-via`, "
                "`policy-binds`) cover the cross-system relationships "
                "auto-discovery cannot infer — those are the canonical "
                "use cases. The four auto-discoverable kinds "
                "(`runs-on`, `mounts`, `routes-through`, `belongs-to`) "
                "are accepted too but ANNOTATING THEM IS NOISE: probes "
                "already write them and the next refresh will mark "
                "your assertion as a §6 conflict marker."
            ),
        },
        "to_name": {
            "type": "string",
            "minLength": 1,
            "maxLength": 256,
            "description": (
                "`graph_node.name` of the edge's `to` endpoint. Same "
                "resolution rules as `from_name`."
            ),
        },
        "from_node_kind": {
            "type": ["string", "null"],
            "description": (
                "Optional `graph_node.kind` pin for the `from_name` "
                "endpoint. Required only when the bare name resolves to "
                "multiple kinds in the tenant (e.g. a `target` and a "
                "`vm` both named `app`); an ambiguous bare name returns "
                "-32602 naming the candidate kinds."
            ),
            "maxLength": 64,
        },
        "to_node_kind": {
            "type": ["string", "null"],
            "description": (
                "Optional `graph_node.kind` pin for the `to_name` "
                "endpoint. Same contract as `from_node_kind`."
            ),
            "maxLength": 64,
        },
        "note": {
            "type": ["string", "null"],
            "maxLength": 2048,
            "description": (
                "Optional free-text annotation stored on "
                "`graph_edge.properties.note`. Use to record the "
                "operational rationale — 'Vault role `k8s-prod-read` "
                "binds to namespace `prod`; rotated 2026-04-22'."
            ),
        },
        "evidence_url": {
            "type": ["string", "null"],
            "maxLength": 2048,
            "description": (
                "Optional URL the operator attached as evidence "
                "(typically an INVENTORY.md anchor / runbook). Stored "
                "on `graph_edge.properties.evidence_url`."
            ),
        },
    },
    "required": ["from_name", "kind", "to_name"],
    "additionalProperties": False,
}


_ANNOTATE_DESCRIPTION: Final[str] = (
    "Assert a curated `graph_edge` that auto-discovery cannot infer "
    "(tenant_admin only). The canonical use case is a cross-system "
    "relationship the probes can't see — `k8s-sa-foo` "
    "`authenticates-via` `vault-role-bar`, `service-X` `depends-on` "
    "`database-Y`. Idempotent on the `(from_name, kind, to_name)` "
    "triple: re-annotate refreshes `last_seen` + `properties` rather "
    "than erroring. Tenant-scoped automatically — no `tenant_id` "
    "argument (cross-tenant annotation is structurally impossible).\n\n"
    "REQUIRES: `from_name` and `to_name` must already exist as "
    "`graph_node` rows in the tenant. A fresh tenant has zero nodes; "
    "calling annotate there returns -32602 `no graph_node matched "
    "<name> in this tenant`. Seed the endpoints first with "
    "`meho.topology.create_node {kind, name}` (the manual MCP seed "
    "verb) or via the CLI `meho topology refresh <target>` (the "
    "probe-driven path). The create_node verb is the right path for "
    "the empty-tenant bootstrap and for curated inner-graph nodes the "
    "probes cannot derive (vault-role, keycloak-realm, ...).\n\n"
    "WHEN TO CALL: an operator asks 'record that the prod namespace "
    "authenticates against the rdc-vault role binding' — "
    "`meho.topology.annotate {from_name: prod, kind: "
    "authenticates-via, to_name: rdc-vault-role-bar}`. After this, "
    "`query_topology {kind: dependents, target: rdc-vault-role-bar}` "
    "surfaces the namespace in the blast radius.\n\n"
    "DO NOT use to annotate edges the probes already discover "
    "(`runs-on`, `mounts`, `routes-through`, `belongs-to`) — those "
    "would land as §6 conflict markers (`conflicts_with`) and clutter "
    "the inventory survey without semantic gain. Use a curated-only "
    "kind for cross-system assertions: `authenticates-via`, "
    "`depends-on`, `replicates-to`, `backed-up-by`, `routes-via`, "
    "`policy-binds`.\n\n"
    "Returns `{edge_id, from: {id, kind, name}, to: {id, kind, name}, "
    'kind, source: "curated", conflicts: [<edge-id>...]}`. `conflicts` '
    "lists edges of an incompatible kind over the same endpoint pair — "
    "a diagnostic; the recovery flow is to `meho.topology.unannotate` "
    "this edge. (Auto edges displaced by this annotation are stamped "
    "`properties.superseded_by` on the database row and recorded in the "
    "audit/broadcast payload, but are not surfaced on the tool's return "
    "shape — inspect them with `query_topology {kind: edges}` if needed.)"
)


async def _annotate_handler(
    operator: Operator,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    """Dispatch a ``meho.topology.annotate`` call to :func:`annotate_edge`.

    Opens a session, builds the two :class:`NodeRef` objects, and
    forwards to the substrate. The service primitive owns the resolve /
    validate / upsert / §6 conflict scan / audit / broadcast — this
    shim does not duplicate any of it.

    Failure-mode translation:

    * :class:`AmbiguousNodeError` and :class:`NodeNotFoundError` →
      ``-32602`` (operator-actionable input problem; same shape the
      closure kinds use).
    * :class:`InvalidEdgeKindError` is structurally unreachable —
      ``kind`` is enum-pinned by the inputSchema — but the catch is
      retained as a belt-and-suspenders guard against a future enum
      drift between :class:`GraphEdgeKind` and the cached
      :data:`_EDGE_KIND_VALUES`.
    """
    sessionmaker = get_sessionmaker()
    from_name: str = arguments["from_name"]
    to_name: str = arguments["to_name"]
    kind: str = arguments["kind"]
    try:
        async with sessionmaker() as session:
            edge = await annotate_edge(
                session,
                operator,
                NodeRef(from_name, arguments.get("from_node_kind")),
                kind,
                NodeRef(to_name, arguments.get("to_node_kind")),
                note=arguments.get("note"),
                evidence_url=arguments.get("evidence_url"),
            )
            # Re-load the endpoint nodes for the response shape. The
            # service returns the edge only; mapping back to the
            # human-readable `(kind, name)` pair is the front's job.
            from meho_backplane.db.models import GraphNode

            from_node = await session.get(GraphNode, edge.from_node_id)
            to_node = await session.get(GraphNode, edge.to_node_id)
    except (AmbiguousNodeError, NodeNotFoundError, InvalidEdgeKindError) as exc:
        raise McpInvalidParamsError(str(exc)) from exc

    if from_node is None or to_node is None:
        # Endpoint resolution succeeded inside the service transaction
        # but the post-commit reload missed — graph in inconsistent
        # state. Surface as -32602 with a diagnostic; the audit /
        # broadcast emitted inside annotate_edge is already committed.
        raise McpInvalidParamsError(f"annotated edge {edge.id} endpoint lookup failed post-commit")

    props = edge.properties or {}
    raw_conflicts = props.get("conflicts_with")
    conflicts = list(raw_conflicts) if isinstance(raw_conflicts, list) else []
    return {
        "edge_id": str(edge.id),
        "from": {
            "id": str(from_node.id),
            "kind": from_node.kind,
            "name": from_node.name,
        },
        "to": {
            "id": str(to_node.id),
            "kind": to_node.kind,
            "name": to_node.name,
        },
        "kind": edge.kind,
        "source": edge.source,
        "conflicts": conflicts,
    }


register_mcp_tool(
    definition=ToolDefinition(
        name=_ANNOTATE_TOOL_NAME,
        description=_ANNOTATE_DESCRIPTION,
        inputSchema=_ANNOTATE_INPUT_SCHEMA,
        outputSchema={
            "type": "object",
            "properties": {
                "edge_id": {"type": "string"},
                "from": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string"},
                        "kind": {"type": "string"},
                        "name": {"type": "string"},
                    },
                    "required": ["id", "kind", "name"],
                },
                "to": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string"},
                        "kind": {"type": "string"},
                        "name": {"type": "string"},
                    },
                    "required": ["id", "kind", "name"],
                },
                "kind": {"type": "string"},
                "source": {"type": "string"},
                "conflicts": {
                    "type": "array",
                    "items": {"type": "string"},
                },
            },
            "required": ["edge_id", "from", "to", "kind", "source", "conflicts"],
        },
        required_role=TenantRole.TENANT_ADMIN,
        op_class="write",
    ),
    handler=_annotate_handler,
)


# --- unannotate ----------------------------------------------------------


_UNANNOTATE_INPUT_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "edge_id": {
            "type": "string",
            "description": (
                "UUID of the curated `graph_edge` to remove. Mutually "
                "exclusive with the `(from_name, kind, to_name)` triple — "
                "pass exactly one selector form."
            ),
            "minLength": 1,
            "maxLength": 64,
        },
        "from_name": {
            "type": "string",
            "description": (
                "Triple selector: the edge's `from` endpoint name. Must "
                "appear together with `kind` and `to_name` (or with "
                "neither, when using `edge_id`)."
            ),
            "minLength": 1,
            "maxLength": 256,
        },
        "kind": {
            "type": "string",
            "enum": list(_EDGE_KIND_VALUES),
            "description": (
                "Triple selector: the edge's `graph_edge.kind`. Must "
                "appear together with `from_name` and `to_name`."
            ),
        },
        "to_name": {
            "type": "string",
            "description": (
                "Triple selector: the edge's `to` endpoint name. Must "
                "appear together with `from_name` and `kind`."
            ),
            "minLength": 1,
            "maxLength": 256,
        },
        "from_node_kind": {
            "type": ["string", "null"],
            "description": (
                "Optional `graph_node.kind` pin for the `from_name` "
                "endpoint, used for ambiguity disambiguation. Only "
                "meaningful with the triple selector form."
            ),
            "minLength": 1,
            "maxLength": 64,
        },
        "to_node_kind": {
            "type": ["string", "null"],
            "description": (
                "Optional `graph_node.kind` pin for the `to_name` "
                "endpoint, used for ambiguity disambiguation. Only "
                "meaningful with the triple selector form."
            ),
            "minLength": 1,
            "maxLength": 64,
        },
    },
    "additionalProperties": False,
    # XOR at the wire boundary: either `edge_id` alone, or the full
    # `(from_name, kind, to_name)` triple. Partial triples, both
    # selectors, or neither are rejected by jsonschema (Draft 2020-12)
    # before reaching the service. The substrate-level XOR guard in
    # `_unannotate_handler` stays as belt-and-suspenders for the
    # never-validated path (direct in-process callers).
    "oneOf": [
        {
            "required": ["edge_id"],
            "not": {
                "anyOf": [
                    {"required": ["from_name"]},
                    {"required": ["kind"]},
                    {"required": ["to_name"]},
                ],
            },
        },
        {
            "required": ["from_name", "kind", "to_name"],
            "not": {"required": ["edge_id"]},
        },
    ],
}


_UNANNOTATE_DESCRIPTION: Final[str] = (
    "Hard-delete a curated `graph_edge` and clear its reciprocal §6 "
    "markers (tenant_admin only). Pass either `edge_id` OR the full "
    "`(from_name, kind, to_name)` triple — both forms, partial triples, "
    "or empty strings are rejected at the inputSchema layer (-32602) "
    "before the service is reached. Tenant-scoped automatically.\n\n"
    "WHEN TO CALL: an annotation was wrong and needs to be revoked — "
    "the operator originally asserted `service-X depends-on database-Y` "
    "but it turns out the real dependency is `database-Z`. "
    "`meho.topology.unannotate {from_name: service-X, kind: depends-on, "
    "to_name: database-Y}` removes the curated row and re-promotes any "
    "auto edge it had marked superseded (§6 recoverability invariant). "
    "After this, blast-radius checks no longer include the wrong edge.\n\n"
    "Refuses to delete an `source='auto'` edge — those resurrect on the "
    "next refresh, making manual deletion meaningless. The refusal "
    "surfaces as a structured -32602 with `auto-discovered` in the "
    "message so the operator sees the diagnostic without a separate "
    "listing call.\n\n"
    'Returns `{edge_id: "<removed-uuid>"}`.'
)


async def _unannotate_handler(
    operator: Operator,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    """Dispatch a ``meho.topology.unannotate`` call to :func:`unannotate_edge`.

    The two selector forms (UUID primary key vs. ``(from, kind, to)``
    triple) are mutually exclusive at the wire boundary — the tool's
    ``inputSchema`` rejects partial triples, both selectors, and the
    empty-arguments case with a -32602 jsonschema error before reaching
    this handler. The service-layer :class:`UnannotateSelectorError`
    guard stays for the never-validated path (direct in-process
    callers), so the matrix is fully covered.

    :class:`AutoEdgeDeletionError` is the §6 auto-vs-curated refusal —
    surfaces as ``-32602`` with the substrate's "auto edges resurrect
    on next refresh" message so the operator gets the diagnostic
    inline.
    """
    edge_id_arg = arguments.get("edge_id")
    from_name = arguments.get("from_name")
    kind = arguments.get("kind")
    to_name = arguments.get("to_name")

    edge_uuid: uuid.UUID | None = None
    if edge_id_arg is not None:
        try:
            edge_uuid = uuid.UUID(edge_id_arg)
        except ValueError as exc:
            raise McpInvalidParamsError(
                f"meho.topology.unannotate: edge_id is not a valid UUID: {edge_id_arg!r}",
            ) from exc

    from_ref = NodeRef(from_name, arguments.get("from_node_kind")) if from_name else None
    to_ref = NodeRef(to_name, arguments.get("to_node_kind")) if to_name else None

    sessionmaker = get_sessionmaker()
    try:
        async with sessionmaker() as session:
            removed_id = await unannotate_edge(
                session,
                operator,
                edge_id=edge_uuid,
                from_ref=from_ref,
                kind=kind,
                to_ref=to_ref,
            )
    except (
        AmbiguousNodeError,
        NodeNotFoundError,
        InvalidEdgeKindError,
        UnannotateSelectorError,
        AutoEdgeDeletionError,
    ) as exc:
        raise McpInvalidParamsError(str(exc)) from exc
    except ValueError as exc:
        # ``unannotate_edge`` raises plain ``ValueError`` when the
        # selector resolves to no row (or to a row in another tenant —
        # the boundary case the service treats as not-found). That is
        # an operator-actionable input problem, so surface as -32602
        # rather than letting it become -32603 Internal Error.
        raise McpInvalidParamsError(str(exc)) from exc

    return {"edge_id": str(removed_id)}


register_mcp_tool(
    definition=ToolDefinition(
        name=_UNANNOTATE_TOOL_NAME,
        description=_UNANNOTATE_DESCRIPTION,
        inputSchema=_UNANNOTATE_INPUT_SCHEMA,
        outputSchema={
            "type": "object",
            "properties": {
                "edge_id": {"type": "string"},
            },
            "required": ["edge_id"],
        },
        required_role=TenantRole.TENANT_ADMIN,
        op_class="write",
    ),
    handler=_unannotate_handler,
)
