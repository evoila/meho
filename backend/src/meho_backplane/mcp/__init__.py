# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Model Context Protocol (MCP) server module.

This package implements the MEHO MCP server surface per
[MCP spec revision 2025-06-18](https://modelcontextprotocol.io/specification/2025-06-18).
Hosted servers use the Streamable HTTP transport (POST to a single MCP
endpoint with JSON-RPC 2.0 envelopes); stdio is for local-subprocess
servers and is explicitly out of scope for MEHO.

T1 (#246) shipped the **transport + dispatch skeleton**; T2 (this
Task #247) layers the **OAuth 2.1 resource-server** chain on top.
Together they expose:

* :data:`~meho_backplane.mcp.server.router` — FastAPI ``APIRouter``
  mounted at ``/mcp`` by :mod:`meho_backplane.main`. Accepts JSON-RPC
  2.0 POST bodies (Streamable HTTP single-response shape) and routes
  each method to a registered handler. Every request runs through
  :func:`~meho_backplane.mcp.auth.verify_mcp_jwt_and_bind` before the
  body is parsed.
* :func:`~meho_backplane.mcp.server.register_method` — module-level
  dispatch registration mirroring the
  :func:`~meho_backplane.health.register_probe` pattern. T3 (#248) layers
  the tool / resource registries on top of this.
* Built-in handlers for the lifecycle primitives the spec defines as
  universal — ``initialize``, ``notifications/initialized``, and
  ``ping``.
* :func:`~meho_backplane.mcp.auth.verify_mcp_jwt` /
  :func:`~meho_backplane.mcp.auth.verify_mcp_jwt_and_bind` — the OAuth-RS
  dependency that validates the Bearer token against the chassis JWKS
  and the **MCP canonical URI** (RFC 8707 §2 audience binding). On 401
  it attaches the RFC 9728 §5.1 ``WWW-Authenticate: Bearer
  resource_metadata=...`` header so spec-conforming clients can
  discover the metadata document. The chassis HTTP API keeps its own
  audience (``KEYCLOAK_AUDIENCE``) so a token issued for one surface
  is never replayable on the other.
* :func:`~meho_backplane.mcp.auth.mcp_resource_uri` /
  :func:`~meho_backplane.mcp.auth.www_authenticate_header` — helpers
  that resolve the canonical URI from ``MCP_RESOURCE_URI`` /
  ``BACKPLANE_URL`` and build the discovery-header value. Re-exported
  so T3 / T6 can reuse them without reaching into the auth module.

The unauthenticated metadata document at
``/.well-known/oauth-protected-resource`` lives in
:mod:`meho_backplane.api.well_known`. That router is what an MCP client
fetches *before* it has a token, to discover the authorisation server.

**Out of scope for T2** (deferred to a later transport-hardening
task): Origin-header validation per the MCP transport DNS-rebinding
warning, content-type 415 strict enforcement, and request-body size
caps. These need ``MCP_ALLOWED_ORIGINS`` / size-cap settings that
haven't been added yet; bundling them with T2 would over-scope the
OAuth-RS work.
"""

from meho_backplane.mcp.auth import (
    mcp_resource_uri,
    verify_mcp_jwt,
    verify_mcp_jwt_and_bind,
    www_authenticate_header,
)
from meho_backplane.mcp.server import McpInvalidParamsError, register_method, router

__all__ = [
    "McpInvalidParamsError",
    "mcp_resource_uri",
    "register_method",
    "router",
    "verify_mcp_jwt",
    "verify_mcp_jwt_and_bind",
    "www_authenticate_header",
]
