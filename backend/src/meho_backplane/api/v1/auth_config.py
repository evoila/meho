# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``GET /api/v1/auth-config`` — public OAuth discovery for the CLI.

The CLI's device-code flow (``cli/internal/cmd/login.go``) needs the
Keycloak realm issuer URL and the audience claim before it can run
``cfg.DeviceAuth(flowCtx)``. Operators should only have to know one
URL — the backplane's — so the CLI fetches both values from this
endpoint at the start of ``meho login``.

Unauthenticated by design:

* This is the OAuth metadata the CLI needs **before** it can auth.
* The values are public OAuth metadata — the issuer URL appears in
  every JWT's ``iss`` claim and the audience appears in every JWT's
  ``aud`` claim. They are not secrets.
* Adding authentication here would create a chicken-and-egg loop with
  ``meho login``.

The values are sourced from the running ``Settings`` singleton — the
same ones :mod:`~meho_backplane.auth.jwt` uses to validate inbound
JWTs. The endpoint cannot drift from the validation surface because
both read from the same env-var contract documented in
:mod:`meho_backplane.settings`.

Response shape locked by the CLI's discovery parser in
``cli/internal/cmd/login.go`` (search for ``keycloak_issuer`` /
``audience`` in that file's ``fetchBackplaneAuthConfig``). Field
renames here are wire-compat breaks for the CLI.

Closes the gap documented in closed Task #44's coordination note
("add the endpoint to G2.2-T3's ``/api/v1/health`` Task body
retroactively as a follow-up issue") that was never filed; the CLI
shipped with the ``--issuer`` / ``--client-id`` fallback flags while
this surface waited.
"""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict

from meho_backplane.settings import get_settings

__all__ = ["router"]


class AuthConfigResponse(BaseModel):
    """OAuth discovery surface returned to ``meho login``.

    Field names match the CLI's expected JSON keys (``keycloak_issuer``
    and ``audience``); renaming either field is a wire-compat break.
    """

    model_config = ConfigDict(frozen=True)

    keycloak_issuer: str
    audience: str


router = APIRouter(prefix="/api/v1", tags=["auth"])


@router.get("/auth-config", response_model=AuthConfigResponse)
async def auth_config() -> AuthConfigResponse:
    """Return Keycloak issuer + audience for the CLI's device-code flow.

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
    )
