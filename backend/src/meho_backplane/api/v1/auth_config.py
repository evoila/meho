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

The three fields:

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
from pydantic import BaseModel, ConfigDict

from meho_backplane.settings import get_settings

__all__ = ["router"]


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


router = APIRouter(prefix="/api/v1", tags=["auth"])


@router.get("/auth-config", response_model=AuthConfigResponse)
async def auth_config() -> AuthConfigResponse:
    """Return Keycloak issuer, audience, and CLI client_id for device-code flow.

    Reads :func:`~meho_backplane.settings.get_settings` so the response
    cannot drift from the values :mod:`~meho_backplane.auth.jwt` uses
    when it validates inbound JWTs. The issuer URL is trailing-slash-
    normalised here — Keycloak's discovery document advertises the
    canonical form without a trailing slash and the CLI's own URL
    construction (``<issuer>/.well-known/openid-configuration``) is
    cleaner against the normalised form.
    """
    settings = get_settings()
    return AuthConfigResponse(
        keycloak_issuer=str(settings.keycloak_issuer_url).rstrip("/"),
        audience=settings.keycloak_audience,
        cli_client_id=settings.keycloak_cli_client_id,
    )
