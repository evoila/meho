# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``/.well-known/*`` discovery surfaces — unauthenticated metadata documents.

This module hosts the discovery routes a third-party MCP client hits
*before* it has a token to present. By construction these endpoints
MUST be reachable without authentication; that's the bootstrap step
that lets the client learn which authorisation server to talk to in
order to obtain the access token the protected ``/mcp`` route demands.

v0.2 ships one route here: ``/.well-known/oauth-protected-resource``
per RFC 9728 §3. It describes the backplane's MCP server resource so
spec-conforming MCP clients (Claude Desktop, MCP Inspector, custom SDK
consumers) can:

1. POST to ``/mcp`` without a token → server returns 401 +
   ``WWW-Authenticate: Bearer resource_metadata="<metadata-url>"``
   (per :mod:`meho_backplane.mcp.auth`).
2. GET ``<metadata-url>`` → returns this route's JSON body.
3. Extract ``authorization_servers[0]`` and fetch its own metadata
   (``/.well-known/oauth-authorization-server`` per RFC 8414, owned by
   Keycloak).
4. Run the OAuth 2.1 + PKCE handshake against Keycloak with the
   ``resource`` parameter set to ``resource`` from this document.
5. Re-POST to ``/mcp`` with the issued access token in
   ``Authorization: Bearer ...``.

The chassis HTTP API has its own discovery surface for the CLI
(``/api/v1/auth-config``); that endpoint is the CLI's bootstrap path
and reads ``KEYCLOAK_AUDIENCE`` rather than the MCP resource URI. The
two coexist because the chassis and MCP surfaces have **distinct
audiences** — see :mod:`meho_backplane.mcp.auth`.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from meho_backplane.mcp.auth import mcp_resource_uri
from meho_backplane.settings import get_settings

__all__ = ["router"]


router = APIRouter(prefix="/.well-known", tags=["discovery"])


@router.get("/oauth-protected-resource")
async def protected_resource_metadata() -> dict[str, Any]:
    """Return the RFC 9728 protected-resource metadata document.

    Fields per RFC 9728 §3:

    * ``resource`` (REQUIRED) — the canonical MCP server URI clients
      pass as the ``resource`` parameter in OAuth requests per RFC 8707.
      Resolved via :func:`~meho_backplane.mcp.auth.mcp_resource_uri`,
      which prefers an explicit ``MCP_RESOURCE_URI`` setting and falls
      back to ``f"{backplane_url}/mcp"``.
    * ``authorization_servers`` — the Keycloak issuer URL. MCP 2025-06-18
      §Authorization Server Discovery requires this to be non-empty.
    * ``scopes_supported`` — v0.2 ships two scopes that downstream
      Tasks (T3 RBAC filter, T4 reference tool, T5 audit) consume.
      Conservative: clients that request only ``mcp:read`` can list /
      inspect but not invoke side-effecting tools.
    * ``bearer_methods_supported`` — ``["header"]`` only. MCP transport
      forbids tokens in the URI query (see §"Access Token Usage").

    No authentication is enforced on this endpoint by design: RFC 9728
    discovery is the bootstrap step that lets unauthenticated clients
    learn how to obtain a token. The chassis middleware chain still
    runs — the request_id stays bound for log correlation, and
    AuditMiddleware skips the row because no ``operator_sub`` is bound
    (the standard unauthenticated-route path).
    """
    settings = get_settings()
    return {
        "resource": mcp_resource_uri(settings),
        "authorization_servers": [
            str(settings.keycloak_issuer_url).rstrip("/"),
        ],
        "scopes_supported": ["mcp:read", "mcp:execute"],
        "bearer_methods_supported": ["header"],
    }
