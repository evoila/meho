# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Admin MCP tools for the connector review + state-machine surface (G0.7-T7).

Eight tools under the ``meho_connector_*`` namespace, deliberately
separate from the agent-surface meta-tools
(:mod:`~meho_backplane.mcp.tools.operations`):

* ``meho_connector_list`` — list ingested connectors. **operator**.
* ``meho_connector_review`` — get the review payload. **operator**.
* ``meho_connector_edit_group`` — edit one group. **tenant_admin**.
* ``meho_connector_edit_op`` — edit one op override. **tenant_admin**.
* ``meho_connector_enable`` — flip the connector to enabled. **tenant_admin**.
* ``meho_connector_enable_reads`` — bulk-enable every read-class
  (GET/HEAD) ingested op; writes stay default-deny (G0.25-T7 #1749).
  **tenant_admin**.
* ``meho_connector_disable`` — flip the connector to disabled. **tenant_admin**.
* ``meho_connector_delete`` — delete the connector's rows + auto-shim
  registration (G0.25-T2 #1700). **tenant_admin**.

The ingest-pipeline tools (``meho_connector_ingest`` +
``meho_connector_ingest_status``) live in the sibling
:mod:`meho_backplane.mcp.tools.connector_ingest` module; the two files
split the admin tools by responsibility (pipeline vs. review) so
neither grows past the code-quality file-size budget. The shared
``connector_id`` / ``tenant_id`` schema snippets, op-class taxonomy
strings, and JSON-safe serialiser live in
:mod:`meho_backplane.mcp.tools._connector_shared`.

Why these are not in the agent surface
======================================

Per ``CLAUDE.md`` postulate 5 and the "What MEHO is NOT" section: the
agent surface is the ~17 meta-tools (``search_connectors``,
``call_operation``, etc.). Vendor-specific identifiers and
administrative verbs never reach the agent's tool list. These admin
tools live in the ``meho.*`` namespace and are visible only to
``tenant_admin`` (mutators) or ``operator`` (the two read tools), so an
agent dispatched with ``read_only`` role sees none of them and an
agent with the default ``operator`` role sees only the two read
helpers.

That partition is enforced by the registry's
:func:`~meho_backplane.mcp.registry.all_tools_for` filter, which lists
each tool only when the requesting operator's
:class:`~meho_backplane.auth.operator.TenantRole` meets or exceeds the
tool's :attr:`~meho_backplane.mcp.registry.ToolDefinition.required_role`.
The dispatcher repeats the check at call time (see
:func:`~meho_backplane.mcp.handlers.handle_tools_call`), so a tool
that's hidden from ``tools/list`` is also un-callable if a client
guesses its name.

Tool wiring discipline
======================

Each tool's handler is a thin shim that wraps the canonical
service layer T5 (CLI) and T6 (REST routes) also consume — there
is no parallel "admin service" class here:

1. ``meho_connector_list`` calls the
   :func:`list_ingested_connectors` query helper directly.
2. Every other tool constructs :class:`ReviewService` and
   delegates to its existing read / edit / state-machine methods.

PATCH-style handlers (``edit_group`` / ``edit_op``) build the
service-layer kwargs dict from ``if "field" in arguments``
key-presence checks so that an omitted field never reaches
:class:`ReviewService`; otherwise ``arguments.get(...)`` would make
"omitted" and "explicit ``null``" indistinguishable and the
PATCH semantics would blur. Only fields the operator explicitly
named are forwarded.

Responses are ``model_dump(mode="json")``-ed so the dispatcher
can wrap them in the MCP ``content`` array. Keeping the dispatch
logic single-source — same service classes the REST router calls —
is the contract called out in #407's task body and the T6 merge
(#488) that promoted ``IngestionPipelineService`` /
``list_ingested_connectors`` / ``api_schemas.IngestRequest`` to the
shared public surface.

Per-tool ``inputSchema`` notes
==============================

Every schema uses ``additionalProperties: false`` (issue AC 4). The
``connector_id`` field appears on all six tools and is documented
identically across them via the shared
:data:`~meho_backplane.mcp.tools._connector_shared._CONNECTOR_ID_DESCRIPTION`
snippet: an operator-facing ``<impl_id>-<version>`` string.

The ``tenant_id`` field is optional everywhere. ``null`` / omitted →
the built-in scope, which is accessible only to ``tenant_admin`` per
the service layer's authorisation rule. Tenant operators pass their
own tenant's UUID when they want to operate on their own
tenant-curated connectors.
"""

from __future__ import annotations

from typing import Any, Final

import structlog

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.mcp.registry import (
    READ_ONLY,
    ToolDefinition,
    ToolSurface,
    register_mcp_tool,
)
from meho_backplane.mcp.tools._connector_shared import (
    _CONNECTOR_ID_DESCRIPTION,
    _OP_CLASS_READ,
    _OP_CLASS_WRITE,
    _PREFER_PROPERTY,
    _TENANT_ID_PROPERTY,
    _coerce_prefer,
    _coerce_tenant_id,
    _model_dump_json_safe,
    raise_invalid_params_for_ambiguous_scope,
    raise_invalid_params_for_builtin_write_forbidden,
    raise_invalid_params_for_connector_in_use,
    raise_invalid_params_for_connector_not_found,
    raise_invalid_params_for_edit_value_error,
    raise_invalid_params_for_legacy_tenant_copy,
)
from meho_backplane.operations.ingest import (
    AmbiguousConnectorScopeError,
    BuiltinConnectorWriteForbiddenError,
    ConnectorInUseError,
    ConnectorListItem,
    ConnectorNotFoundError,
    ConnectorStatusFilter,
    LegacyTenantConnectorCopyError,
    ReviewService,
    list_ingested_connectors,
)
from meho_backplane.operations.jsonflux_reducer import JsonFluxReducer

__all__ = [
    "CONNECTOR_LIST_NAME",
    "list_connector_rows",
    "reduce_connector_list",
]

CONNECTOR_LIST_NAME: Final[str] = "meho_connector_list"

_log = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Handler implementations
# ---------------------------------------------------------------------------


#: Same reducer class and default thresholds (more than 50 rows or 4 KB)
#: ``call_operation`` and ``list_targets`` use (CLAUDE.md postulate 6).
_CONNECTOR_LIST_REDUCER: Final[JsonFluxReducer] = JsonFluxReducer()

#: ``fetch_more.native_pagination`` for a reduced list: how to ask again
#: for a list short enough to come back inline.
_CONNECTOR_LIST_PAGINATION_HINT: Final[dict[str, Any]] = {
    "params": {
        "product": (
            "Exact product name from a row's `product` field. One product's "
            "connectors usually come back inline as `connectors`."
        ),
    },
    "example_next_call": {
        "tool": CONNECTOR_LIST_NAME,
        "args": {"product": "<product>"},
    },
}


def connector_review_status(item: ConnectorListItem) -> str:
    """Return the one-word review status of a connector row.

    The words match the ``status`` filter, so a row with ``"staged"``
    is one that ``status="staged"`` returns. Two more words: a connector
    with no operations yet is ``"registered"``, and one whose groups are
    partly enabled and partly disabled (none staged) is ``"mixed"``. No
    filter except ``all`` returns a mixed row. A connector whose
    connector package is not installed is ``"package_not_installed"``
    (#5560), and one whose code is not in this MEHO is
    ``"code_not_installed"`` (#5585).
    """
    if item.state in ("registered", "package_not_installed", "code_not_installed"):
        return item.state
    if item.staged_group_count > 0:
        return "staged"
    if item.group_count > 0 and item.enabled_group_count == item.group_count:
        return "enabled"
    if item.group_count > 0 and item.disabled_group_count == item.group_count:
        return "disabled"
    return "mixed"


def _connector_row(item: ConnectorListItem, detail: str) -> dict[str, Any]:
    """Project one row: six short fields, or every field for ``detail="full"``.

    The short row keeps ``scope`` because one connector id can appear
    twice, once built-in and once for the tenant.
    """
    if detail == "full":
        return item.model_dump(mode="json")
    return {
        "connector_id": item.connector_id,
        "product": item.product,
        "version": item.version,
        "status": connector_review_status(item),
        "dispatchable": item.dispatchable,
        "scope": item.scope,
    }


async def list_connector_rows(
    operator: Operator,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    """Return ``{"connectors": [...]}`` for the operator, not yet reduced.

    Delegates to :func:`list_ingested_connectors` — the same query
    helper the REST route at ``GET /api/v1/connectors`` uses. ``status``
    has already been checked against the tool's JSON Schema. ``product``
    keeps only rows with exactly that product; empty text means no
    filter. ``detail`` picks short rows (the default) or full rows.

    Split from :func:`reduce_connector_list` so the hosted agent can
    drop the rows outside its connector allow-list *before* the list is
    stored behind a handle.
    """
    status: ConnectorStatusFilter = arguments.get("status", "all")
    product = (arguments.get("product") or "").strip()
    detail = arguments.get("detail", "short")
    connectors = await list_ingested_connectors(operator=operator, status=status)
    return {
        "connectors": [
            _connector_row(item, detail)
            for item in connectors
            if not product or item.product == product
        ],
    }


async def reduce_connector_list(operator: Operator, result: dict[str, Any]) -> dict[str, Any]:
    """Return *result* unchanged, or a JSONFlux summary + ``handle`` above the threshold.

    Same pattern as ``list_targets``: the spill is keyed to the caller's
    tenant and subject, which is the key ``result_query`` reads back with.
    """
    summary, handle = await _CONNECTOR_LIST_REDUCER.reduce(
        result,
        None,
        {
            "op_id": CONNECTOR_LIST_NAME,
            "tenant_id": str(operator.tenant_id),
            "operator_sub": operator.sub,
            "pagination_hint": _CONNECTOR_LIST_PAGINATION_HINT,
        },
    )
    if handle is None:
        return result
    return {**summary, "handle": handle.model_dump(mode="json")}


async def _list_handler(
    operator: Operator,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    """List the connectors; a long list comes back as a handle (#4121)."""
    return await reduce_connector_list(operator, await list_connector_rows(operator, arguments))


async def _review_handler(
    operator: Operator,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    """Return the full review payload for one connector."""
    connector_id: str = arguments["connector_id"]
    tenant_id = _coerce_tenant_id(arguments.get("tenant_id"))
    prefer = _coerce_prefer(arguments.get("prefer"))
    service = ReviewService(operator)
    try:
        payload = await service.get_review_payload(connector_id, tenant_id, prefer=prefer)
    except AmbiguousConnectorScopeError as exc:
        # A label that resolves to both a tenant-curated and a built-in
        # row maps to a structured -32602 carrying the candidate scopes
        # (MCP↔REST parity, #1801); without this it would fall through to
        # the dispatcher's bare -32603. See
        # raise_invalid_params_for_ambiguous_scope.
        raise_invalid_params_for_ambiguous_scope(exc)
    except LegacyTenantConnectorCopyError as exc:
        # #4889: an old tenant copy under a former product name maps to a
        # structured -32602 (REST: 409) that names the copy and the way out.
        raise_invalid_params_for_legacy_tenant_copy(exc)
    except ConnectorNotFoundError as exc:
        # Unknown / cross-tenant label → -32602 connector_not_found
        # (#2481), not the dispatcher's bare -32603 class-name leak.
        raise_invalid_params_for_connector_not_found(exc)
    return _model_dump_json_safe(payload)


async def _edit_group_handler(
    operator: Operator,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    """Update one group's ``when_to_use`` and/or display ``name``.

    PATCH semantics: only fields the operator explicitly named are
    forwarded to :meth:`ReviewService.edit_group`. ``arguments.get(...)``
    would conflate "field omitted" with "field=null" — the service
    layer's empty-body rejection then can't distinguish "operator
    cleared the field" from "operator left the field alone".
    Key-presence checks preserve the intent. A no-editable-field call
    raises a :class:`ValueError` the arm below maps to ``-32602`` —
    REST-400 parity, never the dispatcher's bare ``-32603``.
    """
    connector_id: str = arguments["connector_id"]
    group_key: str = arguments["group_key"]
    tenant_id = _coerce_tenant_id(arguments.get("tenant_id"))
    patch: dict[str, Any] = {}
    if "when_to_use" in arguments:
        patch["when_to_use"] = arguments["when_to_use"]
    if "name" in arguments:
        patch["name"] = arguments["name"]
    service = ReviewService(operator)
    try:
        await service.edit_group(
            connector_id,
            group_key,
            tenant_id=tenant_id,
            **patch,
        )
    except BuiltinConnectorWriteForbiddenError as exc:
        # Built-in row + tenant_admin without platform_admin → -32602
        # builtin_connector_write_forbidden (#3616 parity), not the
        # dispatcher's bare -32603 class-name leak.
        raise_invalid_params_for_builtin_write_forbidden(exc)
    except LegacyTenantConnectorCopyError as exc:
        # #4889: an old tenant copy under a former product name maps to a
        # structured -32602 (REST: 409) that names the copy and the way out.
        raise_invalid_params_for_legacy_tenant_copy(exc)
    except ConnectorNotFoundError as exc:
        # Unknown / cross-tenant label → -32602 connector_not_found
        # (#2481), not the dispatcher's bare -32603 class-name leak.
        raise_invalid_params_for_connector_not_found(exc)
    except ValueError as exc:
        # No editable field supplied → -32602 with the service message
        # (#2488), REST-400 parity, not the dispatcher's -32603 leak.
        raise_invalid_params_for_edit_value_error(exc)
    return {"connector_id": connector_id, "group_key": group_key, "ok": True}


async def _edit_op_handler(
    operator: Operator,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    """Update one per-op override (any of five fields).

    Same PATCH-semantic discipline as :func:`_edit_group_handler`:
    fields not present in ``arguments`` are never forwarded. The
    key-presence check is load-bearing for ``custom_description``: an
    explicit ``custom_description: null`` is forwarded as ``None`` and
    clears the column to SQL NULL (the ``[string, null]`` inputSchema
    promise), while an omitted key never reaches the service — the
    service's :data:`_UNSET` default leaves the column untouched
    (:meth:`ReviewService.edit_op`). A no-editable-field call, or a bad
    ``safety_level`` enum, raises a :class:`ValueError` that the arm below
    maps to ``-32602`` — REST-400 parity, never the dispatcher's bare
    ``-32603 "internal error: ValueError"``.

    ``warnings`` mirrors the REST route's
    :class:`~meho_backplane.operations.ingest.EditOpResponse` field
    (G0.23-T4 #1630): enable-time advisories from the shared service
    layer — today only the ``unreplaced_auto_shim`` probe —
    serialized per-entry via ``model_dump(mode="json")``. Empty list
    on the clean path; never blocks the write (``ok`` stays
    ``True``). Surfacing it here keeps MCP↔REST parity, the same
    discipline the ingest error envelopes follow (#1534 / #1610).
    """
    connector_id: str = arguments["connector_id"]
    op_id: str = arguments["op_id"]
    tenant_id = _coerce_tenant_id(arguments.get("tenant_id"))
    patch: dict[str, Any] = {}
    if "custom_description" in arguments:
        patch["custom_description"] = arguments["custom_description"]
    if "safety_level" in arguments:
        patch["safety_level"] = arguments["safety_level"]
    if "requires_approval" in arguments:
        patch["requires_approval"] = arguments["requires_approval"]
    if "is_enabled" in arguments:
        patch["is_enabled"] = arguments["is_enabled"]
    if "reason" in arguments:
        patch["reason"] = arguments["reason"]
    service = ReviewService(operator)
    try:
        # #4711: an AI client may make an operation stricter, never looser.
        # ``tighten_only`` refuses ``requires_approval: false`` and any lower
        # risk level with a message that names the console / CLI path.
        warnings = await service.edit_op(
            connector_id,
            op_id,
            tenant_id=tenant_id,
            tighten_only=True,
            **patch,
        )
    except BuiltinConnectorWriteForbiddenError as exc:
        # Built-in row + tenant_admin without platform_admin → -32602
        # builtin_connector_write_forbidden (#3616 parity), not the
        # dispatcher's bare -32603 class-name leak.
        raise_invalid_params_for_builtin_write_forbidden(exc)
    except LegacyTenantConnectorCopyError as exc:
        # #4889: an old tenant copy under a former product name maps to a
        # structured -32602 (REST: 409) that names the copy and the way out.
        raise_invalid_params_for_legacy_tenant_copy(exc)
    except ConnectorNotFoundError as exc:
        # Unknown / cross-tenant label → -32602 connector_not_found
        # (#2481), not the dispatcher's bare -32603 class-name leak.
        raise_invalid_params_for_connector_not_found(exc)
    except ValueError as exc:
        # No editable field / bad safety_level enum → -32602 with the
        # service message (#2488), REST-400 parity, not a -32603 leak.
        raise_invalid_params_for_edit_value_error(exc)
    return {
        "connector_id": connector_id,
        "op_id": op_id,
        "ok": True,
        "warnings": [warning.model_dump(mode="json") for warning in warnings],
    }


async def _enable_handler(
    operator: Operator,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    """Flip every group of a connector to ``review_status='enabled'``."""
    connector_id: str = arguments["connector_id"]
    tenant_id = _coerce_tenant_id(arguments.get("tenant_id"))
    service = ReviewService(operator)
    try:
        await service.enable_connector(connector_id, tenant_id=tenant_id)
    except BuiltinConnectorWriteForbiddenError as exc:
        # Built-in row + tenant_admin without platform_admin → -32602
        # builtin_connector_write_forbidden (#3616 parity), not the
        # dispatcher's bare -32603 class-name leak.
        raise_invalid_params_for_builtin_write_forbidden(exc)
    except LegacyTenantConnectorCopyError as exc:
        # #4889: an old tenant copy under a former product name maps to a
        # structured -32602 (REST: 409) that names the copy and the way out.
        raise_invalid_params_for_legacy_tenant_copy(exc)
    except ConnectorNotFoundError as exc:
        # Unknown / cross-tenant label → -32602 connector_not_found
        # (#2481), not the dispatcher's bare -32603 class-name leak.
        raise_invalid_params_for_connector_not_found(exc)
    return {"connector_id": connector_id, "status": "enabled", "ok": True}


async def _enable_reads_handler(
    operator: Operator,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    """Bulk-enable every read-class (GET/HEAD) ingested op (G0.25-T7 #1749).

    Delegates to :meth:`ReviewService.enable_reads` — the same service
    path ``POST /api/v1/connectors/{connector_id}/enable-reads`` drives.
    ``ops_enabled`` echoes the count of ops flipped (``0`` on the
    idempotent re-run). The ``tenant_id`` argument follows the #1699
    contract this module's mutators share: omitted / null → the
    built-in / global scope; an explicit UUID → that tenant's curated
    rows. Mutating a built-in row (whether addressed by null or reached
    via the tenant-preferring fall-back) additionally requires
    platform_admin — a tenant_admin without it is refused with a
    structured -32602 builtin_connector_write_forbidden (#3616 parity).
    """
    connector_id: str = arguments["connector_id"]
    tenant_id = _coerce_tenant_id(arguments.get("tenant_id"))
    prefer = _coerce_prefer(arguments.get("prefer"))
    service = ReviewService(operator)
    try:
        ops_enabled = await service.enable_reads(
            connector_id,
            tenant_id=tenant_id,
            prefer=prefer,
        )
    except BuiltinConnectorWriteForbiddenError as exc:
        # Built-in row + tenant_admin without platform_admin → -32602
        # builtin_connector_write_forbidden (#3616 parity), not the
        # dispatcher's bare -32603 class-name leak.
        raise_invalid_params_for_builtin_write_forbidden(exc)
    except AmbiguousConnectorScopeError as exc:
        # Same ambiguous-scope mapping as _review_handler: structured
        # -32602 with the candidate scopes instead of a bare -32603
        # (#1801 / this is #1910). enable_reads shares the resolver with
        # the review read path, so it can raise the same error.
        raise_invalid_params_for_ambiguous_scope(exc)
    except LegacyTenantConnectorCopyError as exc:
        # #4889: an old tenant copy under a former product name maps to a
        # structured -32602 (REST: 409) that names the copy and the way out.
        raise_invalid_params_for_legacy_tenant_copy(exc)
    except ConnectorNotFoundError as exc:
        # Unknown / cross-tenant label → -32602 connector_not_found
        # (#2481), not the dispatcher's bare -32603 class-name leak.
        raise_invalid_params_for_connector_not_found(exc)
    return {
        "connector_id": connector_id,
        "ops_enabled": ops_enabled,
        "ok": True,
    }


async def _disable_handler(
    operator: Operator,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    """Flip every group of a connector to ``review_status='disabled'``."""
    connector_id: str = arguments["connector_id"]
    tenant_id = _coerce_tenant_id(arguments.get("tenant_id"))
    service = ReviewService(operator)
    try:
        await service.disable_connector(connector_id, tenant_id=tenant_id)
    except BuiltinConnectorWriteForbiddenError as exc:
        # Built-in row + tenant_admin without platform_admin → -32602
        # builtin_connector_write_forbidden (#3616 parity), not the
        # dispatcher's bare -32603 class-name leak.
        raise_invalid_params_for_builtin_write_forbidden(exc)
    except LegacyTenantConnectorCopyError as exc:
        # #4889: an old tenant copy under a former product name maps to a
        # structured -32602 (REST: 409) that names the copy and the way out.
        raise_invalid_params_for_legacy_tenant_copy(exc)
    except ConnectorNotFoundError as exc:
        # Unknown / cross-tenant label → -32602 connector_not_found
        # (#2481), not the dispatcher's bare -32603 class-name leak.
        raise_invalid_params_for_connector_not_found(exc)
    return {"connector_id": connector_id, "status": "disabled", "ok": True}


async def _delete_handler(
    operator: Operator,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    """Delete one connector's rows + its auto-registered ingest shim.

    Delegates to :meth:`ReviewService.delete_connector` — the same
    service path ``DELETE /api/v1/connectors/{connector_id}`` drives.
    ``warnings`` mirrors the ``edit_op`` advisory discipline
    (G0.23-T4 #1630): a connector deleted while it still had enabled
    operations completes the delete and reports the advisory here
    (this tool is the structured wire home; the REST sibling stays
    ``204 No Content`` and logs/audits instead). The ``deleted``
    block carries the row counts plus the ``registry_only`` /
    ``class_deregistered`` flags so an operator can tell a zero-op
    stub clean-up apart from a full row delete.
    """
    connector_id: str = arguments["connector_id"]
    tenant_id = _coerce_tenant_id(arguments.get("tenant_id"))
    service = ReviewService(operator)
    try:
        result = await service.delete_connector(connector_id, tenant_id=tenant_id)
    except ConnectorInUseError as exc:
        # #5027: something still uses the connector; nothing was deleted.
        raise_invalid_params_for_connector_in_use(exc)
    except ConnectorNotFoundError as exc:
        # Unknown / cross-tenant label → -32602 connector_not_found
        # (#2481), not the dispatcher's bare -32603 class-name leak.
        raise_invalid_params_for_connector_not_found(exc)
    return {
        "connector_id": connector_id,
        "ok": True,
        "deleted": {
            "groups_deleted": result.groups_deleted,
            "operations_deleted": result.operations_deleted,
            "enabled_operations_deleted": result.enabled_operations_deleted,
            "class_deregistered": result.class_deregistered,
            "registry_only": result.registry_only,
        },
        "warnings": [
            {
                "code": warning.code,
                "enabled_op_count": warning.enabled_op_count,
                "message": warning.message,
            }
            for warning in result.warnings
        ],
    }


# ---------------------------------------------------------------------------
# Tool registrations
# ---------------------------------------------------------------------------


register_mcp_tool(
    definition=ToolDefinition(
        name=CONNECTOR_LIST_NAME,
        annotations=READ_ONLY,
        surface=ToolSurface.WORKING,
        feature="typed_connector_reads",
        description=(
            "List ingested connectors visible to the operator's tenant "
            "(plus built-in / global). By default each row is short: "
            "connector_id, product, version, status, dispatchable, scope. "
            "Pass `product` to list one product only. A long list comes "
            "back as a short preview plus a `handle`; read its rows with "
            "`result_query`. With detail='full' each row also has counts — "
            "group counts split by review status, plus the operation "
            "rollup split enabled-vs-total (enabled_operation_count = "
            "ops whose per-op is_enabled flag is set, i.e. actually "
            "dispatchable; operation_count = every ingested/typed/"
            "composite op) — and the aggregate review status. The "
            "review status says if a person has checked the connector's "
            "operations yet: 'staged' = at least one group still waits "
            "for review; 'enabled' = reviewed and ready to use; "
            "'disabled' = every group is turned off. With no `status`, "
            "it lists every connector. Do not pass `status` unless the "
            "user asks for connectors in one review state. Read-only; "
            "visible to operator and tenant_admin roles."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "status": {
                    "type": "string",
                    "enum": ["staged", "enabled", "disabled", "all"],
                    "default": "all",
                    "description": (
                        "Aggregate-status filter. 'staged' = at least "
                        "one group still staged; 'enabled' = at least "
                        "one group enabled and none staged; "
                        "'disabled' = every group disabled; 'all' = "
                        "no filter."
                    ),
                },
                "product": {
                    "type": "string",
                    "description": (
                        "Keep only connectors of this product. Use the "
                        "exact value of a row's `product` field. Leave "
                        "it out to list every product."
                    ),
                },
                "detail": {
                    "type": "string",
                    "enum": ["short", "full"],
                    "default": "short",
                    "description": (
                        "'short' = six fields per row (connector_id, "
                        "product, version, status, dispatchable, scope). "
                        "'full' = every field, with group and operation "
                        "counts."
                    ),
                },
            },
            "required": [],
            "additionalProperties": False,
        },
        required_role=TenantRole.OPERATOR,
        op_class=_OP_CLASS_READ,
    ),
    handler=_list_handler,
)


register_mcp_tool(
    definition=ToolDefinition(
        name="meho_connector_review",
        surface=ToolSurface.OPERATOR,
        feature="connector_ingest",
        description=(
            "Get the full review payload for one connector (groups + "
            "per-group operations + per-op flags). Use after "
            "meho_connector_list has surfaced a connector that needs "
            "review, or whenever you need to inspect an already-enabled "
            "connector's surface. The payload includes "
            "operator-editable fields (when_to_use, custom_description, "
            "safety_level, requires_approval, is_enabled) — pair with "
            "meho_connector_edit_group / meho_connector_edit_op to "
            "modify them. Read-only; visible to operator and "
            "tenant_admin roles. Tenant-scoped: cross-tenant reads are "
            "rejected with a ConnectorNotFoundError. When a connector_id "
            "maps to BOTH a tenant-curated row and a built-in row the "
            "call fails with a structured ambiguous-scope error listing "
            "the candidates; pass prefer='tenant' or prefer='builtin' to "
            "target one directly. prefer='legacy' shows the tenant's old "
            "copy stored under a former product name, the copy "
            "meho_connector_delete removes first."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "connector_id": {
                    "type": "string",
                    "minLength": 1,
                    "description": _CONNECTOR_ID_DESCRIPTION,
                },
                "tenant_id": _TENANT_ID_PROPERTY,
                "prefer": _PREFER_PROPERTY,
            },
            "required": ["connector_id"],
            "additionalProperties": False,
        },
        required_role=TenantRole.OPERATOR,
        op_class=_OP_CLASS_READ,
    ),
    handler=_review_handler,
)


register_mcp_tool(
    definition=ToolDefinition(
        name="meho_connector_edit_group",
        surface=ToolSurface.OPERATOR,
        feature="connector_ingest",
        description=(
            "Edit one operation group's when_to_use prose or display "
            "name (tenant_admin only). Use after meho_connector_review "
            "has surfaced a group whose LLM-generated description needs "
            "operator-authored prose. Pass at least one of when_to_use "
            "or name — passing neither is rejected. Each edit writes one "
            "audit row. Do NOT use this to change group membership "
            "(operation-to-group assignment): that requires re-running "
            "ingest or editing per-op assignments via "
            "meho_connector_edit_op."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "connector_id": {
                    "type": "string",
                    "minLength": 1,
                    "description": _CONNECTOR_ID_DESCRIPTION,
                },
                "group_key": {"type": "string", "minLength": 1},
                "when_to_use": {"type": ["string", "null"]},
                "name": {"type": ["string", "null"]},
                "tenant_id": _TENANT_ID_PROPERTY,
            },
            "required": ["connector_id", "group_key"],
            "additionalProperties": False,
        },
        required_role=TenantRole.TENANT_ADMIN,
        op_class=_OP_CLASS_WRITE,
    ),
    handler=_edit_group_handler,
)


register_mcp_tool(
    definition=ToolDefinition(
        name="meho_connector_edit_op",
        surface=ToolSurface.OPERATOR,
        feature="connector_ingest",
        description=(
            "Edit one operation's per-op overrides (tenant_admin only). "
            "Editable fields: custom_description (operator-authored "
            "agent-facing description), safety_level (safe / caution / "
            "dangerous / destructive), requires_approval (gate the "
            "operation behind out-of-band approval), is_enabled (per-op "
            "enable/disable that survives connector-level enable/disable "
            "cycles), reason (written to the audit row). Pass at least one "
            "field; passing none is rejected. This tool can only make an "
            "operation stricter: it refuses requires_approval=false and "
            "any risk level lower than the current one. Turning approval "
            "off is for a platform admin in the console or the CLI; "
            "lowering a risk level needs a pull request. A raised risk "
            "level or required approval survives restarts and re-ingest. "
            "Use during review (after meho_connector_review surfaces the "
            "op) when an LLM-generated description is misleading or when "
            "a DELETE-shaped op needs a higher safety_level or approval. "
            "is_enabled=false here "
            "STICKS: a subsequent meho_connector_enable will NOT "
            "clobber the operator's per-op disable. Do not use this "
            "to flip whole-connector state — pair with "
            "meho_connector_enable / meho_connector_disable for that."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "connector_id": {
                    "type": "string",
                    "minLength": 1,
                    "description": _CONNECTOR_ID_DESCRIPTION,
                },
                "op_id": {
                    "type": "string",
                    "minLength": 1,
                    "description": (
                        "Connector-side op_id (e.g. 'GET:/repos/{owner}/{repo}/issues')."
                    ),
                },
                "custom_description": {"type": ["string", "null"]},
                "safety_level": {
                    "type": ["string", "null"],
                    "enum": ["safe", "caution", "dangerous", "destructive", None],
                },
                "requires_approval": {"type": ["boolean", "null"]},
                "is_enabled": {"type": ["boolean", "null"]},
                "reason": {"type": ["string", "null"], "minLength": 1, "maxLength": 1024},
                "tenant_id": _TENANT_ID_PROPERTY,
            },
            "required": ["connector_id", "op_id"],
            "additionalProperties": False,
        },
        required_role=TenantRole.TENANT_ADMIN,
        op_class=_OP_CLASS_WRITE,
    ),
    handler=_edit_op_handler,
)


register_mcp_tool(
    definition=ToolDefinition(
        name="meho_connector_enable",
        surface=ToolSurface.OPERATOR,
        feature="typed_connector_reads",
        description=(
            "Flip every group of a connector to review_status='enabled' "
            "(tenant_admin only). Cascades is_enabled=true to every "
            "child op except those an operator previously set "
            "is_enabled=false on via meho_connector_edit_op (operator "
            "overrides stick). Use AFTER reviewing the connector with "
            "meho_connector_review + applying any needed edits. "
            "Idempotent — already-enabled groups are a no-op (no audit "
            "row written). DO NOT use this on a connector that's still "
            "actively being reviewed; the agent surface starts "
            "dispatching its operations the moment this returns."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "connector_id": {
                    "type": "string",
                    "minLength": 1,
                    "description": _CONNECTOR_ID_DESCRIPTION,
                },
                "tenant_id": _TENANT_ID_PROPERTY,
            },
            "required": ["connector_id"],
            "additionalProperties": False,
        },
        required_role=TenantRole.TENANT_ADMIN,
        op_class=_OP_CLASS_WRITE,
    ),
    handler=_enable_handler,
)


register_mcp_tool(
    definition=ToolDefinition(
        name="meho_connector_enable_reads",
        surface=ToolSurface.OPERATOR,
        feature="typed_connector_reads",
        description=(
            "Bulk-enable every read-class operation of a connector in "
            "one pass (tenant_admin only). Flips is_enabled=true on "
            "every ingested op whose HTTP method is GET or HEAD, "
            "leaving every write-shaped verb (POST / PUT / PATCH / "
            "DELETE) and every typed/composite op default-deny. Use to "
            "stand up broad governed READ coverage on a large ingested "
            "surface (e.g. a 3000-endpoint vCenter spec) without "
            "editing each op individually — writes stay behind per-op "
            "review / composite curation by design (the governance "
            "boundary). Returns ops_enabled, the count flipped. "
            "Idempotent: re-running once the reads are enabled flips "
            "nothing and writes no audit row (ops_enabled=0). Pair with "
            "meho_connector_review to inspect the surface first. Reach "
            "for meho_connector_enable instead when you want the full "
            "per-group state transition — this verb does NOT move any "
            "group's review_status, so a connector can stay staged at "
            "the group level while its reads become dispatchable. "
            "Writes one meho_connector_enable_reads audit row when at "
            "least one op flips. Omit tenant_id (or pass null) for "
            "built-in / global connectors; pass the operator's own "
            "tenant UUID for tenant-curated rows. When a connector_id "
            "maps to BOTH a tenant-curated row and a built-in row the "
            "call fails with a structured ambiguous-scope error listing "
            "the candidates; pass prefer='tenant' or prefer='builtin' to "
            "apply to one directly (the built-in scope stays "
            "tenant_admin-gated)."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "connector_id": {
                    "type": "string",
                    "minLength": 1,
                    "description": _CONNECTOR_ID_DESCRIPTION,
                },
                "tenant_id": _TENANT_ID_PROPERTY,
                "prefer": _PREFER_PROPERTY,
            },
            "required": ["connector_id"],
            "additionalProperties": False,
        },
        required_role=TenantRole.TENANT_ADMIN,
        op_class=_OP_CLASS_WRITE,
    ),
    handler=_enable_reads_handler,
)


register_mcp_tool(
    definition=ToolDefinition(
        name="meho_connector_disable",
        surface=ToolSurface.OPERATOR,
        feature="typed_connector_reads",
        description=(
            "Flip every group of a connector to "
            "review_status='disabled' (tenant_admin only). Cascades "
            "is_enabled=false to every child op (the connector-level "
            "disable overrides per-op operator overrides for the "
            "duration of the disabled state). Use to roll back a "
            "regression — the connector's operations stop showing up "
            "in search_operations the moment this returns. Idempotent. "
            "Do NOT use as a soft toggle during normal review; pair "
            "with meho_connector_enable to bring the connector back "
            "after the regression is fixed. Operator-set per-op "
            "disables are recovered from the audit log when the re-"
            "enable cascade fires."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "connector_id": {
                    "type": "string",
                    "minLength": 1,
                    "description": _CONNECTOR_ID_DESCRIPTION,
                },
                "tenant_id": _TENANT_ID_PROPERTY,
            },
            "required": ["connector_id"],
            "additionalProperties": False,
        },
        required_role=TenantRole.TENANT_ADMIN,
        op_class=_OP_CLASS_WRITE,
    ),
    handler=_disable_handler,
)


register_mcp_tool(
    definition=ToolDefinition(
        name="meho_connector_delete",
        surface=ToolSurface.OPERATOR,
        feature="typed_connector_reads",
        description=(
            "Delete one connector (tenant_admin only): remove its "
            "operation_group + endpoint_descriptor rows under the "
            "target scope and, when no rows remain for the triple "
            "anywhere, deregister the auto-registered ingest shim "
            "from the v2 registry. Use to clean up the zero-op stubs "
            "an aborted ingest leaves behind (spec parsed to zero "
            "operations, batch failed mid-way) or to remove an "
            "unwanted ingested connector outright. The tenant_id "
            "parameter controls the scope: omit it or pass null for "
            "built-in / global connectors; pass the operator's own "
            "tenant UUID for tenant-curated rows. The REST sibling "
            "DELETE /api/v1/connectors/{connector_id} does NOT expose "
            "this parameter and always scopes to the calling "
            "operator's tenant — for global rows use this tool. "
            "Deleting a connector that still has enabled operations "
            "completes and returns an enabled_operations_deleted "
            "warning (advisory, never blocks). A connector that a "
            "sensor, runbook template, hosted agent, schedule, waiting "
            "approval or queued operation run still uses is NOT "
            "deleted: the call fails with connector_in_use and lists "
            "each user, so remove or change them first. One "
            "meho_connector_delete audit row is written. Do NOT use "
            "this for a temporary roll-back — pair with "
            "meho_connector_disable for reversible state instead; "
            "delete is permanent (no undo), though re-running ingest "
            "for the same product/version/impl re-registers the "
            "connector from scratch. Hand-coded connector classes "
            "(Vault, K8s, bind9, …) are never deregistered — for "
            "those only the rows under the named scope are removed. "
            "Unknown or cross-tenant connector ids fail with the "
            "same not-found error every other connector tool uses."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "connector_id": {
                    "type": "string",
                    "minLength": 1,
                    "description": _CONNECTOR_ID_DESCRIPTION,
                },
                "tenant_id": _TENANT_ID_PROPERTY,
            },
            "required": ["connector_id"],
            "additionalProperties": False,
        },
        required_role=TenantRole.TENANT_ADMIN,
        op_class=_OP_CLASS_WRITE,
    ),
    handler=_delete_handler,
)
