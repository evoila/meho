# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Toolset resolution + handler-to-agent-tool adapter (G11.1-T3 / #810).

This module turns an agent definition's *toolset spec* plus the run's
*identity* into the concrete list of :class:`pydantic_ai.Tool` objects the
:class:`~meho_backplane.agent.run.PydanticAgentRun` loop registers. The
registered set is the **intersection** of two sets:

* **toolset spec** — what the definition author asked for (a free-shaped
  JSON object persisted on
  :class:`~meho_backplane.db.models.AgentDefinition.toolset`; T2 #809 only
  stores it, this Task resolves it).
* **identity permissions** — what the agent's identity is allowed to call.
  The permission *model* (a per-op grant table) is G11.2; until it lands,
  the permission an identity carries today is its tenant role, gated the
  same way every other MEHO surface gates it: each meta-tool declares a
  :class:`~meho_backplane.auth.operator.TenantRole` floor and
  :func:`~meho_backplane.mcp.registry.role_at_least` decides admission.
  This Task *consumes* that gate; it does not invent a new one.

A meta-tool the identity may not call is **not registered** — it is absent
from the agent's tool surface, so the model cannot even attempt it. This is
the least-privilege posture: the safest tool is one that does not exist on
the surface (ai_engineering best practices, tool-surface minimisation).

Why the agent surface is meta-tools, not per-op tools
=====================================================

CLAUDE.md postulate 5 is load-bearing: the agent never sees vendor-specific
tools (no ``vsphere.vm.list`` in the tool list). All execution flows through
``call_operation(connector_id, op_id, ...)``. So "connector-ops ∩ identity
permissions" is **not** "register one Pydantic AI tool per connector op" —
that would put vendor identifiers in tool names, which the architecture
forbids. Connector-op scoping instead rides on the ``call_operation`` tool:
the toolset spec may carry a ``connectors`` allow-list, and the wrapped
``call_operation`` rejects a dispatch to a connector outside that list with
a structured, agent-reasonable error (a :class:`pydantic_ai.ModelRetry`,
not a crash). Per-op RBAC + tenant scoping + audit + sanitization still fire
unchanged inside :func:`~meho_backplane.operations.dispatch` — the agent
layer adds connector-level scoping on top, it does not replace the
dispatch-time gate.

Toolset spec shape
==================

The spec is intentionally small and forward-compatible (JSON-shaped so it
can grow without a migration — the T2 design constraint):

.. code-block:: json

    {
      "meta_tools": ["list_operation_groups", "search_operations", "call_operation"],
      "connectors": ["vmware-rest-9.0", "vault-1.x"]
    }

* ``meta_tools`` — the allow-list of meta-tool names to register. Omitted /
  ``null`` means "all meta-tools the identity's role admits" (the safe
  default for a definition that just wants the standard discovery +
  execution surface). An empty list registers no tools.
* ``connectors`` — an optional allow-list of ``connector_id`` values the
  agent may reach through ``call_operation``. Omitted / ``null`` means "no
  connector-level restriction beyond what dispatch already enforces"
  (the tenant boundary + per-op RBAC still apply). An empty list forbids
  every connector — ``call_operation`` is registered but every dispatch
  is rejected before it reaches the dispatcher.

Unknown keys in the spec are ignored (forward-compat): a future spec field
must not break an older runtime.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

import structlog
from pydantic_ai import ModelRetry, RunContext, Tool

from meho_backplane.agent.approval_wait import resume_or_surface_awaiting_approval
from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.mcp.registry import role_at_least
from meho_backplane.operations.meta_tools import (
    call_operation,
    list_operation_groups,
    search_operations,
)
from meho_backplane.settings import get_settings

__all__ = [
    "META_TOOL_NAMES",
    "MetaToolSpec",
    "resolve_agent_tools",
]

_log = structlog.get_logger(__name__)

#: The shape of a handler the agent meta-tools adapt: the existing
#: ``(operator, arguments) -> dict`` MEHO meta-tool signature shared by the
#: REST, MCP, and CLI surfaces. The adapter repacks the framework's
#: ``RunContext``-first, keyword-only tool call onto this shape.
_MetaToolHandler = Callable[[Operator, dict[str, Any]], Awaitable[dict[str, Any]]]


class MetaToolSpec:
    """One meta-tool's agent-facing wiring: handler + schema + role floor.

    The agent front-end's source of truth for *its* tool surface, parallel
    to the MCP front-end's :func:`~meho_backplane.mcp.registry.register_mcp_tool`
    registrations in :mod:`meho_backplane.mcp.tools.operations`. Both adapt
    the same handlers in :mod:`meho_backplane.operations.meta_tools`; neither
    is a wrapper around the other (the CLAUDE.md dual-front-end contract).

    Fields:

    * ``name`` — the tool name the model sees (matches the MCP tool name so
      a definition author's allow-list reads identically across surfaces).
    * ``handler`` — the ``(operator, arguments) -> dict`` meta-tool handler.
    * ``description`` — the model-facing prompt for *when* to call the tool.
    * ``parameter_schema`` — JSON Schema 2020-12 for the tool's arguments;
      the framework advertises it to the model. The dispatcher re-validates
      ``call_operation`` params against the descriptor's own
      ``parameter_schema`` (defence in depth — the agent-tool schema only
      shapes the meta-tool *arguments*, not the per-op params).
    * ``required_role`` — the :class:`TenantRole` floor; the identity must
      meet it (via :func:`role_at_least`) for the tool to register.
    """

    __slots__ = ("description", "handler", "name", "parameter_schema", "required_role")

    def __init__(
        self,
        *,
        name: str,
        handler: _MetaToolHandler,
        description: str,
        parameter_schema: dict[str, Any],
        required_role: TenantRole,
    ) -> None:
        self.name = name
        self.handler = handler
        self.description = description
        self.parameter_schema = parameter_schema
        self.required_role = required_role


#: Argument key on the ``call_operation`` meta-tool that names the connector
#: the dispatch targets. Lifted to a constant so the connector allow-list
#: check and the schema stay in lock-step.
_CONNECTOR_ID_ARG = "connector_id"


#: The agent meta-tool catalog. Three meta-tools form the agent's working
#: surface over the G0.6 substrate (CLAUDE.md "the load-bearing pattern":
#: pick connector → list operation groups → search operations → call). The
#: parameter schemas mirror the canonical MCP ``inputSchema`` fragments in
#: :mod:`meho_backplane.mcp.tools.operations`; the descriptions are the
#: model's prompt for when to reach for each tool.
_META_TOOL_CATALOG: tuple[MetaToolSpec, ...] = (
    MetaToolSpec(
        name="list_operation_groups",
        handler=list_operation_groups,
        description=(
            "List a connector's enabled operation groups so you can scope a "
            "later operation search. Call this FIRST when you don't yet know "
            "which operation to invoke: it narrows hundreds of operations to "
            "a handful of relevant groups, each with a `when_to_use` blurb. "
            "Argument: `connector_id` in `<impl_id>-<version>` form (e.g. "
            '"vmware-rest-9.0", "vault-1.x") — not the bare product name.'
        ),
        parameter_schema={
            "type": "object",
            "properties": {
                "connector_id": {
                    "type": "string",
                    "minLength": 1,
                    "description": (
                        "Connector identifier in `<impl_id>-<version>` form "
                        '(e.g. "vmware-rest-9.0", "vault-1.x").'
                    ),
                },
            },
            "required": ["connector_id"],
            "additionalProperties": False,
        },
        required_role=TenantRole.OPERATOR,
    ),
    MetaToolSpec(
        name="search_operations",
        handler=search_operations,
        description=(
            "Hybrid lexical + semantic search over a connector's enabled "
            "operations. Use this AFTER `list_operation_groups` has narrowed "
            "the surface to one group, or directly when the query is specific "
            "enough. Returns ranked operations; inspect each hit's "
            "`safety_level` and `requires_approval` before calling it. "
            "Arguments: `connector_id` (required), `query` (required, "
            "free-form), `group` (optional group_key), `limit` (default 10, "
            "max 50)."
        ),
        parameter_schema={
            "type": "object",
            "properties": {
                "connector_id": {"type": "string", "minLength": 1},
                "query": {"type": "string", "minLength": 1},
                "group": {"type": ["string", "null"]},
                "limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 10},
            },
            "required": ["connector_id", "query"],
            "additionalProperties": False,
        },
        required_role=TenantRole.OPERATOR,
    ),
    MetaToolSpec(
        name="call_operation",
        handler=call_operation,
        description=(
            "Invoke an operation through MEHO's governed dispatch path. Use "
            "ONLY after `search_operations` returned an `op_id` you've "
            "confirmed is appropriate (check `safety_level` and "
            "`requires_approval`). The dispatcher validates `params` against "
            "the operation's parameter schema and returns either the result "
            '(`status="ok"`) or a structured error in the same envelope. '
            "Arguments: `connector_id` (required), `op_id` (required), "
            '`target` (optional `{"name": "<slug>"}` for ops acting on a '
            "managed target), `params` (operation-specific)."
        ),
        parameter_schema={
            "type": "object",
            "properties": {
                "connector_id": {"type": "string", "minLength": 1},
                "op_id": {"type": "string", "minLength": 1},
                "target": {
                    "type": ["object", "null"],
                    "properties": {
                        "name": {"type": "string", "minLength": 1},
                        "fqdn": {"type": "string", "minLength": 1},
                    },
                },
                "params": {"type": "object"},
            },
            "required": ["connector_id", "op_id"],
            "additionalProperties": False,
        },
        required_role=TenantRole.OPERATOR,
    ),
)

#: The set of meta-tool names the agent surface knows about. Public so a
#: toolset spec validator (or a test) can check an allow-list against the
#: real catalog without importing the catalog tuple itself.
META_TOOL_NAMES: frozenset[str] = frozenset(spec.name for spec in _META_TOOL_CATALOG)


def _extract_str_list(spec: dict[str, Any], key: str) -> list[str] | None:
    """Return *spec[key]* as a list of strings, or ``None`` if absent.

    A missing key (or an explicit ``null``) returns ``None`` — the "no
    restriction" sentinel both ``meta_tools`` and ``connectors`` use. A
    present-but-non-list value, or a list with a non-string element, raises
    :class:`ValueError`: a mis-shaped spec is a definition-authoring bug that
    should fail loud at resolution time, not silently widen or narrow the
    agent's surface.
    """
    if key not in spec or spec[key] is None:
        return None
    value = spec[key]
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError(
            f"toolset spec field {key!r} must be a list of strings when present; "
            f"got {type(value).__name__}"
        )
    return value


def _make_meta_tool(
    meta: MetaToolSpec,
    *,
    allowed_connectors: frozenset[str] | None,
) -> Tool[Operator]:
    """Build one :class:`pydantic_ai.Tool` wrapping *meta*'s handler.

    The wrapper adapts the framework's ``RunContext``-first, keyword-only
    tool call onto the ``(operator, arguments) -> dict`` handler shape: the
    operator comes from ``ctx.deps`` (so dispatch-time RBAC + audit + the
    sanitizing reducer all see the run's real principal), and the validated
    keyword arguments are repacked into the ``arguments`` dict the handler
    reads.

    For ``call_operation`` with a connector allow-list, the wrapper performs
    a pre-dispatch connector check and raises :class:`ModelRetry` — an
    agent-reasonable structured error, not a crash — when the requested
    connector is outside the spec's ``connectors`` list.

    For ``call_operation`` specifically the wrapper also threads the G11.1-T9
    (#1117) approval-resume substrate: when the dispatch returns
    ``status="awaiting_approval"``, the wrapper blocks on
    :func:`~meho_backplane.agent.approval_wait.wait_for_approval_decision`
    (subscribing to the per-tenant broadcast feed for an
    ``approval.{approved,rejected}`` event keyed on the request id) and
    either re-dispatches with ``_approved=True`` (approved) or surfaces the
    decision to the model (rejected / timeout). This implements the
    operator/agent split: the operator's decision flows through the durable
    approval queue + broadcast, the agent's resume flows through the
    re-dispatch substrate here. Every other path (and every other meta-tool)
    flows straight through to the handler.
    """

    handler = meta.handler
    is_call_operation = meta.name == "call_operation"

    async def _tool(ctx: RunContext[Operator], **arguments: Any) -> dict[str, Any]:
        if is_call_operation and allowed_connectors is not None:
            connector_id = arguments.get(_CONNECTOR_ID_ARG)
            if connector_id not in allowed_connectors:
                # Mirror the dispatcher's `denied` envelope shape but raise
                # it as a ModelRetry so the model receives a tool-level
                # retry prompt it can reason about (pick an allowed
                # connector) rather than a tool-execution crash.
                _log.info(
                    "agent_tool_connector_denied",
                    connector_id=connector_id,
                    operator_sub=ctx.deps.sub,
                    tenant_id=str(ctx.deps.tenant_id),
                )
                raise ModelRetry(
                    f"connector_id {connector_id!r} is not in this agent's "
                    f"allowed connectors {sorted(allowed_connectors)!r}. Pick "
                    f"an allowed connector or stop."
                )
        call_arguments = dict(arguments)
        result = await handler(ctx.deps, call_arguments)
        if is_call_operation and result.get("status") == "awaiting_approval":
            settings = get_settings()
            return await resume_or_surface_awaiting_approval(
                operator=ctx.deps,
                call_arguments=call_arguments,
                awaiting_envelope=result,
                timeout_seconds=settings.agent_approval_wait_timeout_seconds,
            )
        return result

    return Tool.from_schema(
        _tool,
        name=meta.name,
        description=meta.description,
        json_schema=meta.parameter_schema,
        takes_ctx=True,
    )


def resolve_agent_tools(
    toolset: dict[str, Any] | None,
    operator: Operator,
) -> list[Tool[Operator]]:
    """Resolve the toolset spec ∩ identity permissions into Pydantic AI tools.

    Returns the list of :class:`pydantic_ai.Tool` objects to register on the
    loop's :class:`~pydantic_ai.Agent`. The set is the intersection of:

    * the spec's ``meta_tools`` allow-list (or all meta-tools when omitted),
      and
    * the meta-tools the *operator*'s role admits (via
      :func:`~meho_backplane.mcp.registry.role_at_least`).

    A meta-tool failing either side is absent from the result — the
    least-privilege default. A meta-tool name in the spec that the catalog
    does not know is ignored with a warning (forward-compat: an allow-list
    may name a tool a newer runtime ships; an older runtime simply can't
    register it).

    The spec's ``connectors`` allow-list (when present) is threaded into the
    ``call_operation`` tool so a dispatch to a connector outside the list is
    rejected with a structured, agent-reasonable error before it reaches the
    dispatcher.

    Args:
        toolset: the definition's toolset spec (``AgentDefinition.toolset``),
            or ``None`` for the default surface. See the module docstring for
            the shape.
        operator: the run's principal; its ``tenant_role`` is the identity
            permission the meta-tool floors are intersected against.

    Returns:
        The meta-tools to register, in catalog order.

    Raises:
        ValueError: when ``meta_tools`` or ``connectors`` is present but not
            a list of strings.
    """
    spec = toolset or {}
    requested_names = _extract_str_list(spec, "meta_tools")
    connectors = _extract_str_list(spec, "connectors")
    allowed_connectors = frozenset(connectors) if connectors is not None else None

    # An unknown name in the spec is forward-compat noise, not an error;
    # log it once so a definition author sees the typo / version gap.
    if requested_names is not None:
        for name in requested_names:
            if name not in META_TOOL_NAMES:
                _log.warning(
                    "agent_toolset_unknown_meta_tool",
                    meta_tool=name,
                    known=sorted(META_TOOL_NAMES),
                )

    tools: list[Tool[Operator]] = []
    for meta in _META_TOOL_CATALOG:
        # Side 1 — toolset spec: omitted allow-list means "all"; a present
        # list must contain the name.
        if requested_names is not None and meta.name not in requested_names:
            continue
        # Side 2 — identity permissions: the role must meet the tool's floor.
        # A tool the identity can't call is simply not registered.
        if not role_at_least(operator.tenant_role, meta.required_role):
            continue
        tools.append(_make_meta_tool(meta, allowed_connectors=allowed_connectors))

    connector_allow_list = sorted(allowed_connectors) if allowed_connectors is not None else None
    _log.info(
        "agent_toolset_resolved",
        operator_sub=operator.sub,
        tenant_id=str(operator.tenant_id),
        tenant_role=operator.tenant_role.value,
        registered=[t.name for t in tools],
        connector_allow_list=connector_allow_list,
    )
    return tools
