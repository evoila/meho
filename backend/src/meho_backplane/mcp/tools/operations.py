# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Four MCP tools backing the G0.6 operation meta-tool surface.

G0.6-T8 (#399) of Initiative #388. Registers four MCP tools against
the G0.5 tool registry:

* ``list_operation_groups`` -- enumerate enabled operation groups for a
  connector. The agent uses this to decide *which group* to search
  within before issuing a query. ``required_role=OPERATOR``.
* ``search_operations`` -- hybrid BM25 + cosine RRF over
  ``endpoint_descriptor`` rows. The agent's primary discovery tool.
  ``required_role=OPERATOR``.
* ``call_operation`` -- invoke the dispatcher for a resolved op_id.
  ``required_role=OPERATOR``.
* ``preview_operation`` -- resolve the same op + target + params a
  ``call_operation`` would, and return the literal would-be HTTP
  request (for ingested HTTP operations) without dispatching it. ``required_role=OPERATOR``.

Tool descriptions are load-bearing
==================================

Per the G0.5-T4 ``meho_status`` reference impl, the ``description``
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
behavioural change. Measure a description change on the #4067 job
list with the runner that #4253 builds -- the same rule the tool-text
tasks #4111 and #4112 follow.

inputSchema / outputSchema
==========================

JSON-Schema 2020-12 fragments matching :class:`CallOperationBody` /
:class:`OperationGroupSummary` / :class:`OperationSearchHit` in
:mod:`meho_backplane.operations.meta_tools`. The MCP dispatcher
validates incoming ``tools/call.arguments`` against ``inputSchema``
before invoking the handler; the handler's return shape is documented
by ``outputSchema`` for client introspection (T4 ``meho_status``
showed the pattern). ``additionalProperties: false`` on every input
schema keeps the agent from passing unexpected fields the handler
would silently ignore.
"""

from __future__ import annotations

from typing import Any

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.mcp.registry import ToolDefinition, ToolSurface, register_mcp_tool
from meho_backplane.mcp.server import McpInvalidParamsError
from meho_backplane.mcp.tools._call_operation_async import poll_call, submit_async_call
from meho_backplane.operations.meta_tools import (
    SEARCH_NARROWING_MAX,
    SEARCH_OUTCOMES,
    ConnectorNotIngestedError,
    UnknownConnectorError,
    UnknownOperationError,
    call_operation,
    list_operation_groups,
    preview_operation,
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
    ``-32602`` taxonomy (#1482). An unknown ``op_id`` in exact-op mode
    (#3668) maps the same way, with ``data.reason=unknown_op``.
    """
    try:
        return await search_operations(operator, arguments)
    except (UnknownConnectorError, ConnectorNotIngestedError) as exc:
        raise _connector_error_to_invalid_params(exc) from exc
    except UnknownOperationError as exc:
        raise McpInvalidParamsError(str(exc), data=exc.as_error_data()) from exc


async def _call_operation_handler(
    operator: Operator,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    """Shim over :func:`call_operation`, plus async mode (#3695).

    ``async`` and ``run_id`` are MCP controls, not dispatch arguments, so
    they are taken out before the call reaches :func:`call_operation`.
    """
    call_arguments = dict(arguments)
    run_async = bool(call_arguments.pop("async", False))
    run_id = call_arguments.pop("run_id", None)
    if run_id is not None:
        if run_async:
            raise McpInvalidParamsError(
                "send either `async: true` (start a call) or `run_id` (check a call), not both",
                data={"reason": "async_with_run_id"},
            )
        return await poll_call(operator, call_arguments, str(run_id))
    if run_async:
        return await submit_async_call(operator, call_arguments)
    return await call_operation(operator, call_arguments)


async def _preview_operation_handler(
    operator: Operator,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    """Thin shim over :func:`preview_operation` (#1683)."""
    return await preview_operation(operator, arguments)


# ---------------------------------------------------------------------------
# Tool registrations -- side effects run on module import
# ---------------------------------------------------------------------------


register_mcp_tool(
    definition=ToolDefinition(
        feature="typed_connector_reads",
        name="list_operation_groups",
        surface=ToolSurface.WORKING,
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
            "meaningful: it exists, nothing enabled yet). A group whose "
            "own review is still staged but that holds ≥1 per-op-enabled "
            "operation IS listed, flagged `partial=true` with a non-zero "
            "`enabled_op_count` — only those ops are live and "
            "dispatchable; `search_operations` will find them. Pagination: "
            "keyset on `group_key`; "
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
                        "one upper bound."
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
                        "`meho_broadcast_recent`."
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
                            "enabled_op_count": {
                                "type": "integer",
                                "minimum": 0,
                                "description": (
                                    "Count of this group's enabled (live, "
                                    "dispatchable) operations. Equal to "
                                    "`operation_count`; named explicitly so a "
                                    "`partial` group's live-op count reads "
                                    "unambiguously."
                                ),
                            },
                            "partial": {
                                "type": "boolean",
                                "description": (
                                    "True when the group itself is NOT "
                                    "`review_status=enabled` but holds ≥1 "
                                    "per-op-enabled operation (so it is "
                                    "surfaced here solely on per-op "
                                    "enablement). False for a fully-enabled "
                                    "group. When true, `enabled_op_count` is "
                                    "≥1 and only those ops are live; the "
                                    "rest of the group is still staged."
                                ),
                            },
                        },
                        "required": [
                            "group_key",
                            "name",
                            "when_to_use",
                            "operation_count",
                            "enabled_op_count",
                            "partial",
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


_SAFETY_LEVEL_SCHEMA: dict[str, Any] = {
    "type": "string",
    "enum": ["safe", "caution", "dangerous", "destructive"],
}

#: Ranked mode: slim hits. The three score fields appear only when a
#: tenant_admin caller sets ``include_scores`` (#3668).
_SEARCH_RANKED_OUTPUT_SCHEMA: dict[str, Any] = {
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
                    "safety_level": _SAFETY_LEVEL_SCHEMA,
                    "requires_approval": {"type": "boolean"},
                    "fused_score": {"type": "number"},
                    "bm25_score": {"type": ["number", "null"]},
                    "cosine_score": {"type": ["number", "null"]},
                },
                "required": ["op_id", "safety_level", "requires_approval"],
            },
        },
        "query_duration_ms": {"type": "number"},
        "outcome": {"type": "string", "enum": list(SEARCH_OUTCOMES)},
        "advisory": {"type": ["string", "null"]},
        "narrowing": {
            "type": "array",
            "items": {"type": "string"},
            "maxItems": SEARCH_NARROWING_MAX,
        },
    },
    "required": ["hits", "query_duration_ms", "outcome", "narrowing"],
}

#: Exact-op mode: one call contract (``operations._contract.OperationContract``).
_SEARCH_EXACT_OP_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "operation": {
            "type": "object",
            "properties": {
                "connector_id": {"type": "string"},
                "op_id": {"type": "string"},
                "summary": {"type": ["string", "null"]},
                "description": {"type": ["string", "null"]},
                "group_key": {"type": ["string", "null"]},
                "safety_level": _SAFETY_LEVEL_SCHEMA,
                "requires_approval": {"type": "boolean"},
                "parameter_schema": {"type": "object"},
                "examples": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "source": {"type": "string"},
                            "param": {"type": ["string", "null"]},
                            "value": {},
                        },
                        "required": ["source", "param", "value"],
                    },
                },
                "target": {
                    "type": "object",
                    "properties": {
                        "required": {"type": "boolean"},
                        "resolves_by": {"type": "string", "enum": ["name"]},
                        "guidance": {"type": "string"},
                    },
                    "required": ["required", "resolves_by", "guidance"],
                },
                "result_shape": {
                    "type": "object",
                    "properties": {
                        "envelope_fields": {"type": "array", "items": {"type": "string"}},
                        "guidance": {"type": "string"},
                    },
                    "required": ["envelope_fields", "guidance"],
                },
                "pagination_hint": {"type": ["object", "null"]},
                "recovery": {"type": "array", "items": {"type": "string"}},
            },
            "required": [
                "connector_id",
                "op_id",
                "safety_level",
                "requires_approval",
                "parameter_schema",
                "examples",
                "target",
                "result_shape",
                "pagination_hint",
                "recovery",
            ],
        },
    },
    "required": ["operation"],
}


register_mcp_tool(
    definition=ToolDefinition(
        feature="typed_connector_reads",
        name="search_operations",
        surface=ToolSurface.WORKING,
        description=(
            "Hybrid BM25 + cosine retrieval over a connector's enabled "
            "operations. Use this AFTER `list_operation_groups` has "
            "narrowed the connector's surface to one group, or directly "
            "when the query is specific enough that a group filter would "
            "exclude relevant hits. Returns the top N operations ranked "
            "by combined lexical + semantic match. Inspect each hit's "
            "`safety_level` and `requires_approval` before calling "
            "`call_operation` on it. Arguments: `connector_id` (required), "
            "`query` (free-form), `group` (optional, narrows "
            "to that group's ops), `limit` (default 10, max 50). "
            "EXACT-OP MODE: once you have an `op_id`, call again with "
            "`op_id` (instead of `query`) BEFORE `call_operation`. You get "
            "back one `operation` object, not ranked hits: the exact "
            "`parameter_schema` `call_operation` checks `params` against, "
            "small examples, whether a `target` is needed (pass the "
            "target's NAME, not the id `list_targets` returns), the result "
            "shape (large results come back as a `handle` you read with "
            "`result_query`), and retry rules. An unknown op_id is an "
            "error (`-32602`, `data.reason=unknown_op`). "
            "`connector_id` is `<impl_id>-<version>` (NOT the bare "
            "product name); an unknown connector_id is an error "
            "(`-32602`, `data.reason=unknown_connector`), and a "
            "registered-but-not-ingested connector is a recoverable error "
            "(`-32602`, `data.reason=connector_not_ingested` + "
            "`data.next_step.verb` to run, then retry). Every other result "
            "carries `outcome`: `matched` (the top hit matched your words "
            "and is clearly ahead; call it), `ambiguous` (hits are weak or "
            "nearly tied: do not call any of them, narrow the search "
            "first), `no_match` (nothing matched; do "
            "not call an operation this search did not return), "
            "`unknown_group` (the group does not exist; `narrowing` lists "
            "valid group keys), or `zero_enabled_operations` (nothing on "
            "this connector is enabled; do not call it, an operator must "
            "enable operations). `advisory` explains any outcome other than "
            "`matched`; `narrowing` lists next steps."
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
                "op_id": {
                    "type": "string",
                    "minLength": 1,
                    "description": (
                        "Exact-op mode: return this one operation's call "
                        "contract instead of ranked hits. `query`, `group` "
                        "and `limit` are ignored when it is set."
                    ),
                },
                "include_scores": {
                    "type": "boolean",
                    "default": False,
                    "description": (
                        "Add ranking scores to each ranked hit. Only "
                        "honoured for a tenant_admin caller; ignored "
                        "otherwise. For tuning descriptions, not for "
                        "picking an operation."
                    ),
                },
            },
            "required": ["connector_id"],
            # Either `query` (ranked mode) or `op_id` (exact-op mode) is
            # needed. `anyOf` is stripped from the wire schema (the
            # Anthropic Messages API rejects top-level combinators), so
            # the description carries the rule to agents; the server-side
            # jsonschema check in mcp/handlers.py still enforces it.
            "anyOf": [
                {"required": ["query"]},
                {"required": ["op_id"]},
            ],
            "additionalProperties": False,
        },
        # Two shapes (#3668): ranked hits, or one exact-op contract. Same
        # top-level ``oneOf`` idiom as ``query_audit``'s flat|replay union.
        outputSchema={
            "type": "object",
            "oneOf": [_SEARCH_RANKED_OUTPUT_SCHEMA, _SEARCH_EXACT_OP_OUTPUT_SCHEMA],
        },
        required_role=TenantRole.OPERATOR,
        op_class="read",
    ),
    handler=_search_operations_handler,
)


register_mcp_tool(
    definition=ToolDefinition(
        feature="typed_connector_reads",
        name="call_operation",
        surface=ToolSurface.WORKING,
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
            "OperationResult shape. For a long call (a deploy or another "
            "multi-step operation that can take minutes) pass `async: true`: "
            'you get `status="pending"` and `extras.run_id` at once, and the '
            "call runs in the background. Then call `call_operation` again "
            "with the same `connector_id`, `op_id` and that `run_id` to check "
            "it; once it is done you get its normal result."
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
                        "rejected at the schema layer. One reserved key is "
                        "handled by the dispatcher itself and never reaches "
                        "the op schema: `reveal_secret` (bool). A "
                        "credential_read op (e.g. `vault.kv.read`) returns a "
                        "key-name-scrubbed response by default -- secret "
                        "values are replaced so they never enter this agent "
                        "transcript. Pass `reveal_secret: true` ONLY when you "
                        "must pipe the raw value onward; the raw value is "
                        "then returned AND the reveal is stamped on the audit "
                        "row. Prefer `secret.move` to relocate a credential "
                        "without it transiting your context."
                    ),
                },
                "work_ref": {
                    "type": "string",
                    "minLength": 1,
                    "description": (
                        "Optional external change-ticket reference for "
                        "this operation -- an opaque typed-URI string "
                        '(e.g. `"gh:evoila/meho#7"`, a Jira key, a CR '
                        "id) correlating the governed write to the "
                        "change record that authorised it. Stamped onto "
                        "the operation's `audit_log.work_ref` so "
                        "`meho audit query --work-ref ...` can later find "
                        "every row bound to that ticket. Scoped to this "
                        "single call (no leakage to the next); a per-op "
                        "value here overrides any ambient `Meho-Work-Ref` "
                        "request header. Opaque -- not validated and "
                        "never sent to the tracker."
                    ),
                },
                "preview_hash": {
                    "type": "string",
                    "minLength": 1,
                    "description": (
                        "Preview-result-hash binding for a destructive-tier "
                        "delete. REQUIRED when the op is "
                        "`safety_level='destructive'`: first call "
                        "`preview_operation` with the identical `connector_id` "
                        "/ `op_id` / `target` / `params`, take the `preview_hash` "
                        "it returns, and pass it here. The dispatcher recomputes "
                        "the preview server-side and refuses to queue the "
                        "approval (fail-closed) unless the hashes match — so "
                        "the human approver sees exactly what will be destroyed. "
                        "Ignored for every non-destructive op."
                    ),
                },
                "idempotency_key": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 200,
                    "description": (
                        "Optional retry key for a call that waits for approval. "
                        "Send the same key with the identical call again (for "
                        "example after a restart) and you get the same "
                        "`approval_request_id` back "
                        "(`extras.approval_request_reused: true`) instead of a "
                        "second approval request. The same key with a different "
                        "call is refused with `idempotency_key_conflict`."
                    ),
                },
                "async": {
                    "type": "boolean",
                    "default": False,
                    "description": (
                        "Start the call in the background. Use it for long "
                        "calls (deploys, multi-step operations) that can take "
                        "minutes. The answer comes at once with "
                        '`status="pending"` and `extras.run_id`; the policy '
                        "checks, approvals and audit rows are the same as for "
                        "a normal call. Check the call later with `run_id`."
                    ),
                },
                "run_id": {
                    "type": "string",
                    "format": "uuid",
                    "description": (
                        "Check a call you started with `async: true`. Send the "
                        "same `connector_id` and `op_id` and the `extras.run_id` "
                        'you got. While it runs: `status="pending"` or '
                        '`"running"`, with `extras.started_at`. When it is done: '
                        "the call's normal result. A run that failed or was "
                        'cancelled gives `status="error"`. Nothing is run again.'
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
                    # ``awaiting_approval`` is the parked outcome the
                    # dispatcher returns for an approval-gated op that has
                    # not yet been approved (G11.7-T1 #1401). It is a
                    # first-class non-error result, so a spec-compliant MCP
                    # client validating the structured result against this
                    # outputSchema (MCP 2025-06-18: "Clients SHOULD
                    # validate structured results against this schema")
                    # must accept it. ``rate_limited`` is the refusal the
                    # dispatcher returns when the caller is over its
                    # per-principal rate or concurrency cap (#3500); the
                    # envelope carries ``extras.retry_after_seconds``.
                    # ``pending`` / ``running`` are the async-mode answers
                    # (#3695): a call started with ``async: true`` that has
                    # not finished yet. The enum lists only statuses this
                    # tool actually emits at this boundary (no
                    # ``already_resumed`` — that one is approval-resume only).
                    "enum": [
                        "ok",
                        "error",
                        "denied",
                        "awaiting_approval",
                        "rate_limited",
                        "pending",
                        "running",
                    ],
                },
                "op_id": {"type": "string"},
                "result": {
                    "description": "Operation payload on success; null on error.",
                },
                "error": {"type": ["string", "null"]},
                "duration_ms": {"type": "number"},
                "audit_id": {
                    "type": ["string", "null"],
                    "format": "uuid",
                    "description": ("Committed audit receipt; null when a row cannot be promised."),
                },
                "delivery": {
                    "type": ["string", "null"],
                    "enum": ["complete", "partial", "unavailable", None],
                    "description": (
                        "Response delivery state, separate from whether the operation executed. "
                        "`partial`: the response was too large to deliver; read the result "
                        "through `result_query` with `handle`. `unavailable`: no result was "
                        "delivered. In both cases the operation already ran; do not re-invoke it."
                    ),
                },
                # ``handle`` was missing from the declared properties
                # (#2774 schema-honesty sweep): the payload is
                # ``OperationResult.model_dump`` verbatim, which always
                # carries the JSONFlux spill signal — null when the
                # response was inlined, a ResultHandle object when the
                # full set-shaped payload is addressable via
                # ``result_query`` instead.
                "handle": {
                    "type": ["object", "null"],
                    "description": (
                        "JSONFlux result handle. Non-null when the raw "
                        "response was set-shaped and spilled out-of-band; "
                        "drill in via `result_query` with `handle_id`. "
                        "A large response with no single list is kept as one "
                        "document (`result.result_record` is `document`); "
                        "read it with `query.path`."
                    ),
                },
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


register_mcp_tool(
    definition=ToolDefinition(
        feature="typed_connector_reads",
        name="preview_operation",
        surface=ToolSurface.WORKING,
        description=(
            "Preview an operation WITHOUT running it. Resolves the same "
            "op + target + params `call_operation` would and returns the "
            "literal would-be HTTP request -- `method`, `resolved_path` "
            "(path placeholders substituted + connector mount prefix "
            "applied), `query`, and a REDACTED `redacted_body` -- instead "
            "of dispatching. Use this to diagnose a write failure: when a "
            "`call_operation` write returned a 4xx (e.g. a gh-rest 422 / "
            "403), re-issue the SAME arguments here to read back exactly "
            "what would be put on the wire (the operation audit persists "
            "only a hashed params_hash, so the request shape is otherwise "
            "unrecoverable). Inspection only -- it never sends the request "
            "and never re-dispatches a past one. Covers ingested HTTP ops "
            "(`source_kind='ingested'`). A typed/composite op returns "
            '`status="unavailable"` (no single literal HTTP request), '
            "except a destructive or approval-gated one: it returns "
            '`status="ok"`, `method="COMPOSITE"`, the redacted params and '
            "a `preview_hash`. It shows the plan as `proposed_effect` only "
            "when the plan can be built without contacting the target; "
            "otherwise `plan_shown` is false and `plan_note` says the plan "
            "is shown on the approval request instead. An approval-gated op "
            "that writes a credential (for example `vault.kv.put`) is NOT "
            'previewable and stays `status="unavailable"`, so its secret '
            "params never surface. Every preview whose op resolves (ok, "
            "unavailable, and the error statuses after the op lookup) also "
            "says the risk level and whether this call will wait for "
            "approval: `safety_level`, `requires_approval`, and "
            "`policy_verdict` (what MEHO would do with the call for THIS "
            "caller and target: `auto-execute`, `needs-approval` or `deny`, "
            "with `policy_reason` when it is not auto-execute). The verdict "
            "is decided the same way `call_operation` decides it, but "
            "nothing is written. Arguments mirror "
            "`call_operation`: "
            "`connector_id` (required), `op_id` (required), `target` "
            '(optional; bare string `"rdc-vcenter"` or dict '
            '`{"name": "rdc-vcenter"}`), `params` (operation-specific).'
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
                        'Examples: "POST:/repos/{owner}/{repo}/issues", '
                        '"GET:/api/vcenter/cluster".'
                    ),
                },
                "target": {
                    "type": ["string", "object", "null"],
                    "description": (
                        "Target reference. Same two shapes as "
                        '`call_operation`: a bare string `"rdc-vcenter"` '
                        'or a dict `{"name": "rdc-vcenter"}` (with an '
                        "optional `fqdn` override). Pass null for ops that "
                        "do not act on a target. Resolving the target lets "
                        "the preview reflect the same connector + mount "
                        "prefix the real dispatch would hit."
                    ),
                    "minLength": 1,
                    "properties": {
                        "name": {"type": "string", "minLength": 1},
                        "fqdn": {
                            "type": "string",
                            "minLength": 1,
                            "description": (
                                "Per-call override for the resolved "
                                "target's `fqdn` column; threaded into the "
                                "connector for vhost routing. Dict-shape "
                                "only."
                            ),
                        },
                    },
                },
                "params": {
                    "type": "object",
                    "description": (
                        "Operation-specific parameters. Validated against "
                        "the operation's parameter_schema before the "
                        "request is resolved; an invalid shape returns "
                        '`status="error"` + `extras.validation_errors` '
                        "(the same shape `call_operation` rejects with)."
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
                    "enum": ["ok", "error", "unavailable"],
                },
                "op_id": {"type": "string"},
                "connector_id": {"type": "string"},
                "source_kind": {"type": "string"},
                "method": {
                    "type": "string",
                    "description": "Resolved HTTP verb (present on status=ok).",
                },
                "resolved_path": {
                    "type": "string",
                    "description": (
                        "Fully resolved request path -- placeholders "
                        "substituted, mount prefix applied (status=ok)."
                    ),
                },
                "query": {
                    "type": ["object", "null"],
                    "description": "Query-string params, or null (status=ok).",
                },
                "redacted_body": {
                    "description": (
                        "The would-be JSON request body after the "
                        "connector-boundary redaction pipeline; null when "
                        "the op declares no body (status=ok)."
                    ),
                },
                "preview_hash": {
                    "type": "string",
                    "description": (
                        "Preview-result-hash binding (status=ok): a "
                        "stable SHA-256 over the resolved request. Present it "
                        "as `call_operation`'s `preview_hash` arg to govern a "
                        "destructive-tier delete."
                    ),
                },
                "safety_level": {
                    "type": "string",
                    "description": (
                        "The op's risk level: safe, caution, dangerous, or "
                        "destructive. Present on every preview whose op "
                        "resolved (every status except unknown_op / "
                        "unknown_connector)."
                    ),
                },
                "requires_approval": {
                    "type": "boolean",
                    "description": (
                        "True when the op is marked as needing a human "
                        "approval before it runs. Present whenever "
                        "`safety_level` is."
                    ),
                },
                "policy_verdict": {
                    "type": "string",
                    "enum": ["auto-execute", "needs-approval", "deny"],
                    "description": (
                        "What MEHO would do with this call for this caller "
                        "and target: run it at once (auto-execute), wait for "
                        "a second person's approval (needs-approval), or "
                        "refuse it (deny). Decided the same way "
                        "`call_operation` decides it; nothing is written. "
                        "Present whenever `safety_level` is."
                    ),
                },
                "policy_reason": {
                    "type": ["string", "null"],
                    "description": (
                        "The policy gate's reason for the verdict; null when "
                        "it gives none (a person's call that runs at once)."
                    ),
                },
                "proposed_effect": {
                    "type": "object",
                    "description": (
                        "The plan of what the operation will change, the "
                        "same block the approval request shows. Present only "
                        "on a typed/composite preview whose plan can be built "
                        "without contacting the target."
                    ),
                },
                "plan_shown": {
                    "type": "boolean",
                    "description": (
                        "Typed/composite preview only: true when "
                        "`proposed_effect` is present. False means the plan "
                        "needs the target and is shown only on the approval "
                        "request."
                    ),
                },
                "plan_note": {
                    "type": "string",
                    "description": "Plain reason the plan is not shown (when plan_shown is false).",
                },
                "error": {"type": ["string", "null"]},
                "extras": {"type": "object"},
            },
            "required": ["status", "op_id", "connector_id"],
        },
        required_role=TenantRole.OPERATOR,
        # A read-only request-shape inspection. No dispatch, no audit row,
        # no mutation -- ``"read"`` matches the actual op-class (unlike
        # ``call_operation``'s ``"tool_call"`` envelope, which wraps a
        # dispatch whose true class lives on the inner DISPATCH row).
        op_class="read",
    ),
    handler=_preview_operation_handler,
)
