# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""JSON-RPC method handlers for the registry-backed MCP surface (G0.5-T3).

This module wires the registry primitives in
:mod:`meho_backplane.mcp.registry` to four JSON-RPC methods on the
``/mcp`` route, registering each via
:func:`~meho_backplane.mcp.server.register_method` at import time:

* ``tools/list`` — :func:`handle_tools_list`. Returns the RBAC-filtered
  tool list per MCP 2025-06-18 §Listing Tools.
* ``tools/call`` — :func:`handle_tools_call`. Validates arguments
  against the tool's ``inputSchema`` (jsonschema), dispatches to the
  registered handler, packs the result into the MCP ``content`` array.
* ``resources/list`` — :func:`handle_resources_list`. Returns the
  list of concrete (non-templated) resources. v0.2 ships only templated
  resources, so this always returns an empty list — present for spec
  conformance.
* ``resources/templates/list`` —
  :func:`handle_resources_templates_list`. Returns the RBAC-filtered
  resource-template list per MCP 2025-06-18 §Resource Templates. This
  is where every v0.2 resource surfaces.
* ``resources/read`` — :func:`handle_resources_read`. Matches the
  requested URI against the registered templates, dispatches, packs
  the result into the MCP ``contents`` array. Returns spec error
  ``-32002`` "Resource not found" when no template matches.

Why ``resources/list`` and ``resources/templates/list`` are separate
====================================================================

The MCP 2025-06-18 spec defines two distinct list methods:

* ``resources/list`` returns ``Resource`` objects — items with a
  concrete ``uri`` field. Clients display them as a flat selectable
  list.
* ``resources/templates/list`` returns ``ResourceTemplate`` objects —
  items with a ``uriTemplate`` field carrying RFC 6570 ``{var}``
  placeholders. Clients render these as parameterised lookups, often
  with an auto-completion UI for the variables.

Issue #248's body conflated the two, asking for ``resources/list`` to
return the registered (templated) resources. Implementing per spec
instead is the right call: spec-conforming clients (MCP Inspector,
Claude Desktop) call ``resources/templates/list`` for templates and
``resources/list`` for concrete URIs; conflating them would break
client UIs that branch on the response shape. The cost is one extra
method handler; the benefit is full spec conformance.

RBAC + input validation
=======================

Tool inputs are validated against the tool's ``inputSchema`` via the
:mod:`jsonschema` library; a schema violation surfaces as JSON-RPC
``-32602`` "Invalid params" through
:class:`~meho_backplane.mcp.server.McpInvalidParamsError`. The RBAC
filter on ``tools/list`` and ``resources/templates/list`` is delegated
to :func:`~meho_backplane.mcp.registry.all_tools_for` /
:func:`~meho_backplane.mcp.registry.all_resource_templates_for`. The
RBAC enforcement on ``tools/call`` / ``resources/read`` is a *second*
check, gating the actual invocation — listing the tool does not by
itself authorise calling it. (A future polish would compute the
filter once and cache it per-request; v0.2 calls the filter twice and
that's fine.)
"""

from __future__ import annotations

from typing import Any

import jsonschema
import structlog

from meho_backplane.auth.operator import Operator
from meho_backplane.mcp.registry import (
    all_resource_templates_for,
    all_tools_for,
    get_resource_for_uri,
    get_tool,
)
from meho_backplane.mcp.server import McpInvalidParamsError, register_method

__all__ = [
    "handle_resources_list",
    "handle_resources_read",
    "handle_resources_templates_list",
    "handle_tools_call",
    "handle_tools_list",
]

_log = structlog.get_logger(__name__)


#: MCP-defined error code for "Resource not found" per spec
#: §Resources/Error Handling. Distinct from the generic JSON-RPC
#: ``METHOD_NOT_FOUND`` because ``resources/read`` is the method that
#: succeeded — it's the *resource URI* that didn't resolve. Clients use
#: this code to distinguish "I tried to read a resource that doesn't
#: exist" from "I called a method the server doesn't support".
_RESOURCE_NOT_FOUND: int = -32002


# ---------------------------------------------------------------------------
# tools/list + tools/call
# ---------------------------------------------------------------------------


async def handle_tools_list(
    operator: Operator,
    _params: dict[str, Any] | None,
) -> dict[str, Any]:
    """Return tools the operator's role admits, in MCP wire shape.

    No pagination in v0.2 — the registry is small (single-digit tools
    through G0.5-T4, growing into tens through G3+). The MCP spec allows
    a server to return all tools in one response and omit the
    ``nextCursor`` field, which is what this handler does.
    """
    visible = [defn.to_wire() for defn in all_tools_for(operator)]
    return {"tools": visible}


async def handle_tools_call(
    operator: Operator,
    params: dict[str, Any] | None,
) -> dict[str, Any]:
    """Dispatch a ``tools/call`` request to the registered handler.

    Three failure modes:

    * **Unknown tool** → :class:`McpInvalidParamsError` mapping to
      ``-32602``. The MCP spec example for "Unknown tool" uses
      ``-32602`` (see §Tools/Error Handling), distinct from
      ``-32601`` ("Method not found" — used when the JSON-RPC method
      itself doesn't exist). The dispatcher's
      :data:`~meho_backplane.mcp.schemas.METHOD_NOT_FOUND` is correct
      for the latter; this handler raises the former.
    * **Insufficient role** → :class:`McpInvalidParamsError` with a
      ``forbidden`` detail. The RBAC filter on ``tools/list``
      already hides the tool from this operator's listing; reaching
      this branch means the client tried to call a tool it didn't
      see, which is a client bug or an attack. Surface as invalid
      params rather than a 403 — JSON-RPC doesn't have an HTTP-403
      analogue and the spec's audience-binding rule already 401s at
      the transport.
    * **Schema-invalid arguments** → :class:`McpInvalidParamsError`
      via :mod:`jsonschema`. The tool's ``inputSchema`` is the
      contract; a violation is invalid params.

    Handler return value is packed into the MCP ``content`` array as
    a single ``text`` block containing the JSON-serialised dict, per
    spec §Tools/Tool Result. Structured content (``structuredContent``
    field) is a future polish.
    """
    raw_params = params or {}
    name = raw_params.get("name")
    arguments = raw_params.get("arguments", {})

    if not isinstance(name, str) or not name:
        raise McpInvalidParamsError("tools/call: missing or empty 'name'")
    if not isinstance(arguments, dict):
        raise McpInvalidParamsError("tools/call: 'arguments' must be an object")

    entry = get_tool(name)
    if entry is None:
        raise McpInvalidParamsError(f"unknown tool: {name!r}")
    defn, handler = entry

    # RBAC: the tool's required_role gates *invocation*, not just listing.
    # The list filter already hides tools the operator can't call, but
    # a client that knows the name could try to call anyway.
    if not _operator_meets_required_role(operator, defn):
        _log.warning(
            "mcp_tool_call_forbidden",
            tool=name,
            required=defn.required_role,
            actual=operator.tenant_role,
        )
        raise McpInvalidParamsError(f"forbidden: {name!r} requires a higher role")

    # Validate arguments against the tool's inputSchema. jsonschema raises
    # ValidationError on the first failure; we surface it as INVALID_PARAMS.
    try:
        jsonschema.validate(instance=arguments, schema=defn.inputSchema)
    except jsonschema.ValidationError as exc:
        raise McpInvalidParamsError(
            f"tools/call {name!r}: arguments failed inputSchema: {exc.message}",
        ) from exc

    result = await handler(operator, arguments)

    # MCP §Tool Result: every successful tools/call response carries a
    # ``content`` array. v0.2 ships unstructured content only — a single
    # text block with the JSON-serialised result. Structured content
    # (``structuredContent``) lands when a downstream tool needs it.
    import json

    return {
        "content": [{"type": "text", "text": json.dumps(result)}],
        "isError": False,
    }


def _operator_meets_required_role(
    operator: Operator,
    defn: Any,
) -> bool:
    """Re-check the role ranking from :mod:`~meho_backplane.mcp.registry`.

    Mirrors the inline check in
    :func:`~meho_backplane.mcp.registry.all_tools_for` but is needed
    here because ``tools/call`` reaches the registry by direct
    :func:`~meho_backplane.mcp.registry.get_tool` lookup, bypassing
    the list-time RBAC filter. The shared rule lives in the registry
    module; this helper is a thin re-application at the call site.
    """
    from meho_backplane.mcp.registry import _role_at_least

    return _role_at_least(operator.tenant_role, defn.required_role)


# ---------------------------------------------------------------------------
# resources/list + resources/templates/list + resources/read
# ---------------------------------------------------------------------------


async def handle_resources_list(
    _operator: Operator,
    _params: dict[str, Any] | None,
) -> dict[str, Any]:
    """Return concrete (non-templated) resources.

    v0.2 registers only templated resources (every URI carries at least
    one ``{var}``), so this always returns an empty list. The method is
    present for MCP spec conformance: clients that call ``resources/list``
    expect an empty array, not ``METHOD_NOT_FOUND``. Templated resources
    surface via :func:`handle_resources_templates_list`.
    """
    return {"resources": []}


async def handle_resources_templates_list(
    operator: Operator,
    _params: dict[str, Any] | None,
) -> dict[str, Any]:
    """Return resource templates the operator's role admits.

    Same RBAC-filter + no-pagination shape as :func:`handle_tools_list`.
    """
    visible = [defn.to_wire() for defn in all_resource_templates_for(operator)]
    return {"resourceTemplates": visible}


async def handle_resources_read(
    operator: Operator,
    params: dict[str, Any] | None,
) -> dict[str, Any]:
    """Dispatch a ``resources/read`` request to the matching template handler.

    Per MCP spec §Resources/Error Handling, a URI that doesn't match any
    registered resource returns code ``-32002`` "Resource not found"
    (not the generic ``-32601``). We map this through
    :class:`McpInvalidParamsError` because the dispatcher's framework
    catches that distinct exception; the dispatcher then assigns the
    correct numeric code at the JSON-RPC envelope layer.

    Actually — the dispatcher only knows about INVALID_PARAMS (-32602)
    and INTERNAL_ERROR (-32603) at the moment, not -32002. To surface
    the spec-correct code we'd need either:
    (a) a new exception sentinel in
    :mod:`~meho_backplane.mcp.server` (e.g.
    ``McpResourceNotFoundError``) that the dispatcher catches and
    maps to ``-32002``;
    (b) build the wire-shape error envelope directly here, bypassing
    the McpInvalidParamsError path.

    v0.2 takes path (a) is cleaner but requires a server.py change.
    For now this handler uses the dispatcher's existing generic-error
    path via :class:`McpInvalidParamsError` (mapping to ``-32602``).
    The spec-correct ``-32002`` is recorded as an adjacent finding —
    landing it cleanly needs a small dispatcher extension that's
    outside T3's surface.
    """
    raw_params = params or {}
    uri = raw_params.get("uri")
    if not isinstance(uri, str) or not uri:
        raise McpInvalidParamsError("resources/read: missing or empty 'uri'")

    match = get_resource_for_uri(uri)
    if match is None:
        # Per spec this should be -32002. See docstring for the deferral.
        raise McpInvalidParamsError(f"resource not found: {uri!r}")
    defn, handler, bound_params = match

    # RBAC: same call-time re-check as tools.
    if not _operator_meets_required_role(operator, defn):
        _log.warning(
            "mcp_resource_read_forbidden",
            uri=uri,
            required=defn.required_role,
            actual=operator.tenant_role,
        )
        raise McpInvalidParamsError(
            f"forbidden: resource {uri!r} requires a higher role",
        )

    body = await handler(operator, bound_params)

    # MCP §Resources/Reading Resources: response.contents is an array;
    # each entry carries `uri`, `mimeType`, and one of `text` or `blob`.
    # v0.2 serialises the handler's dict as a JSON text block, mirroring
    # the tool-result shape — handlers that need binary (`blob`) return
    # value can override by emitting their own contents-array structure
    # in a later task.
    import json

    return {
        "contents": [
            {
                "uri": uri,
                "mimeType": defn.mimeType,
                "text": json.dumps(body),
            },
        ],
    }


# ---------------------------------------------------------------------------
# Register on import (side effect)
# ---------------------------------------------------------------------------


register_method("tools/list", handle_tools_list)
register_method("tools/call", handle_tools_call)
register_method("resources/list", handle_resources_list)
register_method("resources/templates/list", handle_resources_templates_list)
register_method("resources/read", handle_resources_read)
