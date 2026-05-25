# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""RFC 8693 token-exchange delegation + ``client_credentials`` for autonomous runs.

This module is the G11.2-T2 (#816) implementation of MEHO's two
agent-identity grant flows:

1. **Delegation (user-triggered run)** — a user-initiated agent run
   exchanges the user's access token for an agent-acting-on-behalf
   token via :func:`exchange_for_delegation`. The resulting token
   carries ``sub``=user, ``act``=agent so the audit row records both
   the initiating human and the acting agent.

2. **Autonomous run (client credentials)** — a scheduled / cron run
   with no human initiator authenticates directly as the agent
   principal via :func:`get_client_credentials_token`. The agent is
   both subject and actor; ``actor_sub`` is NULL on the audit row.

Both flows call Keycloak's standard token endpoint
(``{issuer}/protocol/openid-connect/token``). RFC 8693 is GA in
Keycloak 26.2; the required ``standard-token-exchange`` switch on
the agent client must be enabled in the Keycloak admin console — see
``docs/cross-repo/keycloak-agent-client.md`` for the setup recipe.

Error contract
--------------

Both functions raise :class:`TokenExchangeError` on every failure
mode — network unreachable, Keycloak refuses the exchange (``may_act``
not permitted, client secret invalid, ``invalid_target``), or the
response shape is unexpected. The caller converts the error into
the appropriate surface response (401/403 for HTTP routes; a
structured agent-loop abort for the invocation path).

Callers must **not** cache the returned tokens beyond their ``expires_in``
window; Keycloak issues short-lived access tokens and reissuing on
every agent-run start is the correct pattern. Long-lived agent loops
that need token refreshes are a future concern (G11.3 scheduler);
the current surface is per-invocation.

Security posture
----------------

* Delegation not impersonation. The exchange uses **actor_token**
  (the agent's ``client_credentials`` token) + **subject_token**
  (the user's token) to produce a *delegation* token, not an
  *impersonation* token. The distinction matters: delegation
  preserves the audit chain (``sub``=user, ``act``=agent); impersonation
  would collapse it (``sub``=user, no ``act``). See RFC 8693 §1.1.
* The agent's ``client_secret`` is never logged. Structlog events
  in this module emit only the ``client_id`` and the Keycloak error
  code; secrets stay out of the log pipeline.
* ``requested_token_type`` is pinned to
  ``urn:ietf:params:oauth:token-type:access_token`` — the caller
  wants a standard access token, not a refresh token or an ID token.
* HTTP timeout is shared with the JWKS-fetch timeout
  (:data:`~meho_backplane.auth.jwt._HTTP_TIMEOUT_SECONDS`): 5 s.
  A hung Keycloak should fail-closed quickly rather than starve the
  invocation path.
"""

from __future__ import annotations

__all__ = [
    "TokenExchangeError",
    "TokenExchangeExchangeRefusedError",
    "exchange_for_delegation",
    "get_client_credentials_token",
]

import httpx
import structlog

#: Keycloak token endpoint path appended to the issuer URL.
_TOKEN_PATH: str = "/protocol/openid-connect/token"

#: RFC 8693 grant type for token exchange.
_GRANT_TYPE_TOKEN_EXCHANGE: str = "urn:ietf:params:oauth:grant-type:token-exchange"

#: RFC 8693 token type for standard OAuth access tokens.
_TOKEN_TYPE_ACCESS: str = "urn:ietf:params:oauth:token-type:access_token"

#: HTTP timeout in seconds — mirrors ``auth.jwt._HTTP_TIMEOUT_SECONDS``.
_HTTP_TIMEOUT_SECONDS: float = 5.0


class TokenExchangeError(Exception):
    """Raised when a token-exchange or client-credentials call fails.

    Carries a structured ``code`` (the Keycloak error string or a
    synthetic ``network_error`` / ``unexpected_response`` code) and a
    human-readable ``detail`` for logging. The ``code`` is safe to
    surface in structured logs; the ``detail`` may contain Keycloak's
    ``error_description`` which is operator-controlled and is logged
    only at ``warning`` level (never in 401/403 response bodies).
    """

    def __init__(self, code: str, detail: str) -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}")


class TokenExchangeExchangeRefusedError(TokenExchangeError):
    """Keycloak refused the exchange — ``may_act`` not permitted or invalid target.

    Subclass of :class:`TokenExchangeError` so callers that only want
    to distinguish "refused" from "broken" can catch this specifically.
    The HTTP surfaces map this to 403 (the exchange was rejected on
    policy grounds, not a network failure).
    """


async def exchange_for_delegation(
    *,
    issuer_url: str,
    subject_token: str,
    agent_client_id: str,
    agent_client_secret: str,
    audience: str,
) -> str:
    """Exchange a user token for a delegation token via RFC 8693.

    Sends a token-exchange request to Keycloak's token endpoint using
    the agent's ``client_credentials`` as the ``actor_token``, so the
    resulting token carries ``sub``=user and ``act``=agent. The returned
    string is the raw access token; the caller passes it as a Bearer
    header on the agent-run request (or directly to
    :func:`~meho_backplane.auth.jwt.verify_jwt_for_audience` to resolve
    the delegated :class:`~meho_backplane.auth.operator.Operator`).

    Parameters
    ----------
    issuer_url:
        Keycloak realm issuer URL (``KEYCLOAK_ISSUER_URL``). The token
        endpoint is derived by appending
        ``/protocol/openid-connect/token``.
    subject_token:
        The initiating user's current access token (the raw Bearer
        string). Passed as ``subject_token`` in the exchange request.
    agent_client_id:
        The Keycloak ``client_id`` of the agent client (the acting
        party). Must have ``standard-token-exchange`` enabled in the
        Keycloak admin console and the ``may_act`` audience scope
        granted. Passed as Basic-auth username and as
        ``actor_token``'s source principal.
    agent_client_secret:
        The agent client's secret. Passed as Basic-auth password.
        Never logged.
    audience:
        The ``aud`` the requested token must carry. Should match
        ``KEYCLOAK_AUDIENCE`` (the backplane's client id) so the
        resulting token is accepted by ``verify_jwt``.

    Returns
    -------
    str
        The delegated access token (``access_token`` from Keycloak's
        response).

    Raises
    ------
    TokenExchangeExchangeRefusedError
        Keycloak returned an ``invalid_target``, ``unauthorized_client``,
        or ``access_denied`` error — ``may_act`` is not permitted or the
        agent client is misconfigured.
    TokenExchangeError
        Any other failure: network unreachable, unexpected status, or
        missing ``access_token`` in a 200 response.
    """
    log = structlog.get_logger(__name__)
    token_url = _token_endpoint(issuer_url)

    # Step 1: obtain the agent's own token via client_credentials so we
    # have an actor_token to pass to the exchange request.
    actor_token = await _fetch_client_credentials_token(
        token_url=token_url,
        client_id=agent_client_id,
        client_secret=agent_client_secret,
        audience=audience,
        log=log,
    )

    # Step 2: the delegation exchange.
    form_data = {
        "grant_type": _GRANT_TYPE_TOKEN_EXCHANGE,
        "subject_token": subject_token,
        "subject_token_type": _TOKEN_TYPE_ACCESS,
        "actor_token": actor_token,
        "actor_token_type": _TOKEN_TYPE_ACCESS,
        "requested_token_type": _TOKEN_TYPE_ACCESS,
        "audience": audience,
    }
    try:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT_SECONDS) as client:
            response = await client.post(
                token_url,
                data=form_data,
                auth=(agent_client_id, agent_client_secret),
            )
    except httpx.HTTPError as exc:
        log.warning(
            "token_exchange_network_error",
            client_id=agent_client_id,
            error=type(exc).__name__,
        )
        raise TokenExchangeError(
            "network_error",
            f"token-exchange request to {token_url} failed: {type(exc).__name__}",
        ) from exc

    return _extract_access_token(
        response=response,
        client_id=agent_client_id,
        operation="delegation_exchange",
        log=log,
    )


async def get_client_credentials_token(
    *,
    issuer_url: str,
    agent_client_id: str,
    agent_client_secret: str,
    audience: str,
) -> str:
    """Obtain an access token for an autonomous agent run.

    Uses ``client_credentials`` grant — no user token is present
    (e.g. a scheduled / cron run). The resulting token has
    ``sub``=agent and no ``act`` claim; ``actor_sub`` on the resolved
    :class:`~meho_backplane.auth.operator.Operator` will be ``None``.
    The audit row records the agent as both subject and actor.

    Parameters
    ----------
    issuer_url:
        Keycloak realm issuer URL (``KEYCLOAK_ISSUER_URL``).
    agent_client_id:
        The Keycloak ``client_id`` of the agent client. Must have
        ``Service accounts enabled`` in the Keycloak admin console
        and the required ``audience`` in the issued token's ``aud``
        claim (add an audience mapper on the client).
    agent_client_secret:
        The agent client's secret. Never logged.
    audience:
        The ``aud`` the requested token must carry.

    Returns
    -------
    str
        The raw access token.

    Raises
    ------
    TokenExchangeError
        Network failure, Keycloak rejects the client credentials, or
        the response is missing ``access_token``.
    """
    log = structlog.get_logger(__name__)
    token_url = _token_endpoint(issuer_url)
    return await _fetch_client_credentials_token(
        token_url=token_url,
        client_id=agent_client_id,
        client_secret=agent_client_secret,
        audience=audience,
        log=log,
    )


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _token_endpoint(issuer_url: str) -> str:
    """Derive the Keycloak token endpoint URL from the issuer URL."""
    return str(issuer_url).rstrip("/") + _TOKEN_PATH


async def _fetch_client_credentials_token(
    *,
    token_url: str,
    client_id: str,
    client_secret: str,
    audience: str,
    log: structlog.stdlib.BoundLogger,
) -> str:
    """Fetch a ``client_credentials`` token from Keycloak.

    Shared by both :func:`exchange_for_delegation` (which calls it
    to obtain the ``actor_token`` before the exchange) and by
    :func:`get_client_credentials_token` (direct autonomous flow).
    """
    form_data: dict[str, str] = {
        "grant_type": "client_credentials",
        "audience": audience,
    }
    try:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT_SECONDS) as client:
            response = await client.post(
                token_url,
                data=form_data,
                auth=(client_id, client_secret),
            )
    except httpx.HTTPError as exc:
        log.warning(
            "client_credentials_network_error",
            client_id=client_id,
            error=type(exc).__name__,
        )
        raise TokenExchangeError(
            "network_error",
            f"client_credentials request to {token_url} failed: {type(exc).__name__}",
        ) from exc

    return _extract_access_token(
        response=response,
        client_id=client_id,
        operation="client_credentials",
        log=log,
    )


_REFUSED_ERRORS: frozenset[str] = frozenset(
    {"invalid_target", "unauthorized_client", "access_denied"}
)


def _extract_access_token(
    *,
    response: httpx.Response,
    client_id: str,
    operation: str,
    log: structlog.stdlib.BoundLogger,
) -> str:
    """Parse the token endpoint response and return the ``access_token``.

    Raises :class:`TokenExchangeExchangeRefusedError` for policy-denied
    responses (``may_act`` not granted, ``invalid_target``) and
    :class:`TokenExchangeError` for every other failure, so callers can
    distinguish "Keycloak policy refused" from "something is broken".
    """
    if response.status_code != 200:
        try:
            body = response.json()
            error_code = body.get("error", "unknown_error")
            error_desc = body.get("error_description", "")
        except Exception:
            error_code = "unexpected_response"
            error_desc = f"status={response.status_code}"

        log.warning(
            "token_endpoint_error",
            client_id=client_id,
            operation=operation,
            error=error_code,
        )
        if error_code in _REFUSED_ERRORS:
            raise TokenExchangeExchangeRefusedError(
                error_code,
                f"{operation} refused for client {client_id!r}: {error_desc}",
            )
        raise TokenExchangeError(
            error_code,
            f"{operation} failed for client {client_id!r}: {error_desc}",
        )

    try:
        body = response.json()
        access_token = body["access_token"]
        if not isinstance(access_token, str) or not access_token:
            raise KeyError("access_token empty or not a string")
    except (KeyError, ValueError) as exc:
        log.warning(
            "token_endpoint_missing_access_token",
            client_id=client_id,
            operation=operation,
        )
        raise TokenExchangeError(
            "missing_access_token",
            f"{operation}: Keycloak response missing access_token",
        ) from exc

    return access_token
