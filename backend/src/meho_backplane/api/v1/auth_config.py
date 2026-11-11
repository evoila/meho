# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``GET /api/v1/auth-config`` — public OAuth discovery for the CLI.

The CLI's device-code flow (``cli/internal/cmd/login.go``) needs the
Keycloak realm issuer URL, the backplane audience, and the public
device-code ``client_id`` before it can run ``cfg.DeviceAuth(flowCtx)``.
Operators should only have to know one URL — the backplane's — so the
CLI fetches all three values from this endpoint at the start of
``meho login``.

Unauthenticated by design:

* This is the OAuth metadata the CLI needs **before** it can auth.
* The values are public OAuth metadata — the issuer URL appears in
  every JWT's ``iss`` claim, the audience appears in every JWT's
  ``aud`` claim, and the CLI ``client_id`` is a public OAuth identifier
  (no client secret involved). They are not secrets.
* Adding authentication here would create a chicken-and-egg loop with
  ``meho login``.

The values are sourced from the running ``Settings`` singleton — the
same ones :mod:`~meho_backplane.auth.jwt` uses to validate inbound
JWTs. The endpoint cannot drift from the validation surface because
both read from the same env-var contract documented in
:mod:`meho_backplane.settings`.

Response shape locked by the CLI's discovery parser in
``cli/internal/cmd/login.go`` (search for ``keycloak_issuer`` /
``audience`` / ``cli_client_id`` in that file's
``fetchBackplaneAuthConfig``). Field renames here are wire-compat
breaks for the CLI.

The fields:

* ``keycloak_issuer`` — the realm issuer URL.
* ``audience`` — the JWT ``aud`` value the backplane validates inbound
  tokens against. This is the **confidential** resource-server
  identifier (e.g. ``meho-backplane``); device-code clients cannot use
  it as ``client_id`` because it carries a client secret.
* ``cli_client_id`` — the **public** device-code ``client_id`` the CLI
  uses to drive the RFC 8628 device authorization grant. Sourced from
  :attr:`~meho_backplane.settings.Settings.keycloak_cli_client_id`
  (chart-wired via ``config.keycloakCliClientId``). When unset (empty
  string), the CLI surfaces an actionable error naming the public-client
  requirement rather than silently using ``audience``.

* ``cli_approve_client_id`` — the **public** client the CLI uses for the
  fresh browser sign-in of ``meho approvals approve`` (#5515). Sourced
  from :attr:`~meho_backplane.settings.Settings.keycloak_cli_approve_client_id`.
  Only a token made for this client can approve on the command line or
  REST. Empty means the backplane accepts no token approve.

* ``mcp_clients`` -- the **public** login client of each AI tool kind
  (``{"claude-code": "meho-mcp-code", "claude-desktop":
  "meho-mcp-desktop"}``). Listed only when the ``MCP_CLIENT_PER_TOOL_KIND``
  switch is on; empty otherwise. The Claude Code plugin and the Claude
  Desktop extension read it at start. A tool whose kind is listed signs in
  with that client and sends the ``Meho-Tool`` header, so it keeps a saved
  login of its own. A tool whose kind is not listed keeps the shared
  ``meho-mcp`` client.

The ``cli_client_id`` field was added by G0.9.1-T9 after the 2026-05-21
RDC dogfood (Signal #16) showed that the v0.3.1 endpoint (issuer +
audience only) silently broke ``meho login`` on its documented happy
path — the CLI was mapping ``audience`` to ``client_id``, but Keycloak
rejects device-code initiation against a confidential resource-server
client with ``401 unauthorized_client``. Closes the gap documented in
closed Task #44's coordination note that was never filed.
"""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict, Field

from meho_backplane.settings import get_settings

__all__ = ["MCP_TOOL_CLIENTS", "router"]

#: The login client of each AI tool kind (``MCP_CLIENT_PER_TOOL_KIND``). The
#: realm file of the built-in Keycloak and ``meho admin keycloak
#: bootstrap-clients`` create these clients with the same settings as
#: ``meho-mcp``. The keys are the values the tools send in ``Meho-Tool``.
MCP_TOOL_CLIENTS: dict[str, str] = {
    "claude-code": "meho-mcp-code",
    "claude-desktop": "meho-mcp-desktop",
}


class AuthConfigResponse(BaseModel):
    """OAuth discovery surface returned to ``meho login``.

    Field names match the CLI's expected JSON keys (``keycloak_issuer``,
    ``audience``, ``cli_client_id``); renaming any field is a wire-compat
    break for the CLI's discovery parser. ``cli_client_id`` defaults to
    the empty string on the response when the backplane has not been
    wired (``KEYCLOAK_CLI_CLIENT_ID`` unset); the CLI distinguishes
    empty-string from absent-key and emits the same actionable
    public-client error in both cases.
    """

    model_config = ConfigDict(frozen=True)

    keycloak_issuer: str
    audience: str
    cli_client_id: str
    #: The public client of the approval sign-in (#5515). A client that
    #: does not know this field ignores it.
    cli_approve_client_id: str = ""
    #: The login client of each AI tool kind. Empty while the
    #: ``MCP_CLIENT_PER_TOOL_KIND`` switch is off.
    mcp_clients: dict[str, str] = Field(default_factory=dict)


router = APIRouter(prefix="/api/v1", tags=["auth"])


@router.get("/auth-config", response_model=AuthConfigResponse)
async def auth_config() -> AuthConfigResponse:
    """Return Keycloak issuer, audience, and the public client ids.

    Reads :func:`~meho_backplane.settings.get_settings` so the response
    cannot drift from the values :mod:`~meho_backplane.auth.jwt` uses
    when it validates inbound JWTs. The issuer URL is returned exactly
    as configured, trailing slash included. Keycloak publishes its
    issuer without one; providers such as authentik publish theirs
    with one, and a client that compares the value against the
    discovery document (RFC 8414 §3.3) needs the published form. The
    CLI strips the slash itself before it builds
    ``<issuer>/.well-known/openid-configuration``.
    """
    settings = get_settings()
    return AuthConfigResponse(
        keycloak_issuer=str(settings.keycloak_issuer_url),
        audience=settings.keycloak_audience,
        cli_client_id=settings.keycloak_cli_client_id,
        cli_approve_client_id=settings.keycloak_cli_approve_client_id,
        mcp_clients=dict(MCP_TOOL_CLIENTS) if settings.mcp_client_per_tool_kind else {},
    )
