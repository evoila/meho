# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Model Context Protocol (MCP) server module.

This package implements the MEHO MCP server surface per
[MCP spec revision 2025-06-18](https://modelcontextprotocol.io/specification/2025-06-18).
Hosted servers use the Streamable HTTP transport (POST to a single MCP
endpoint with JSON-RPC 2.0 envelopes); stdio is for local-subprocess
servers and is explicitly out of scope for MEHO.

T1 (this Task #246) ships the **transport + dispatch skeleton**:

* :data:`~meho_backplane.mcp.server.router` — FastAPI ``APIRouter``
  mounted at ``/mcp`` by :mod:`meho_backplane.main`. Accepts JSON-RPC
  2.0 POST bodies, parses + dispatches them, and returns either a
  single-shot JSON response (for *requests*) or HTTP 202 Accepted (for
  *notifications*) per the Streamable HTTP transport spec.
* :func:`~meho_backplane.mcp.server.register_method` — module-level
  dispatch registration mirroring the
  :func:`~meho_backplane.health.register_probe` pattern. T3 (#248) layers
  the tool / resource registries on top of this.
* Built-in handlers for the lifecycle primitives the spec defines as
  universal — ``initialize``, ``notifications/initialized``, and
  ``ping``.

**No Bearer-token auth in T1.** Every well-formed JSON-RPC request
currently succeeds. T2 (#247) adds the OAuth 2.1 resource-server
chain: ``/.well-known/oauth-protected-resource``, the
``WWW-Authenticate`` header on 401s, audience validation per RFC 8707,
and reuse of the existing :func:`~meho_backplane.auth.jwt.verify_jwt`
chain. Origin-header validation per the MCP transport security warning
(DNS rebinding defence) is also deferred to T2 — it requires the
``MCP_ALLOWED_ORIGINS`` setting T2 introduces.
"""

from meho_backplane.mcp.server import register_method, router

__all__ = ["register_method", "router"]
