# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Governed discovery and result-handle meta-tools for hosted agent runs (#3737).

A hosted run that only has ``call_operation`` and the discovery pair cannot
find a connector, name a target, check a request before it runs, or read the
rest of a large result. This module gives configured definitions five more
tools, each one the MCP working-surface tool of the same name:

* ``meho_status`` — whether MEHO itself is healthy (#4233).
* ``meho_connector_list`` — which connectors the tenant can use. When the
  definition has a ``connectors`` allow-list, the toolset keeps only those
  rows (#4233).
* ``list_targets`` — which managed targets the tenant has (a large page comes
  back as a JSONFlux result handle).
* ``preview_operation`` — the request ``call_operation`` would send, without
  sending it.
* ``result_query`` — read rows back from a JSONFlux result handle.

The agent front-end does NOT re-implement them. Like the broadcast bridge
(:mod:`meho_backplane.agent.toolset_broadcast`), it resolves the
``(definition, handler)`` pair the MCP surface registered and reuses the
handler verbatim. So tenant scoping, target authorization (the cross-tenant
``platform_admin`` check in ``list_targets``), the per-principal rate limit on
``preview_operation``, and the result-handle owner check in ``result_query``
are the same code on both surfaces.

The MCP ``tools/call`` path runs gates *before* the handler that the agent
loop does not run on its own. :func:`_governed_handler` repeats them on every
call:

* role and capability, read from the run's operator at call time;
* the registered ``inputSchema`` (JSON Schema 2020-12). Some handlers rely on
  that schema for their bounds (``list_targets`` and ``result_query`` page
  ``limit``), and ``pydantic_ai.Tool.from_schema`` does not validate
  arguments against the advertised schema.

A failed gate, and any ``McpInvalidParamsError`` / ``McpRateLimitedError`` the
handler raises, reaches the model as :class:`~pydantic_ai.ModelRetry` (tool
feedback it can act on) instead of aborting the run.

``meho_status`` (#4233) builds the same health payload as the MCP tool and
``GET /api/v1/health``, then drops the fields that only describe an MCP
session (protocol version, session id capture, OAuth scopes, and the list of
MCP tools). A hosted run has none of these, and the MCP tool list would tell
the model it has tools it does not have. Its hosted role floor is
``OPERATOR``, the same as ``GET /api/v1/health``, because the check reads a
credential through Vault.

Retrieval tools (#3759)
=======================

:func:`build_retrieval_meta_tools` bridges three more working-surface tools
the same way: ``query_topology``, ``search_memory``, and ``search_knowledge``.
They return stored text from the tenant (Memory and knowledge base entries,
topology names), so the toolset resolver registers them only when a
definition names them in its ``meta_tools`` list. The two search tools' MCP
descriptions say to read a hit's ``locator`` or ``uri`` with
``resources/read``; a hosted run has no ``resources/read``, so their hosted
description adds one sentence that points at ``read_context_resource``.
"""

from __future__ import annotations

import copy
from typing import Any

import jsonschema
from pydantic_ai import ModelRetry

from meho_backplane.agent.meta_tool import MetaToolSpec, _MetaToolHandler
from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.mcp.registry import (
    ToolDefinition,
    ToolSurface,
    capability_satisfied,
    get_tool,
    role_at_least,
)
from meho_backplane.mcp.server import McpInvalidParamsError, McpRateLimitedError

# Imported for their registration side effect: each module runs the
# ``register_mcp_tool`` call for one of the bridged tools, so :func:`get_tool`
# can resolve its (definition, handler) pair below.
from meho_backplane.mcp.tools import connector_admin as _connector_admin_tools  # noqa: F401
from meho_backplane.mcp.tools import knowledge as _knowledge_tools  # noqa: F401
from meho_backplane.mcp.tools import memory as _memory_tools  # noqa: F401
from meho_backplane.mcp.tools import operations as _operations_tools  # noqa: F401
from meho_backplane.mcp.tools import result_query as _result_query_tools  # noqa: F401
from meho_backplane.mcp.tools import topology as _topology_tools  # noqa: F401

__all__ = [
    "CONNECTOR_LIST",
    "DISCOVERY_META_TOOL_NAMES",
    "RETRIEVAL_META_TOOL_NAMES",
    "build_discovery_meta_tools",
    "build_retrieval_meta_tools",
    "keep_allowed_connectors",
]


_MEHO_STATUS = "meho_status"
CONNECTOR_LIST = "meho_connector_list"
_LIST_TARGETS = "list_targets"
_PREVIEW_OPERATION = "preview_operation"
_RESULT_QUERY = "result_query"

#: The bridged tools, in the order they are advertised to the model. The
#: agent name is the MCP name, so an allow-list reads the same on both
#: surfaces.
DISCOVERY_META_TOOL_NAMES: tuple[str, ...] = (
    _MEHO_STATUS,
    CONNECTOR_LIST,
    _LIST_TARGETS,
    _PREVIEW_OPERATION,
    _RESULT_QUERY,
)

_QUERY_TOPOLOGY = "query_topology"
_SEARCH_MEMORY = "search_memory"
_SEARCH_KNOWLEDGE = "search_knowledge"

#: The bridged retrieval tools (#3759), in advertised order. Opt-in: see
#: :data:`~meho_backplane.agent.toolset.OPT_IN_META_TOOL_NAMES`.
RETRIEVAL_META_TOOL_NAMES: tuple[str, ...] = (
    _QUERY_TOPOLOGY,
    _SEARCH_MEMORY,
    _SEARCH_KNOWLEDGE,
)

#: Added to the MCP description of a bridged retrieval tool. The MCP text
#: names ``resources/read``, which a hosted run does not have.
_HOSTED_READ_NOTE = (
    " In this hosted run there is no `resources/read`: read a hit's `locator` "
    "or `uri` with `read_context_resource` when you have that tool, otherwise "
    "work from the snippet or passages returned here."
)

#: Hosted description for ``meho_status``. The MCP text talks about OAuth
#: scopes and the MCP tool list, which the hosted result leaves out.
_STATUS_DESCRIPTION = (
    "Check whether MEHO itself is healthy. Returns who you run as (tenant and "
    "role), whether MEHO can reach its credential store (`vault`), whether the "
    "database is migrated (`db`), the sensor runner state, and add-on "
    "pairings. Call this when the task asks about MEHO's own status, or when "
    "calls fail and you need to know if MEHO is the cause. No arguments. "
    "Read-only."
)

#: ``meho_status`` fields that only describe an MCP session. A hosted run is
#: not an MCP session, so the hosted tool leaves them out.
_MCP_SESSION_STATUS_FIELDS: frozenset[str] = frozenset(
    {"mcp_session_id_capture", "mcp_protocol_version", "granted_scopes", "surface_summary"}
)

#: Hosted description for ``meho_connector_list``. The MCP text points at
#: ``meho_connector_review``, an operator tool a hosted run does not have.
_CONNECTOR_LIST_DESCRIPTION = (
    "List the connectors your tenant can use (tenant-ingested plus built-in). "
    "Call this FIRST when you do not yet know which `connector_id` to pass to "
    "`list_operation_groups`, `search_operations`, or `call_operation`. Each "
    "row carries the `connector_id`, its review status, and "
    "`enabled_operation_count` (operations you can actually call). Optional "
    "`status` filter: `enabled`, `staged`, `disabled`, or `all` (default). "
    "Read-only."
)


def _mcp_tool(name: str) -> tuple[ToolDefinition, _MetaToolHandler]:
    """Return a registered working-surface MCP tool's ``(definition, handler)``.

    Missing registration, or a tool that moved off the working surface, is a
    configuration bug, so it fails loud instead of silently changing what a
    hosted run can reach.
    """
    entry = get_tool(name)
    if entry is None:  # pragma: no cover - defensive; the imports register all four
        raise RuntimeError(f"discovery bridge requires MCP tool {name!r} to be registered")
    definition, handler = entry
    if definition.surface is not ToolSurface.WORKING:  # pragma: no cover - defensive
        raise RuntimeError(f"discovery bridge only bridges working-surface tools; {name!r} is not")
    return definition, handler


def _governed_handler(definition: ToolDefinition, handler: _MetaToolHandler) -> _MetaToolHandler:
    """Wrap *handler* with the gates MCP ``tools/call`` runs before it.

    Role and capability are read from the operator of *this call*, so a
    model tool call cannot reach the handler under an identity the gate
    would refuse, even if the tool was registered. The arguments are checked
    against the registered ``inputSchema`` with the same validator and format
    checker :func:`~meho_backplane.mcp.handlers.handle_tools_call` uses.
    """
    name = definition.name

    async def _wrapped(operator: Operator, arguments: dict[str, Any]) -> dict[str, Any]:
        if not role_at_least(operator.tenant_role, definition.required_role):
            raise ModelRetry(f"forbidden: {name!r} requires a higher role. Do not retry it.")
        if not capability_satisfied(operator, definition.required_capability):
            raise ModelRetry(
                f"forbidden: {name!r} requires a capability this tenant does not have. "
                "Do not retry it."
            )
        try:
            jsonschema.validate(
                instance=arguments,
                schema=definition.inputSchema,
                cls=jsonschema.Draft202012Validator,
                format_checker=jsonschema.Draft202012Validator.FORMAT_CHECKER,
            )
        except jsonschema.ValidationError as exc:
            raise ModelRetry(f"{name}: invalid arguments: {exc.message}") from exc
        try:
            return await handler(operator, arguments)
        except (McpInvalidParamsError, McpRateLimitedError) as exc:
            raise ModelRetry(str(exc)) from exc

    return _wrapped


def keep_allowed_connectors(
    result: dict[str, Any],
    allowed_connectors: frozenset[str],
) -> dict[str, Any]:
    """Return a ``meho_connector_list`` *result* with only the allowed rows.

    The toolset calls this when the spec has a ``connectors`` allow-list, so
    the agent is shown only the connectors it may call (#4233).
    """
    rows = result.get("connectors", [])
    return {
        **result,
        "connectors": [r for r in rows if r.get("connector_id") in allowed_connectors],
    }


async def _hosted_status(operator: Operator, arguments: dict[str, Any]) -> dict[str, Any]:
    """Return the ``meho_status`` health payload without the MCP-session fields.

    Same payload builder as the MCP tool and ``GET /api/v1/health``.
    """
    # Imported here: the health module imports the agent package, so a
    # module-level import would be circular.
    from meho_backplane.api.v1.health import build_health_response

    if arguments:
        raise ModelRetry(f"{_MEHO_STATUS} takes no arguments.")
    health = (await build_health_response(operator)).model_dump(mode="json")
    return {k: v for k, v in health.items() if k not in _MCP_SESSION_STATUS_FIELDS}


def _bridge(name: str, *, description: str | None = None) -> MetaToolSpec:
    """Build one :class:`MetaToolSpec` from the registered MCP tool *name*.

    The schema is the MCP **wire** schema (``to_wire()``), deep-copied, for
    the same reason the broadcast bridge uses it (#2644): it strips the
    top-level combinators the Anthropic Messages API rejects. The role floor
    is the MCP tool's own ``required_role``.
    """
    definition, handler = _mcp_tool(name)
    return MetaToolSpec(
        name=name,
        handler=_governed_handler(definition, handler),
        description=description if description is not None else definition.description,
        parameter_schema=copy.deepcopy(definition.to_wire()["inputSchema"]),
        required_role=definition.required_role,
    )


def build_discovery_meta_tools() -> tuple[MetaToolSpec, ...]:
    """Build the five governed status, discovery, and result-handle meta-tools."""
    # Built here, not with _bridge: the MCP module for this tool imports the
    # agent package, so it is not registered yet when this catalog is built.
    # The hosted floor is OPERATOR, like GET /api/v1/health, because the
    # check reads a credential through Vault. The MCP floor is lower.
    status = MetaToolSpec(
        name=_MEHO_STATUS,
        handler=_hosted_status,
        description=_STATUS_DESCRIPTION,
        parameter_schema={"type": "object", "properties": {}, "additionalProperties": False},
        required_role=TenantRole.OPERATOR,
    )
    return (
        status,
        _bridge(CONNECTOR_LIST, description=_CONNECTOR_LIST_DESCRIPTION),
        _bridge(_LIST_TARGETS),
        _bridge(_PREVIEW_OPERATION),
        _bridge(_RESULT_QUERY),
    )


def build_retrieval_meta_tools() -> tuple[MetaToolSpec, ...]:
    """Build the three governed retrieval meta-tools (#3759).

    Same bridge as :func:`build_discovery_meta_tools`, so the MCP handler,
    tenant scoping, Memory RBAC, the untrusted-text envelope on stored text,
    and the response size limits are the same code as on MCP.
    """
    search_tools = tuple(
        _bridge(name, description=_mcp_tool(name)[0].description + _HOSTED_READ_NOTE)
        for name in (_SEARCH_MEMORY, _SEARCH_KNOWLEDGE)
    )
    return (_bridge(_QUERY_TOPOLOGY), *search_tools)
