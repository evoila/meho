# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""OAuth 2.1 resource-server chain for the ``/mcp`` route.

This module is the MCP-side of the auth boundary: it consumes the same
Keycloak JWTs the chassis accepts but enforces a **different audience**
— the canonical MCP resource URI per RFC 8707 §2 — and emits the
RFC 9728 §5.1 ``WWW-Authenticate`` header on 401 so spec-conforming
MCP clients can discover the protected-resource metadata document and
follow the OAuth 2.1 + PKCE handshake. The chassis ``verify_jwt`` keeps
its existing ``KEYCLOAK_AUDIENCE`` audience for HTTP API routes; MCP
gets its own audience binding so a token issued for the HTTP API can't
be replayed at the MCP endpoint and vice-versa.

Reused chassis infrastructure
=============================

* :func:`~meho_backplane.auth.jwt.verify_jwt_for_audience` — the
  parameterised seam (signature + claims + kid-rotation + Operator
  projection). MCP simply passes its own ``expected_audience``.
* :class:`~meho_backplane.auth.operator.Operator` — same identity
  shape (sub / name / email / tenant_id / tenant_role / raw_jwt). MCP
  routes receive the operator by injection just like chassis routes.
* :func:`~meho_backplane.middleware.verify_jwt_and_bind` is **not**
  reused: this module ships its own :func:`verify_mcp_jwt_and_bind`
  that mirrors the binding contract (operator_sub + tenant_id into
  structlog contextvars) but layers WWW-Authenticate header injection
  on the 401 path.

Spec citations
==============

* MCP 2025-06-18 §Authorization —
  https://modelcontextprotocol.io/specification/2025-06-18/basic/authorization
  ("MCP servers MUST validate that tokens presented to them were
  specifically issued for them as the intended audience, according to
  RFC 8707 §2. Invalid or expired tokens MUST receive a HTTP 401
  response.")
* RFC 9728 §5.1 — Bearer-scheme WWW-Authenticate response containing
  the ``resource_metadata`` parameter pointing at the protected-
  resource metadata document.
* RFC 8707 — Resource Indicators. The ``resource`` parameter sent at
  the OAuth-AS during authorisation must equal the canonical MCP URI;
  the issued token's ``aud`` claim then binds to that resource.
"""

from __future__ import annotations

import structlog
from fastapi import Depends, Header, HTTPException

from meho_backplane.auth.jwt import verify_jwt_for_audience
from meho_backplane.auth.operator import Operator
from meho_backplane.settings import Settings, get_settings

__all__ = [
    "mcp_resource_uri",
    "verify_mcp_jwt",
    "verify_mcp_jwt_and_bind",
    "www_authenticate_header",
]

_log = structlog.get_logger(__name__)


def mcp_resource_uri(settings: Settings | None = None) -> str:
    """Return the canonical MCP server URI for audience binding.

    Resolves in priority order:

    1. ``Settings.mcp_resource_uri`` when explicitly set — operators
       with non-default MCP mount points (e.g. ``/api/mcp``) override
       per environment. The value is normalised (``.strip()`` +
       ``.rstrip('/')``) so an operator who pastes the URI from a
       browser bar with a trailing slash, or leaves a stray newline in
       the env var, doesn't cause an ``aud`` mismatch against tokens
       whose ``resource`` parameter Keycloak emitted in canonical
       form. MCP 2025-06-18 §"Canonical Server URI" mandates the no-
       trailing-slash form for interop, so the normalisation also
       enforces the spec contract.
    2. ``f"{settings.backplane_url}/mcp"`` derived at use time.
    3. Empty string when neither is set — fail-closed for MCP: no JWT
       can have an empty audience claim, so every ``/mcp`` request 401s
       until an operator wires ``BACKPLANE_URL`` (or ``MCP_RESOURCE_URI``).
       Defence in depth: :func:`~meho_backplane.auth.jwt.verify_jwt_for_audience`
       short-circuits an empty expected_audience to a 401 explicitly so
       the fail-closed property is local to the verifier and doesn't
       depend on any honest-issuer assumption.

    Per MCP 2025-06-18 §Resource Parameter Implementation, the canonical
    form is lowercase scheme + host with no trailing slash; the helper
    strips any trailing slash on ``backplane_url`` before concatenating
    to enforce that convention.
    """
    settings = settings or get_settings()
    if settings.mcp_resource_uri:
        return settings.mcp_resource_uri.strip().rstrip("/")
    if not settings.backplane_url:
        return ""
    return f"{settings.backplane_url.rstrip('/')}/mcp"


def www_authenticate_header(settings: Settings | None = None) -> str:
    """Build the RFC 9728 §5.1 ``WWW-Authenticate`` header value.

    Shape per spec example::

        Bearer resource_metadata="https://meho.example/.well-known/oauth-protected-resource"

    The metadata URL is rooted at ``backplane_url`` because RFC 9728
    §3 hosts the document at ``/.well-known/oauth-protected-resource``
    *on the resource server's origin*, not on the authorisation
    server. When ``backplane_url`` is empty (chassis-only deployment),
    the helper returns the bare ``Bearer`` challenge with no
    ``resource_metadata`` parameter — a degraded but spec-legal
    response that doesn't leak a malformed URL.
    """
    settings = settings or get_settings()
    base = settings.backplane_url.rstrip("/")
    if not base:
        return "Bearer"
    metadata_url = f"{base}/.well-known/oauth-protected-resource"
    return f'Bearer resource_metadata="{metadata_url}"'


async def verify_mcp_jwt(
    authorization: str | None = Header(default=None),
) -> Operator:
    """FastAPI dependency: validate the Bearer token for the MCP audience.

    Reuses :func:`~meho_backplane.auth.jwt.verify_jwt_for_audience` with
    :func:`mcp_resource_uri` as the ``expected_audience``. The chassis
    chain raises :class:`fastapi.HTTPException` with status 401 on
    every failure mode (missing header, malformed Authorization, JWKS
    unreachable, signature mismatch, expired token, audience mismatch,
    issuer mismatch, malformed JWT, malformed tenant claims). On any of
    those, this wrapper re-raises the same exception with the
    RFC 9728 §5.1 ``WWW-Authenticate`` header attached so MCP clients
    can discover the resource-metadata URL and walk the OAuth-RS
    discovery flow.

    A 401 is preserved verbatim — same detail token, same status code.
    Non-401 status codes from the chassis chain (none today, reserved
    for future cases) propagate without the header injection.
    """
    settings = get_settings()
    expected_audience = mcp_resource_uri(settings)
    try:
        return await verify_jwt_for_audience(
            authorization,
            expected_audience=expected_audience,
        )
    except HTTPException as exc:
        if exc.status_code != 401:
            raise
        headers = dict(exc.headers or {})
        headers["WWW-Authenticate"] = www_authenticate_header(settings)
        raise HTTPException(
            status_code=exc.status_code,
            detail=exc.detail,
            headers=headers,
        ) from exc


async def verify_mcp_jwt_and_bind(
    operator: Operator = Depends(verify_mcp_jwt),
) -> Operator:
    """Validate the Bearer token *and* bind operator identity into contextvars.

    Mirrors the chassis :func:`~meho_backplane.middleware.verify_jwt_and_bind`
    pattern: every authenticated MCP route handler declares
    ``Depends(verify_mcp_jwt_and_bind)`` so the
    ``operator_sub`` and ``tenant_id`` slots end up in structlog's
    contextvars before the handler runs. The chassis
    :class:`~meho_backplane.audit.AuditMiddleware` reads ``operator_sub``
    to decide whether to write an audit row; without this binding the
    middleware would skip the row and the operator-action trail would
    have an `/mcp` shaped hole. G0.5-T5 (#250) layers MCP-specific
    audit semantics on top of this implicit chassis trail.

    ``tenant_id`` is bound as ``str(operator.tenant_id)`` for the same
    JSON-renderer reason the chassis wrapper documents — see
    :func:`~meho_backplane.middleware.verify_jwt_and_bind`.
    """
    structlog.contextvars.bind_contextvars(
        operator_sub=operator.sub,
        tenant_id=str(operator.tenant_id),
    )
    return operator
