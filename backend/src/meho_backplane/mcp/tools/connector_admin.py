# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Admin MCP tools for the connector ingest + review pipeline (G0.7-T7).

Seven tools under the ``meho.connector.*`` namespace, deliberately
separate from the agent-surface meta-tools
(:mod:`~meho_backplane.mcp.tools.operations`):

* ``meho.connector.ingest`` — run the ingest pipeline. **tenant_admin**.
* ``meho.connector.list`` — list ingested connectors. **operator**.
* ``meho.connector.review`` — get the review payload. **operator**.
* ``meho.connector.edit_group`` — edit one group. **tenant_admin**.
* ``meho.connector.edit_op`` — edit one op override. **tenant_admin**.
* ``meho.connector.enable`` — flip the connector to enabled. **tenant_admin**.
* ``meho.connector.disable`` — flip the connector to disabled. **tenant_admin**.

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

1. ``meho.connector.ingest`` constructs a per-call
   :class:`IngestionPipelineService` bound to the calling
   :class:`Operator` and calls :meth:`IngestionPipelineService.ingest`.
2. ``meho.connector.list`` calls the
   :func:`list_ingested_connectors` query helper directly.
3. Every other tool constructs :class:`ReviewService` and
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
``connector_id`` field appears on six of the seven tools (everything
but ``ingest``) and is documented identically across them: an operator-
facing ``<impl_id>-<version>`` string.

The ``tenant_id`` field is optional everywhere. ``null`` / omitted →
the built-in scope, which is accessible only to ``tenant_admin`` per
the service layer's authorisation rule. Tenant operators pass their
own tenant's UUID when they want to operate on their own
tenant-curated connectors.
"""

from __future__ import annotations

import json
from typing import Any, Final
from uuid import UUID

import structlog

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.mcp.registry import ToolDefinition, register_mcp_tool
from meho_backplane.operations.ingest import (
    ConnectorStatusFilter,
    IngestionPipelineService,
    IngestRequest,
    ReviewService,
    SpecSource,
    default_llm_client_factory,
    list_ingested_connectors,
)

__all__: list[str] = []

_log = structlog.get_logger(__name__)


# Op-class strings keep parity with the audit table conventions used
# by :mod:`meho_backplane.broadcast.classify` for the credential /
# audit / read / write taxonomy. List + review are read; the five
# mutators are write.
_OP_CLASS_READ: Final[str] = "read"
_OP_CLASS_WRITE: Final[str] = "write"


# Shared snippet documenting the connector_id format on every tool
# that accepts one. Authored once so a future convention tweak edits
# one string instead of six.
_CONNECTOR_ID_DESCRIPTION: Final[str] = (
    "Operator-facing connector identifier — '<impl_id>-<version>' "
    "(e.g. 'vmware-rest-9.0', 'vault-1.x'). Split into the parsed "
    "(product, version, impl_id) triple by the same rule the CLI "
    "uses."
)

#: Optional tenant scope shared across every tool. Omit (or pass
#: ``null``) for the built-in / global connector pool — that scope is
#: ``tenant_admin``-only. Pass the operator's own tenant UUID to
#: operate on tenant-curated rows.
_TENANT_ID_PROPERTY: Final[dict[str, Any]] = {
    "type": ["string", "null"],
    "format": "uuid",
    "description": (
        "Tenant scope for this operation. Omit or pass null for "
        "built-in / global connectors (tenant_admin only). Pass the "
        "operator's own tenant UUID for tenant-curated rows; cross-"
        "tenant requests are rejected."
    ),
}


# ---------------------------------------------------------------------------
# Argument coercion helpers
# ---------------------------------------------------------------------------


def _coerce_tenant_id(raw: Any) -> UUID | None:
    """Convert the ``tenant_id`` argument from JSON-RPC into ``UUID | None``.

    The JSON-Schema-validated value is always either a UUID string or
    ``None``; the helper preserves ``None`` and parses the string. A
    malformed UUID surfaces at JSON-Schema validation time (because of
    the ``format: uuid`` annotation interpreted by
    :mod:`jsonschema`'s format-checker chain). This helper assumes the
    schema validator has already run.
    """
    if raw is None:
        return None
    return UUID(raw)


# ---------------------------------------------------------------------------
# Handler implementations
# ---------------------------------------------------------------------------


async def _ingest_handler(
    operator: Operator,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    """Run the full T1 + T2 + T3 ingest pipeline.

    Translates the MCP arguments into the canonical
    :class:`IngestRequest` (from :mod:`api_schemas`) and delegates to
    :meth:`IngestionPipelineService.ingest`. The handler installs
    :func:`default_llm_client_factory` so a misconfigured chassis
    surfaces :class:`LlmClientUnavailable` rather than crashing
    mid-grouping — the production Anthropic adapter wires its own
    factory at the FastAPI lifespan layer once T5 (#405) lands.

    The response is the canonical :class:`IngestResponse` shape the
    REST route at ``POST /api/v1/connectors/ingest`` also returns.
    """
    request = IngestRequest(
        product=arguments["product"],
        version=arguments["version"],
        impl_id=arguments["impl_id"],
        specs=[SpecSource(uri=spec["uri"]) for spec in arguments["specs"]],
        base_url=arguments.get("base_url"),
        dry_run=bool(arguments.get("dry_run", False)),
    )
    tenant_id = _coerce_tenant_id(arguments.get("tenant_id"))
    service = IngestionPipelineService(
        operator,
        llm_client_factory=default_llm_client_factory,
    )
    result = await service.ingest(
        product=request.product,
        version=request.version,
        impl_id=request.impl_id,
        specs=request.specs,
        base_url=request.base_url,
        tenant_id=tenant_id,
        dry_run=request.dry_run,
    )
    ingestion_model, grouping_model = result.to_api_models()
    # Build the canonical IngestResponse manually rather than
    # constructing a new instance and round-tripping the inner
    # models — Pydantic's frozen=True forbids mutation on the way
    # back out anyway, and the explicit shape here is the same wire
    # contract the REST router emits.
    return {
        "ingestion": ingestion_model.model_dump(mode="json"),
        "grouping": (
            grouping_model.model_dump(mode="json") if grouping_model is not None else None
        ),
    }


async def _list_handler(
    operator: Operator,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    """Return connectors visible to the operator's tenant + built-in.

    Delegates to :func:`list_ingested_connectors` — the same query
    helper the REST route at ``GET /api/v1/connectors`` uses. The
    ``status`` argument is the canonical
    :data:`ConnectorStatusFilter` literal; the JSON-Schema validator
    has already restricted it to one of the four legal values before
    this handler runs.
    """
    raw_status = arguments.get("status", "all")
    # Normalise the JSON-Schema-validated string into the canonical
    # Literal type the query helper takes. ``"all"`` is the explicit
    # no-filter sentinel; the function itself also accepts ``None``.
    status: ConnectorStatusFilter = raw_status
    connectors = await list_ingested_connectors(operator=operator, status=status)
    return {
        "connectors": [item.model_dump(mode="json") for item in connectors],
    }


async def _review_handler(
    operator: Operator,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    """Return the full review payload for one connector."""
    connector_id: str = arguments["connector_id"]
    tenant_id = _coerce_tenant_id(arguments.get("tenant_id"))
    service = ReviewService(operator)
    payload = await service.get_review_payload(connector_id, tenant_id)
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
    Key-presence checks preserve the intent.
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
    await service.edit_group(
        connector_id,
        group_key,
        tenant_id=tenant_id,
        **patch,
    )
    return {"connector_id": connector_id, "group_key": group_key, "ok": True}


async def _edit_op_handler(
    operator: Operator,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    """Update one per-op override (any of four fields).

    Same PATCH-semantic discipline as :func:`_edit_group_handler`:
    fields not present in ``arguments`` are never forwarded.
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
    service = ReviewService(operator)
    await service.edit_op(
        connector_id,
        op_id,
        tenant_id=tenant_id,
        **patch,
    )
    return {"connector_id": connector_id, "op_id": op_id, "ok": True}


async def _enable_handler(
    operator: Operator,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    """Flip every group of a connector to ``review_status='enabled'``."""
    connector_id: str = arguments["connector_id"]
    tenant_id = _coerce_tenant_id(arguments.get("tenant_id"))
    service = ReviewService(operator)
    await service.enable_connector(connector_id, tenant_id=tenant_id)
    return {"connector_id": connector_id, "status": "enabled", "ok": True}


async def _disable_handler(
    operator: Operator,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    """Flip every group of a connector to ``review_status='disabled'``."""
    connector_id: str = arguments["connector_id"]
    tenant_id = _coerce_tenant_id(arguments.get("tenant_id"))
    service = ReviewService(operator)
    await service.disable_connector(connector_id, tenant_id=tenant_id)
    return {"connector_id": connector_id, "status": "disabled", "ok": True}


# ---------------------------------------------------------------------------
# Serialisation helper
# ---------------------------------------------------------------------------


def _model_dump_json_safe(model: Any) -> dict[str, Any]:
    """Serialise a Pydantic model to a JSON-safe dict.

    Uses ``mode="json"`` so UUIDs and datetimes serialise as strings
    rather than native Python objects (the dispatcher's
    :func:`json.dumps` over the response would otherwise raise
    :class:`TypeError`). Mirrors the meho_status reference tool's
    serialisation discipline.
    """
    raw = model.model_dump(mode="json")
    # Round-trip via ``json`` to surface any latent non-serialisable
    # field at registration time rather than at the dispatcher's
    # ``json.dumps`` call. The cost is one extra encode/decode per
    # tool call; the benefit is that a Pydantic field shape that
    # ``mode="json"`` can't handle (e.g. a future ``set[UUID]``) is
    # rejected here with the offending field named in the traceback
    # rather than as a generic dispatcher error.
    rehydrated: dict[str, Any] = json.loads(json.dumps(raw))
    return rehydrated


# ---------------------------------------------------------------------------
# Tool registrations
# ---------------------------------------------------------------------------


register_mcp_tool(
    definition=ToolDefinition(
        name="meho.connector.ingest",
        description=(
            "Ingest one or more OpenAPI specs into a MEHO connector "
            "(tenant_admin only). Parses each spec, registers the "
            "operations into the endpoint_descriptor table, and runs the "
            "LLM-summarised grouping pass. The connector lands in "
            "'staged' state — operators must review groups + per-op flags "
            "and then call meho.connector.enable before the connector's "
            "operations become dispatchable. "
            "Use when adding a new vendor surface (product=vmware "
            "version=9.0 impl_id=vmware-rest specs=[...]); supports "
            "merging multiple specs under one connector (vSphere ingests "
            "vcenter.yaml + vi-json.yaml). "
            "Do NOT use for typed connectors (Vault, K8s, bind9) — those "
            "register via register_typed_operation() at connector init "
            "and never need this tool. Pass dry_run=true to validate "
            "specs without writing to the DB."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "product": {"type": "string", "minLength": 1, "maxLength": 64},
                "version": {"type": "string", "minLength": 1, "maxLength": 64},
                "impl_id": {"type": "string", "minLength": 1, "maxLength": 128},
                "specs": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 16,
                    "items": {
                        "type": "object",
                        "properties": {
                            "uri": {"type": "string", "minLength": 1, "maxLength": 2048},
                        },
                        "required": ["uri"],
                        "additionalProperties": False,
                    },
                },
                "base_url": {"type": ["string", "null"], "maxLength": 2048},
                "dry_run": {"type": "boolean", "default": False},
                "tenant_id": _TENANT_ID_PROPERTY,
            },
            "required": ["product", "version", "impl_id", "specs"],
            "additionalProperties": False,
        },
        required_role=TenantRole.TENANT_ADMIN,
        op_class=_OP_CLASS_WRITE,
    ),
    handler=_ingest_handler,
)


register_mcp_tool(
    definition=ToolDefinition(
        name="meho.connector.list",
        description=(
            "List ingested connectors visible to the operator's tenant "
            "(plus built-in / global). Returns per-connector counts and "
            "the aggregate review status (staged / enabled / disabled). "
            "Use as the entry point when an operator needs to find a "
            "connector to review or to see what's already in place. "
            "Filter via status=staged to surface only connectors that "
            "need review. Pair with meho.connector.review to drill into "
            "an individual connector. Read-only; visible to operator and "
            "tenant_admin roles."
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
        name="meho.connector.review",
        description=(
            "Get the full review payload for one connector (groups + "
            "per-group operations + per-op flags). Use after "
            "meho.connector.list has surfaced a connector that needs "
            "review, or whenever you need to inspect an already-enabled "
            "connector's surface. The payload includes "
            "operator-editable fields (when_to_use, custom_description, "
            "safety_level, requires_approval, is_enabled) — pair with "
            "meho.connector.edit_group / meho.connector.edit_op to "
            "modify them. Read-only; visible to operator and "
            "tenant_admin roles. Tenant-scoped: cross-tenant reads are "
            "rejected with a ConnectorNotFoundError."
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
        required_role=TenantRole.OPERATOR,
        op_class=_OP_CLASS_READ,
    ),
    handler=_review_handler,
)


register_mcp_tool(
    definition=ToolDefinition(
        name="meho.connector.edit_group",
        description=(
            "Edit one operation group's when_to_use prose or display "
            "name (tenant_admin only). Use after meho.connector.review "
            "has surfaced a group whose LLM-generated description needs "
            "operator-authored prose. Pass at least one of when_to_use "
            "or name — passing neither is rejected. Each edit writes one "
            "audit row. Do NOT use this to change group membership "
            "(operation-to-group assignment): that requires re-running "
            "ingest or editing per-op assignments via "
            "meho.connector.edit_op."
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
        name="meho.connector.edit_op",
        description=(
            "Edit one operation's per-op overrides (tenant_admin only). "
            "Editable fields: custom_description (operator-authored "
            "agent-facing description), safety_level (safe / caution / "
            "dangerous), requires_approval (gate the operation behind "
            "out-of-band approval), is_enabled (per-op enable/disable "
            "that survives connector-level enable/disable cycles). Pass "
            "at least one field; passing none is rejected. Use during "
            "review (after meho.connector.review surfaces the op) when "
            "an LLM-generated description is misleading or when a "
            "DELETE-shaped op needs the safety_level promoted from "
            "'dangerous' to require approval. is_enabled=false here "
            "STICKS: a subsequent meho.connector.enable will NOT "
            "clobber the operator's per-op disable. Do not use this "
            "to flip whole-connector state — pair with "
            "meho.connector.enable / .disable for that."
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
                    "description": ("Connector-side op_id (e.g. 'GET:/api/vcenter/cluster')."),
                },
                "custom_description": {"type": ["string", "null"]},
                "safety_level": {
                    "type": ["string", "null"],
                    "enum": ["safe", "caution", "dangerous", None],
                },
                "requires_approval": {"type": ["boolean", "null"]},
                "is_enabled": {"type": ["boolean", "null"]},
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
        name="meho.connector.enable",
        description=(
            "Flip every group of a connector to review_status='enabled' "
            "(tenant_admin only). Cascades is_enabled=true to every "
            "child op except those an operator previously set "
            "is_enabled=false on via meho.connector.edit_op (operator "
            "overrides stick). Use AFTER reviewing the connector with "
            "meho.connector.review + applying any needed edits. "
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
        name="meho.connector.disable",
        description=(
            "Flip every group of a connector to "
            "review_status='disabled' (tenant_admin only). Cascades "
            "is_enabled=false to every child op (the connector-level "
            "disable overrides per-op operator overrides for the "
            "duration of the disabled state). Use to roll back a "
            "regression — the connector's operations stop showing up "
            "in search_operations the moment this returns. Idempotent. "
            "Do NOT use as a soft toggle during normal review; pair "
            "with meho.connector.enable to bring the connector back "
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
