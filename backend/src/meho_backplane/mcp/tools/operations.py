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
from meho_backplane.mcp.server import McpInvalidParamsError
from meho_backplane.operations.meta_tools import (
    ConnectorNotIngestedError,
    UnknownConnectorError,
    call_operation,
    list_operation_groups,
    search_operations,
)

__all__: list[str] = []


# ---------------------------------------------------------------------------
# Handler shims -- match the registry's ToolHandler type
# ---------------------------------------------------------------------------


def _connector_error_to_invalid_params(
    exc: UnknownConnectorError | ConnectorNotIngestedError,
) -> McpInvalidParamsError:
    """Map a connector-resolution domain error to a typed ``-32602``.

    The discovery meta-tools raise a :class:`ValueError` subclass when a
    ``connector_id`` does not resolve. Left to propagate, the dispatcher's
    generic ``except Exception`` would mistranslate it into an opaque
    ``-32603 "internal error: …"`` — exactly the trap #1482 removes.
    Catching it here and re-raising :class:`McpInvalidParamsError` flips
    the wire code to ``-32602 INVALID_PARAMS`` (the spec's "bad argument"
    code) and threads a machine-readable ``error.data`` discriminator so
    an agent can tell the two cases apart:

    * :class:`ConnectorNotIngestedError` →
      ``{"reason": "connector_not_ingested", "connector_id", "next_step"}``
      — the connector exists but awaits ingest; ``next_step.verb`` is the
      ``meho connector ingest …`` command to run.
    * :class:`UnknownConnectorError` →
      ``{"reason": "unknown_connector", "connector_id"}`` — no such
      connector on this deploy.
    """
    if isinstance(exc, ConnectorNotIngestedError):
        return McpInvalidParamsError(str(exc), data=exc.as_error_data())
    return McpInvalidParamsError(
        str(exc),
        data={"reason": "unknown_connector", "connector_id": exc.connector_id},
    )


async def _list_operation_groups_handler(
    operator: Operator,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    """Thin shim over :func:`list_operation_groups`.

    Translates the connector-resolution domain errors to a typed
    ``-32602`` (see :func:`_connector_error_to_invalid_params`) so a
    registered-but-not-ingested connector surfaces an actionable
    ``connector_not_ingested`` hint instead of an opaque ``-32603``
    (#1482).
    """
    try:
        return await list_operation_groups(operator, arguments)
    except (UnknownConnectorError, ConnectorNotIngestedError) as exc:
        raise _connector_error_to_invalid_params(exc) from exc


async def _search_operations_handler(
    operator: Operator,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    """Thin shim over :func:`search_operations`.

    Shares :func:`_list_operation_groups_handler`'s connector-error
    mapping so both discovery meta-tools surface the same typed
    ``-32602`` taxonomy (#1482).
    """
    try:
        return await search_operations(operator, arguments)
    except (UnknownConnectorError, ConnectorNotIngestedError) as exc:
        raise _connector_error_to_invalid_params(exc) from exc


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
            "product name. Returns groups in `group_key` order. An "
            "UNKNOWN connector_id is an error (no such connector, "
            "`-32602` with `data.reason=unknown_connector`); a "
            "REGISTERED-BUT-NOT-INGESTED connector is also an error but "
            "recoverable (`-32602` with `data.reason=connector_not_ingested` "
            "and `data.next_step.verb` = the `meho connector ingest …` "
            "command to run, then retry); a KNOWN connector with no "
            "enabled groups returns an empty list (operationally "
            "meaningful: it exists, nothing enabled yet). Pagination "
            "(G0.18-T5 #1358): keyset on `group_key`; "
            "default `limit=100`, max 500; pass the response's "
            "`next_cursor` back as the next call's `cursor` to fetch "
            "the next page. A `null` `next_cursor` is the end."
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
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 500,
                    "default": 100,
                    "description": (
                        "Page size. Default 100; max 500. Matches "
                        "`list_targets` paging — sibling list tools share "
                        "one upper bound (G0.18-T5 #1358)."
                    ),
                },
                "cursor": {
                    "type": ["string", "null"],
                    "description": (
                        "Keyset-pagination cursor: pass the last "
                        "`group_key` from the previous page to fetch the "
                        "next. Results are ordered by `group_key` "
                        "ascending. Matches `cursor` on `query_audit` / "
                        "`query_topology` / `list_targets` / "
                        "`meho.broadcast.recent` (G0.18-T5 #1358)."
                    ),
                    "maxLength": 256,
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
                "next_cursor": {
                    "type": ["string", "null"],
                    "description": (
                        "Keyset cursor for the next page (last "
                        "`group_key` on this page) or `null` when this "
                        "page is the end of the listing."
                    ),
                },
            },
            "required": ["connector_id", "groups", "next_cursor"],
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
            "product name); an unknown connector_id is an error "
            "(`-32602`, `data.reason=unknown_connector`), and a "
            "registered-but-not-ingested connector is a recoverable error "
            "(`-32602`, `data.reason=connector_not_ingested` + "
            "`data.next_step.verb` to run, then retry). An unknown group, "
            "by contrast, narrows the result set to zero hits and is not "
            "an error."
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
            "`target` (optional, accepts EITHER a bare string "
            '`"rdc-vcenter"` -- preferred forward shape, matches '
            "`query_topology` / `query_audit` -- OR a dict "
            '`{"name": "rdc-vcenter"}`; required for ops that act on a '
            "target), `params` (operation-specific). Returns the full "
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
                    "type": ["string", "object", "null"],
                    "description": (
                        "Target reference. Two shapes are accepted; "
                        "either reduces to the same dispatch:\n"
                        '  * Bare string -- e.g. `"rdc-vcenter"`. '
                        "The forward-preferred shape; matches "
                        "`query_topology` / `query_audit` so a target "
                        "name carried across read and write surfaces "
                        "needs no reshape.\n"
                        '  * Dict -- e.g. `{"name": "rdc-vcenter"}`. '
                        "The original shape; supports the optional "
                        "`fqdn` field below for per-call vhost "
                        "override. Use this form when you need the "
                        "override.\n"
                        "Pass null for operations that do not act on "
                        "a target. The dispatcher resolves `name` "
                        "against the targets registry; aliases are "
                        "accepted. See `docs/architecture/mcp.md` "
                        "('Target-reference shape convention') for "
                        "the cross-tool convention. The optional "
                        "`fqdn` field (dict-shape only) is a per-call "
                        "override for the resolved target's vhost "
                        "name; honoured by connectors that route by "
                        "`Host:` header (notably `vcfa-rest-9.0` "
                        "where reaching the appliance by IP without "
                        "an `fqdn` returns 404 with empty body)."
                    ),
                    "minLength": 1,
                    "properties": {
                        "name": {"type": "string", "minLength": 1},
                        "fqdn": {
                            "type": "string",
                            "minLength": 1,
                            "description": (
                                "Per-call override for the resolved "
                                "target's `fqdn` column. Threaded into "
                                "the connector for vhost routing; the "
                                "DB row is not modified. Dict-shape "
                                "only -- bare-string callers must "
                                "switch to the dict to opt in."
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
        # G0.15-T3 #1212 — finding 1: ``call_operation`` is a tool-call
        # envelope, not a domain operation. The actual mutation /
        # read-class of the inner op lives on the DISPATCH row the
        # dispatcher writes from inside the handler; the outer MCP
        # wrapper row's class must NOT shadow that with a fixed value
        # (the pre-#1212 ``"write"`` mis-classified every ``k8s.node.list``
        # / ``k8s.about`` invocation as a write at the audit-query layer).
        # ``"tool_call"`` is the agreed Option-A value from the issue
        # (the inner DISPATCH carries the truth; this is a filterable
        # envelope marker). ``classify_op`` in
        # :mod:`meho_backplane.broadcast.events` treats unknown classes
        # as ``other`` for redaction, which keeps the broadcast event's
        # full-detail shape for the envelope row — operators can still
        # see the request params on the SSE feed.
        op_class="tool_call",
    ),
    handler=_call_operation_handler,
)
