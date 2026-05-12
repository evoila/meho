# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Model Context Protocol (MCP) server module.

This package implements the MEHO MCP server surface per
[MCP spec revision 2025-06-18](https://modelcontextprotocol.io/specification/2025-06-18).
Hosted servers use the Streamable HTTP transport (POST to a single MCP
endpoint with JSON-RPC 2.0 envelopes); stdio is for local-subprocess
servers and is explicitly out of scope for MEHO.

* T1 (#246) shipped the **transport + dispatch skeleton** — the
  ``/mcp`` route, JSON-RPC envelope parsing, and the lifecycle
  primitives (``initialize`` / ``ping`` / ``notifications/initialized``).
* T2 (#247) layered the **OAuth 2.1 resource-server** chain on top —
  Bearer-token validation against the MCP canonical URI per RFC 8707
  §2, RFC 9728 ``WWW-Authenticate`` discovery header on 401s, and the
  ``/.well-known/oauth-protected-resource`` metadata document (in
  :mod:`meho_backplane.api.well_known`).
* T3 (#248, this Task) layers the **tool + resource registries** on
  top — :func:`register_mcp_tool` / :func:`register_mcp_resource`,
  the five JSON-RPC methods (``tools/list``, ``tools/call``,
  ``resources/list``, ``resources/templates/list``, ``resources/read``),
  RBAC filtering by :class:`~meho_backplane.auth.operator.TenantRole`,
  ``jsonschema``-based ``inputSchema`` validation, and the
  :func:`eager_import_mcp_modules` startup hook that auto-discovers
  every module under ``mcp/tools/`` and ``mcp/resources/``. T4 (#249)
  populates the registries with the reference ``meho.status`` tool and
  ``meho://tenant/{tenant_id}/info`` resource.

Public surface
==============

* :data:`~meho_backplane.mcp.server.router` — FastAPI ``APIRouter``
  mounted at ``/mcp`` by :mod:`meho_backplane.main`. Accepts JSON-RPC
  2.0 POST bodies (Streamable HTTP single-response shape) and routes
  each method to a registered handler. Every request runs through
  :func:`~meho_backplane.mcp.auth.verify_mcp_jwt_and_bind` before the
  body is parsed.
* :func:`~meho_backplane.mcp.server.register_method` — module-level
  JSON-RPC method registration; primarily used internally by T1's
  lifecycle handlers and T3's :mod:`~meho_backplane.mcp.handlers`.
* :func:`register_mcp_tool` / :func:`register_mcp_resource` — the
  registries every G3-G9 verb registers against. Tool / resource
  modules under ``mcp/tools/`` and ``mcp/resources/`` call these at
  their module top, then :func:`eager_import_mcp_modules` discovers
  them at FastAPI lifespan startup.
* :class:`ToolDefinition` / :class:`ResourceTemplateDefinition` —
  Pydantic v2 frozen models for the registry entries; ``required_role``
  is the RBAC gate, ``op_class`` is the audit-classification hint T5
  (#250) will consume.
* :func:`~meho_backplane.mcp.auth.verify_mcp_jwt` /
  :func:`~meho_backplane.mcp.auth.verify_mcp_jwt_and_bind` — the OAuth-RS
  dependency that validates the Bearer token against the chassis JWKS
  and the **MCP canonical URI**. On 401 it attaches the RFC 9728 §5.1
  ``WWW-Authenticate: Bearer resource_metadata=...`` header.
* :func:`~meho_backplane.mcp.auth.mcp_resource_uri` /
  :func:`~meho_backplane.mcp.auth.www_authenticate_header` — URI / header
  builders for the OAuth-RS chain.

T5 (#250) layers per-operation audit on top of T3's dispatch surface:
:func:`~meho_backplane.mcp.audit.write_mcp_audit_row` is called from
inside :func:`~meho_backplane.mcp.handlers.handle_tools_call` and
:func:`~meho_backplane.mcp.handlers.handle_resources_read` so each
invocation produces one :class:`~meho_backplane.db.models.AuditLog`
row regardless of how many calls share a JSON-RPC POST. The chassis
:class:`~meho_backplane.audit.AuditMiddleware` skips ``/mcp`` paths so
the granularity is one-row-per-operation, not one-row-per-envelope.

**Out of scope for T5** (will be picked up by later tasks): MCP Inspector
acceptance test + cross-repo docs (T6, #251). Origin-header validation
per the MCP transport DNS-rebinding warning and request-body size caps
remain deferred to a transport-hardening fast-follow.
"""

# Importing `handlers` runs the side-effect registration of the five T3
# JSON-RPC methods (tools/list, tools/call, resources/list,
# resources/templates/list, resources/read) against the dispatcher in
# `mcp/server.py`. Module access through `_handlers` keeps the import
# explicit for ruff's F401 check.
from meho_backplane.mcp import handlers as _handlers  # noqa: F401
from meho_backplane.mcp.auth import (
    mcp_resource_uri,
    verify_mcp_jwt,
    verify_mcp_jwt_and_bind,
    www_authenticate_header,
)
from meho_backplane.mcp.registry import (
    ResourceHandler,
    ResourceTemplateDefinition,
    ToolDefinition,
    ToolHandler,
    eager_import_mcp_modules,
    register_mcp_resource,
    register_mcp_tool,
)
from meho_backplane.mcp.server import McpInvalidParamsError, register_method, router

__all__ = [
    "McpInvalidParamsError",
    "ResourceHandler",
    "ResourceTemplateDefinition",
    "ToolDefinition",
    "ToolHandler",
    "eager_import_mcp_modules",
    "mcp_resource_uri",
    "register_mcp_resource",
    "register_mcp_tool",
    "register_method",
    "router",
    "verify_mcp_jwt",
    "verify_mcp_jwt_and_bind",
    "www_authenticate_header",
]
