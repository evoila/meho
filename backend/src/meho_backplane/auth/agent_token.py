# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Agent ``client_credentials`` token acquisition (G11.2-T2 #816).

Autonomous (cron / no-human) agent runs authenticate as the agent
principal itself via the OAuth ``client_credentials`` grant — which
Keycloak fully supports, unlike RFC 8693 delegation token exchange
(keycloak#38279). The returned token's ``sub`` is the agent's service
account, so an autonomous run executes with ``operator_sub``=agent and no
separate actor (``actor_sub`` stays ``None``). The human-initiated path that
records *both* subjects is :mod:`meho_backplane.auth.delegation`.

This is the agent-side mirror of the admin-side ``client_credentials`` flow
in :class:`~meho_backplane.auth.keycloak_admin.KeycloakAdminClient`: a thin
async ``httpx`` call, structured errors, and the client secret never logged.
The autonomous trigger that supplies the credentials and consumes the token
is G11.3's scope; this module is the authentication primitive it calls.

It is now the shared grant primitive rather than an agent-only one: the
check-runner's background-dispatch identity
(:mod:`meho_backplane.auth.runner_identity`, #2642) mints its service
principal's token through the same call. The ``AgentToken*`` names are kept
for the existing call sites; :func:`get_client_credentials_grant` is the
principal-agnostic entry point, and returns the declared lifetime so a
caller that runs on a tick can cache instead of re-minting per use.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx
import structlog

__all__ = [
    "AgentTokenError",
    "ClientCredentialsGrant",
    "get_client_credentials_grant",
    "get_client_credentials_token",
]

_TOKEN_HTTP_TIMEOUT_SECONDS: float = 10.0

#: Lifetime assumed when the token endpoint omits ``expires_in``. Keycloak
#: always sends it, but the field is optional in RFC 6749 §5.1, so a caller
#: that caches on it needs a floor rather than a ``KeyError``. Deliberately
#: short: re-minting early is cheap, using an expired token is not.
_DEFAULT_EXPIRES_IN_SECONDS: int = 60


@dataclass(frozen=True)
class ClientCredentialsGrant:
    """A ``client_credentials`` token plus the lifetime the IdP declared.

    ``expires_in`` is what lets a caller cache the token instead of
    re-minting one per use — the check-runner's background-dispatch identity
    (:mod:`meho_backplane.auth.runner_identity`) would otherwise hit Keycloak
    once per sensor evaluation. The token value is never logged.
    """

    access_token: str
    expires_in: int


class AgentTokenError(Exception):
    """Raised when the agent ``client_credentials`` grant fails.

    Carries a stable ``code`` so callers can branch on the failure mode
    (``network_error`` / ``http_<status>`` / ``missing_access_token``)
    without parsing the message. The agent ``client_secret`` is never
    included in the message or any structlog event.
    """

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


async def get_client_credentials_token(
    *,
    issuer_url: str,
    client_id: str,
    client_secret: str,
    audience: str | None = None,
) -> str:
    """Return just the access token — see :func:`get_client_credentials_grant`."""
    grant = await get_client_credentials_grant(
        issuer_url=issuer_url,
        client_id=client_id,
        client_secret=client_secret,
        audience=audience,
    )
    return grant.access_token


async def get_client_credentials_grant(
    *,
    issuer_url: str,
    client_id: str,
    client_secret: str,
    audience: str | None = None,
) -> ClientCredentialsGrant:
    """Obtain an access token for *client_id* via the ``client_credentials`` grant.

    POSTs to the realm token endpoint derived from *issuer_url*
    (``{issuer}/protocol/openid-connect/token``) and returns the
    ``access_token`` plus its declared lifetime. The token's ``sub`` is the
    client's service account, so a run authenticated with it is attributed
    to that principal as subject — there is no separate actor.

    Args:
        issuer_url: The Keycloak realm issuer URL (the token endpoint is
            derived from it).
        client_id: The OAuth client id (``agent:<name>`` for an agent
            principal, the configured check-runner client for background
            dispatch).
        client_secret: The client's secret. Never logged.
        audience: Optional ``aud`` to request (RFC 8707), when the token
            must be bound to a specific resource.

    Raises:
        AgentTokenError: on a network failure (``code="network_error"``), a
            non-2xx response (``code="http_<status>"`` — e.g. a Keycloak
            ``invalid_client`` 401), or a 200 response carrying no
            ``access_token`` (``code="missing_access_token"``).
    """
    log = structlog.get_logger(__name__)
    token_url = f"{issuer_url.rstrip('/')}/protocol/openid-connect/token"
    data: dict[str, str] = {
        "grant_type": "client_credentials",
        "client_id": client_id,
        "client_secret": client_secret,
    }
    if audience is not None:
        data["audience"] = audience
    try:
        async with httpx.AsyncClient(timeout=_TOKEN_HTTP_TIMEOUT_SECONDS) as http:
            resp = await http.post(token_url, data=data)
    except httpx.HTTPError as exc:
        log.warning("agent_token_unreachable", client_id=client_id, error=type(exc).__name__)
        raise AgentTokenError(
            "network_error",
            f"agent token endpoint unreachable: {type(exc).__name__}",
        ) from exc
    if resp.status_code not in (200, 201):
        log.warning("agent_token_failed", client_id=client_id, status=resp.status_code)
        raise AgentTokenError(
            f"http_{resp.status_code}",
            f"agent client_credentials grant failed: HTTP {resp.status_code}",
        )
    try:
        body: Any = resp.json()
    except ValueError as exc:
        # A 2xx with a non-JSON body is malformed — surface it as a typed
        # error rather than leaking a raw JSONDecodeError to the caller.
        raise AgentTokenError(
            "missing_access_token",
            "agent client_credentials grant returned a non-JSON body",
        ) from exc
    token = body.get("access_token") if isinstance(body, dict) else None
    if not isinstance(token, str) or not token:
        raise AgentTokenError(
            "missing_access_token",
            "agent client_credentials grant returned no access_token",
        )
    raw_expires_in = body.get("expires_in") if isinstance(body, dict) else None
    expires_in = (
        raw_expires_in
        if isinstance(raw_expires_in, int) and not isinstance(raw_expires_in, bool)
        else _DEFAULT_EXPIRES_IN_SECONDS
    )
    return ClientCredentialsGrant(access_token=token, expires_in=max(expires_in, 0))
