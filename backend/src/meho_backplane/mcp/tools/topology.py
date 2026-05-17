# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``query_topology`` + ``list_targets`` — the G9 Targets/topology MCP family.

Task #455 (G9.1-T7). Exactly **two** meta-tools register here, matching
the CLAUDE.md narrow-waist agent surface (postulate 5, the
Targets/topology row of the agent-surface table — 2 of the ~17
meta-tools):

* ``query_topology`` — *parametric*. One ``kind`` argument
  (``dependents`` / ``dependencies`` / ``path``) selects between the
  three T4 (#451) recursive-CTE read shapes. The per-shape verbs are
  **not** registered as separate MCP tools — that would be the
  per-op-tool anti-pattern CLAUDE.md's "What MEHO is NOT" bullet 1
  forbids. ``topology.refresh`` is deliberately absent from the agent
  surface: it is the operator CLI verb ``meho topology refresh
  <target>`` (Initiative #363 item 10 amendment, 2026-05-14).
* ``list_targets`` — enumerate the operator's accessible infrastructure
  targets so an agent can pick a target before a ``call_operation`` or
  a ``query_topology`` call.

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

from typing import Any, Final

from sqlalchemy import select

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import Target as TargetORM
from meho_backplane.db.models import Tenant
from meho_backplane.mcp.registry import ToolDefinition, register_mcp_tool
from meho_backplane.mcp.server import McpInvalidParamsError
from meho_backplane.operations._lookup import parse_connector_id
from meho_backplane.topology.query import (
    AmbiguousNodeError,
    find_dependencies,
    find_dependents,
    find_path,
)

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


_QUERY_TOPOLOGY_INPUT_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "kind": {
            "type": "string",
            "enum": ["dependents", "dependencies", "path"],
            "description": (
                "Which traversal to run. `dependents` = reverse closure "
                "(what depends on `target`); `dependencies` = forward "
                "closure (what `target` depends on); `path` = shortest "
                "unweighted route between `from_name` and `to_name`."
            ),
        },
        "target": {
            "type": ["string", "null"],
            "description": (
                "Root node name for `dependents` / `dependencies`. "
                "Resolved against `graph_node.name` scoped to the "
                "operator's tenant. Required when `kind` is `dependents` "
                "or `dependencies`; ignored for `path`."
            ),
            "maxLength": 256,
        },
        "from_name": {
            "type": ["string", "null"],
            "description": (
                "Path start node name. Required when `kind` is `path`; ignored otherwise."
            ),
            "maxLength": 256,
        },
        "to_name": {
            "type": ["string", "null"],
            "description": (
                "Path end node name. Required when `kind` is `path`; ignored otherwise."
            ),
            "maxLength": 256,
        },
        "kind_filter": {
            "type": ["string", "null"],
            "description": (
                "Optional `graph_edge.kind` filter for `dependents` / "
                "`dependencies` (e.g. `runs-on`, `mounts`, "
                "`routes-through`, `belongs-to`). Restricts the walk to "
                "edges of that kind. Ignored for `path`."
            ),
            "maxLength": 64,
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
    "connectivity between two specific resources. Tenant-scoped "
    "automatically.\n\n"
    "WHEN TO CALL: before suggesting any delete/shutdown/detach — "
    "'is it safe to delete namespace customer-a-prod-foo?' → "
    "`query_topology {kind: dependents, target: customer-a-prod-foo}` "
    "returns every service / ingress / database that would break. Also "
    "for impact reasoning ('what does this VM run on?') and reachability "
    "('is there any route from this ingress to that datastore?').\n\n"
    "PARAMETRIC: `kind` is the discriminator — `dependents` / "
    "`dependencies` need `target`; `path` needs `from_name` + "
    "`to_name`. The three shapes are one tool, not three: there is no "
    "separate `topology.dependents` tool. If a name is ambiguous in the "
    "tenant (same name as both, e.g., a target and a vm) pass "
    "`node_kind` to pin the anchor; an ambiguous bare name returns "
    "-32602 naming the candidate kinds.\n\n"
    "Returns `{kind, nodes: [TopologyNode, ...]}` for the closure kinds "
    "(root at depth 0, so a one-element list means 'exists but nothing "
    "depends on it' and an empty list means 'no such node in this "
    'tenant\'); `{kind: "path", path: TopologyPath|null}` for `path` '
    "(null = unreachable within `max_hops`, a valid answer, not an "
    "error)."
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
                    "enum": ["dependents", "dependencies", "path"],
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
