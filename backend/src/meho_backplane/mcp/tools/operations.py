# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Three MCP tools backing the G0.6 operation meta-tool surface.

G0.6-T8 (#399) of Initiative #388. Registers three MCP tools against
the G0.5 tool registry:

* ``list_operation_groups`` -- enumerate enabled operation groups for a
  connector. The agent uses this to decide *which group* to search
  within before issuing a query. ``required_role=OPERATOR``.
* ``search_operations`` -- hybrid BM25 + cosine RRF over
  ``endpoint_descriptor`` rows. The agent's primary discovery tool.
  ``required_role=OPERATOR``.
* ``call_operation`` -- invoke the dispatcher for a resolved op_id.
  ``required_role=OPERATOR``.

Tool descriptions are load-bearing
==================================

Per :doc:`../../../../../.claude/skills/implement-issue/ai_engineering_best_practices`
and the G0.5-T4 ``meho.status`` reference impl, the ``description``
field is the agent's prompt for *when to call this tool*. Imprecise
descriptions get tools called incorrectly or never invoked at all.
Each description below names:

1. **What the tool does** -- one sentence.
2. **When to call it** -- the discovery / dispatch flow the agent
   should follow.
3. **When NOT to call it** -- common failure modes (calling
   ``call_operation`` before ``search_operations`` returned a hit,
   passing ``limit=1`` and then complaining about miss rates, etc.).

These descriptions are part of the contract; rewording them is a
behavioural change that requires re-evaluating against the agent
recipe-completion bench (post-G6).

inputSchema / outputSchema
==========================

JSON-Schema 2020-12 fragments matching :class:`CallOperationBody` /
:class:`OperationGroupSummary` / :class:`OperationSearchHit` in
:mod:`meho_backplane.operations.meta_tools`. The MCP dispatcher
validates incoming ``tools/call.arguments`` against ``inputSchema``
before invoking the handler; the handler's return shape is documented
by ``outputSchema`` for client introspection (T4 ``meho.status``
showed the pattern). ``additionalProperties: false`` on every input
schema keeps the agent from passing unexpected fields the handler
would silently ignore.
"""

from __future__ import annotations

from typing import Any

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.mcp.registry import ToolDefinition, register_mcp_tool
from meho_backplane.operations.meta_tools import (
    call_operation,
    list_operation_groups,
    search_operations,
)

__all__: list[str] = []


# ---------------------------------------------------------------------------
# Handler shims -- match the registry's ToolHandler type
# ---------------------------------------------------------------------------


async def _list_operation_groups_handler(
    operator: Operator,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    """Thin shim over :func:`list_operation_groups`."""
    return await list_operation_groups(operator, arguments)


async def _search_operations_handler(
    operator: Operator,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    """Thin shim over :func:`search_operations`."""
    return await search_operations(operator, arguments)


async def _call_operation_handler(
    operator: Operator,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    """Thin shim over :func:`call_operation`."""
    return await call_operation(operator, arguments)


# ---------------------------------------------------------------------------
# Tool registrations -- side effects run on module import
# ---------------------------------------------------------------------------


register_mcp_tool(
    definition=ToolDefinition(
        name="list_operation_groups",
        description=(
            "List enabled operation groups for a connector. Each group "
            "carries a `when_to_use` blurb explaining what the group is "
            "for so you can pick the right group before searching its "
            "operations. Call this FIRST when you don't know which "
            "operation to invoke -- it narrows the search space from "
            "hundreds of operations to a handful of relevant ones. "
            "Argument: `connector_id` in `<impl_id>-<version>` form "
            '(e.g. "vmware-rest-9.0", "vault-1.x") -- NOT the bare '
            "product name. Returns groups in name order. An UNKNOWN "
            "connector_id is an error (no such connector); a KNOWN "
            "connector with no enabled groups returns an empty list "
            "(operationally meaningful: it exists, nothing enabled yet)."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "connector_id": {
                    "type": "string",
                    "description": (
                        "Connector identifier in the form "
                        '`<impl_id>-<version>` (e.g. "vmware-rest-9.0", '
                        '"vault-1.x") -- NOT the bare product name. A '
                        "value naming no registered connector is an error."
                    ),
                    "minLength": 1,
                },
            },
            "required": ["connector_id"],
            "additionalProperties": False,
        },
        outputSchema={
            "type": "object",
            "properties": {
                "connector_id": {"type": "string"},
                "groups": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "group_key": {"type": "string"},
                            "name": {"type": "string"},
                            "when_to_use": {"type": "string"},
                            "operation_count": {"type": "integer", "minimum": 0},
                        },
                        "required": [
                            "group_key",
                            "name",
                            "when_to_use",
                            "operation_count",
                        ],
                    },
                },
            },
            "required": ["connector_id", "groups"],
        },
        required_role=TenantRole.OPERATOR,
        op_class="read",
    ),
    handler=_list_operation_groups_handler,
)


register_mcp_tool(
    definition=ToolDefinition(
        name="search_operations",
        description=(
            "Hybrid BM25 + cosine retrieval over a connector's enabled "
            "operations. Use this AFTER `list_operation_groups` has "
            "narrowed the connector's surface to one group, or directly "
            "when the query is specific enough that a group filter would "
            "exclude relevant hits. Returns the top N operations ranked "
            "by combined lexical + semantic match. Inspect each hit's "
            "`safety_level` and `requires_approval` before calling "
            "`call_operation` on it. Arguments: `connector_id` (required), "
            "`query` (required, free-form), `group` (optional, narrows "
            "to that group's ops), `limit` (default 10, max 50). "
            "`connector_id` is `<impl_id>-<version>` (NOT the bare "
            "product name); an unknown connector_id is an error. An "
            "unknown group, by contrast, narrows the result set to "
            "zero hits and is not an error."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "connector_id": {
                    "type": "string",
                    "description": "Connector identifier; same shape as `list_operation_groups`.",
                    "minLength": 1,
                },
                "query": {
                    "type": "string",
                    "description": (
                        "Free-form query. Both BM25 (lexical) and cosine "
                        "(semantic) signals consume it; ranks are fused via RRF."
                    ),
                    "minLength": 1,
                },
                "group": {
                    "type": ["string", "null"],
                    "description": (
                        "Optional group_key filter. Narrows results to "
                        "operations whose `group_id` matches the named group."
                    ),
                },
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 50,
                    "default": 10,
                    "description": "Maximum number of ranked hits to return.",
                },
            },
            "required": ["connector_id", "query"],
            "additionalProperties": False,
        },
        outputSchema={
            "type": "object",
            "properties": {
                "hits": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "op_id": {"type": "string"},
                            "summary": {"type": ["string", "null"]},
                            "description": {"type": ["string", "null"]},
                            "group_key": {"type": ["string", "null"]},
                            "safety_level": {
                                "type": "string",
                                "enum": ["safe", "caution", "dangerous"],
                            },
                            "requires_approval": {"type": "boolean"},
                            "fused_score": {"type": "number"},
                            "bm25_score": {"type": ["number", "null"]},
                            "cosine_score": {"type": ["number", "null"]},
                        },
                        "required": [
                            "op_id",
                            "safety_level",
                            "requires_approval",
                            "fused_score",
                        ],
                    },
                },
                "query_duration_ms": {"type": "number"},
            },
            "required": ["hits", "query_duration_ms"],
        },
        required_role=TenantRole.OPERATOR,
        op_class="read",
    ),
    handler=_search_operations_handler,
)


register_mcp_tool(
    definition=ToolDefinition(
        name="call_operation",
        description=(
            "Invoke an operation. Use ONLY after `search_operations` has "
            "returned an op_id and you've confirmed the operation is "
            "appropriate (check `safety_level` and `requires_approval` "
            "on the hit). The dispatcher validates `params` against the "
            "operation's parameter_schema and either returns the result "
            '(`status="ok"`) or a structured error in the same envelope '
            '(`status="error"` + `error="<code>: ..."`). DO NOT retry '
            "an `invalid_params` error verbatim -- inspect "
            "`extras.validation_errors` and fix the params shape first. "
            "Arguments: `connector_id` (required), `op_id` (required), "
            '`target` (optional partial descriptor like `{"name": '
            '"rdc-vcenter"}`; required for ops that act on a target), '
            "`params` (operation-specific). Returns the full "
            "OperationResult shape."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "connector_id": {"type": "string", "minLength": 1},
                "op_id": {
                    "type": "string",
                    "minLength": 1,
                    "description": (
                        "Operation id as returned by `search_operations`. "
                        'Examples: "GET:/api/vcenter/cluster", '
                        '"vault.kv.read", "vmware.composite.vm.create".'
                    ),
                },
                "target": {
                    "type": ["object", "null"],
                    "description": (
                        "Partial target descriptor. The dispatcher "
                        "resolves the `name` field against the targets "
                        "registry; aliases are accepted. Pass null "
                        "for operations that do not act on a target. "
                        "NOTE: this tool's `target` is a dict "
                        '({"name": "<target-name>"}) because the '
                        "dispatcher reserves room for additional "
                        "future fields (e.g. an alias-precedence pin); "
                        "the topology / audit read tools (`query_topology`, "
                        "`query_audit`) take a bare-string `target` "
                        "since they only need the name. See "
                        "`docs/architecture/mcp.md` ('Target-reference "
                        "shape convention') for the canonical forward "
                        "convention any new tool should follow. The "
                        "optional `fqdn` field is a per-call override "
                        "for the resolved target's vhost name; honoured "
                        "by connectors that route by `Host:` header "
                        "(notably `vcfa-rest-9.0` where reaching the "
                        "appliance by IP without an `fqdn` returns 404 "
                        "with empty body)."
                    ),
                    "properties": {
                        "name": {"type": "string", "minLength": 1},
                        "fqdn": {
                            "type": "string",
                            "minLength": 1,
                            "description": (
                                "Per-call override for the resolved "
                                "target's `fqdn` column. Threaded into "
                                "the connector for vhost routing; the "
                                "DB row is not modified."
                            ),
                        },
                    },
                },
                "params": {
                    "type": "object",
                    "description": (
                        "Operation-specific parameters. The dispatcher "
                        "validates against the operation's parameter_schema "
                        "before invoking the handler; unknown fields are "
                        "rejected at the schema layer."
                    ),
                },
            },
            "required": ["connector_id", "op_id"],
            "additionalProperties": False,
        },
        outputSchema={
            "type": "object",
            "properties": {
                "status": {
                    "type": "string",
                    "enum": ["ok", "error", "denied"],
                },
                "op_id": {"type": "string"},
                "result": {
                    "description": "Operation payload on success; null on error.",
                },
                "error": {"type": ["string", "null"]},
                "duration_ms": {"type": "number"},
                "extras": {"type": "object"},
            },
            "required": ["status", "op_id", "duration_ms"],
        },
        required_role=TenantRole.OPERATOR,
        op_class="write",
    ),
    handler=_call_operation_handler,
)
