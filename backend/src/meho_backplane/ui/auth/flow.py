# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""OAuth 2.1 Authorization Code + PKCE client primitives for the BFF.

Initiative #337 (G10.0 Frontend chassis), Task #865 (T4). This module
hosts the OAuth-client plumbing the ``/ui/auth/*`` route handlers in
:mod:`meho_backplane.ui.auth.routes` call into:

* :func:`build_authorization_request` -- mints a per-login
  ``(state, code_verifier)`` pair, registers the verifier in the
  module-level :class:`PKCEVerifierStore` (see
  :mod:`meho_backplane.ui.auth.verifier_store`) keyed on ``state``,
  and asks authlib's :class:`AsyncOAuth2Client` to build the Keycloak
  authorization URL (carries ``code_challenge`` +
  ``code_challenge_method=S256`` + ``resource=<backplane_url>/api`` per
  RFC 8707).
* :func:`exchange_code_for_tokens` -- callback-side hop. Pops the
  ``code_verifier`` from the store (one-time use; absence on a second
  callback request with the same ``state`` deliberately fails-closed),
  then exchanges ``authorization_code`` + ``code_verifier`` against
  Keycloak's token endpoint and returns the OAuth2-token dict.
* :func:`resolve_oidc_endpoints` -- discovery-doc fetch that the
  routes call at login + callback + logout time to obtain the
  ``authorization_endpoint`` / ``token_endpoint`` /
  ``end_session_endpoint`` URLs. Backed by a TTL'd in-process cache
  identical in shape to :mod:`meho_backplane.auth.jwt`'s JWKS cache.

Library choice (per Task #865 acceptance + ADR 0004): authlib's
:class:`authlib.integrations.httpx_client.AsyncOAuth2Client`. authlib
is already a backplane dependency (chassis JWT verification chain in
:mod:`meho_backplane.auth.jwt`); its OAuth2 client primitive ships
PKCE, state, and the RFC 8707 ``resource`` parameter as kwargs to the
two methods above. We deliberately do **not** reach for
:mod:`authlib.integrations.starlette_client` -- that integration owns
its own session/cookie machinery, which the BFF cannot delegate
(decision #11 keeps tokens server-side in
:mod:`~meho_backplane.ui.auth.session_store`).

Why PKCE on a confidential client
---------------------------------

OAuth 2.1 mandates PKCE for *every* authorization-code flow, not only
public clients. The BCP rationale (RFC 9700 §2.1.1 /
draft-ietf-oauth-browser-based-apps §6.1) is that PKCE is the only
defence against authorization-code interception when an intermediate
hop on the redirect path is not under the relying-party's control.
``code_challenge_method=S256`` is the only method accepted; plain is
not negotiable.

Security discipline (#865 is auth)
----------------------------------

* The client secret resolved from
  :attr:`Settings.ui_keycloak_client_secret` never enters a log
  line, error body, or structlog context. The only seam that touches
  it is :func:`_build_oauth_client` (writes it onto the
  :class:`AsyncOAuth2Client` instance, which sends it as
  ``client_secret_basic`` on the token endpoint Authorization header).

* ``state`` is the CSRF guard on the callback. authlib's
  :meth:`fetch_token` re-checks ``state`` when called with the
  callback URL; this module's :func:`exchange_code_for_tokens`
  delegates that check.

* ``code_verifier`` is removed from the store as soon as the
  callback consumes it (single-use semantics). A second callback
  with the same ``state`` finds no verifier and is rejected.

References
----------

* RFC 6749 §4.1 (Authorization Code grant):
  https://www.rfc-editor.org/rfc/rfc6749
* RFC 7636 (PKCE):
  https://www.rfc-editor.org/rfc/rfc7636
* RFC 8707 (Resource Indicators):
  https://www.rfc-editor.org/rfc/rfc8707
* RFC 9700 (OAuth Security BCP, Jan 2025):
  https://datatracker.ietf.org/doc/rfc9700/
* authlib AsyncOAuth2Client:
  https://docs.authlib.org/en/latest/client/httpx.html
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any, Final

import httpx
import structlog
from authlib.common.security import generate_token
from authlib.integrations.httpx_client import AsyncOAuth2Client

from meho_backplane.settings import Settings, get_settings
from meho_backplane.ui.auth.verifier_store import (
    AUTHORIZATION_FLOW_TTL_SECONDS,
    PendingFlow,
    PKCEVerifierStore,
    get_verifier_store,
    reset_verifier_store_for_testing,
)

__all__ = [
    "AUTHORIZATION_FLOW_TTL_SECONDS",
    "MISSING_CLIENT_SECRET_DETAIL",
    "OAuthFlowConfigurationError",
    "OAuthFlowError",
    "OIDCEndpoints",
    "PKCEVerifierStore",
    "TokenExchangeResult",
    "build_authorization_request",
    "clear_discovery_cache",
    "exchange_code_for_tokens",
    "get_verifier_store",
    "reset_verifier_store_for_testing",
    "resolve_oidc_endpoints",
]


#: HTTP timeout for the OIDC discovery hit. Mirrors
#: :mod:`meho_backplane.auth.jwt`'s ``_HTTP_TIMEOUT_SECONDS`` -- a hung
#: Keycloak should fail-closed quickly rather than starving request
#: capacity. The same value applies on the token-endpoint POST that
#: :class:`AsyncOAuth2Client` issues from :func:`exchange_code_for_tokens`.
_HTTP_TIMEOUT_SECONDS: float = 5.0

#: Detail token surfaced when :attr:`Settings.ui_keycloak_client_secret`
#: is unset and the flow would otherwise silently send an empty
#: ``client_secret`` on the token-endpoint POST (which Keycloak rejects
#: as ``invalid_client`` -- a confusing error if the operator does not
#: realise the secret was never rendered into the pod environment).
MISSING_CLIENT_SECRET_DETAIL: Final[str] = (
    "ui_oauth_not_configured: UI_KEYCLOAK_CLIENT_ID / "
    "UI_KEYCLOAK_CLIENT_SECRET are unset. Render the confidential "
    "client credentials from Vault per "
    "docs/cross-repo/keycloak-web-client.md before serving /ui/auth/*."
)


class OAuthFlowError(Exception):
    """Base class for BFF OAuth-flow failures.

    Catch this when a single error-response shape is acceptable;
    subclasses carry the specific intent.
    """


class OAuthFlowConfigurationError(OAuthFlowError):
    """The BFF OAuth client is not configured.

    Raised at the top of :func:`build_authorization_request` and
    :func:`exchange_code_for_tokens` when
    :attr:`Settings.ui_keycloak_client_id` or
    :attr:`Settings.ui_keycloak_client_secret` is empty. The route
    handler maps this into an operator-facing 503 with the
    :data:`MISSING_CLIENT_SECRET_DETAIL` remediation.
    """


@dataclass(frozen=True)
class OIDCEndpoints:
    """Subset of the OIDC discovery document the BFF consumes.

    Decoupled from :class:`Settings` because the discovery doc is the
    realm's contract on which URLs the BFF posts to -- the operator
    cannot override these without breaking the IdP round-trip. Resolved
    at runtime by :func:`resolve_oidc_endpoints` and cached on a TTL.
    """

    issuer: str
    authorization_endpoint: str
    token_endpoint: str
    end_session_endpoint: str | None


@dataclass(frozen=True)
class TokenExchangeResult:
    """Outcome of a successful authorization-code exchange.

    Frozen because callers stash the access + refresh tokens in the
    encrypted session row and pass nothing else along; mutability is
    a footgun (a downstream caller mutating the access token in-place
    would silently desynchronise from the ciphertext in storage).
    """

    access_token: str
    refresh_token: str
    expires_in: int
    return_to: str


# ---------------------------------------------------------------------------
# OIDC discovery
# ---------------------------------------------------------------------------


#: Module-level discovery cache. Mirrors the shape of
#: :mod:`meho_backplane.auth.jwt`'s JWKS cache so the two surfaces
#: stay grep-compatible. The TTL borrows
#: :attr:`Settings.keycloak_jwks_cache_ttl_seconds` -- one operator
#: knob governs "how often do we hit Keycloak's metadata".
_discovery_cache: OIDCEndpoints | None = None
_discovery_fetched_at: float = 0.0
_discovery_lock: asyncio.Lock = asyncio.Lock()


def clear_discovery_cache() -> None:
    """Invalidate the discovery cache. Test-only -- never call from production."""
    global _discovery_cache, _discovery_fetched_at
    _discovery_cache = None
    _discovery_fetched_at = 0.0


async def _fetch_discovery_doc(settings: Settings) -> OIDCEndpoints:
    """Fetch ``/.well-known/openid-configuration`` and project the BFF subset.

    Network failures (DNS, TLS, read timeout, non-2xx) propagate as
    :class:`httpx.HTTPError` -- the same error class
    :mod:`meho_backplane.auth.jwt` raises on its own discovery hit, so
    the operator-facing 502 mapping in the routes module is one shape.
    """
    issuer = str(settings.keycloak_issuer_url).rstrip("/")
    discovery_url = f"{issuer}/.well-known/openid-configuration"
    async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT_SECONDS) as client:
        response = await client.get(discovery_url)
        response.raise_for_status()
        doc: Any = response.json()
    if not isinstance(doc, dict):
        raise httpx.HTTPError(
            f"expected JSON object from {discovery_url}, got {type(doc).__name__}"
        )
    authz = doc.get("authorization_endpoint")
    token = doc.get("token_endpoint")
    if not isinstance(authz, str) or not authz:
        raise httpx.HTTPError(f"discovery doc at {discovery_url} missing 'authorization_endpoint'")
    if not isinstance(token, str) or not token:
        raise httpx.HTTPError(f"discovery doc at {discovery_url} missing 'token_endpoint'")
    raw_end_session = doc.get("end_session_endpoint")
    end_session = raw_end_session if isinstance(raw_end_session, str) and raw_end_session else None
    return OIDCEndpoints(
        issuer=issuer,
        authorization_endpoint=authz,
        token_endpoint=token,
        end_session_endpoint=end_session,
    )


async def resolve_oidc_endpoints() -> OIDCEndpoints:
    """Return the cached :class:`OIDCEndpoints`, fetching on cache miss.

    The cache TTL is the same one
    :attr:`Settings.keycloak_jwks_cache_ttl_seconds` governs -- the
    discovery doc and the JWKS doc rotate on similar cadences, and
    pinning both surfaces to one operator-tunable knob keeps the
    operational story uniform.
    """
    global _discovery_cache, _discovery_fetched_at
    settings = get_settings()
    ttl = settings.keycloak_jwks_cache_ttl_seconds
    if _discovery_cache is not None and (time.monotonic() - _discovery_fetched_at) < ttl:
        return _discovery_cache
    async with _discovery_lock:
        if _discovery_cache is not None and (time.monotonic() - _discovery_fetched_at) < ttl:
            return _discovery_cache
        endpoints = await _fetch_discovery_doc(settings)
        _discovery_cache = endpoints
        _discovery_fetched_at = time.monotonic()
        return endpoints


# ---------------------------------------------------------------------------
# Authorization request + token exchange
# ---------------------------------------------------------------------------


def _ensure_client_configured(settings: Settings) -> None:
    """Raise :class:`OAuthFlowConfigurationError` when OAuth knobs are unset.

    Defence-in-depth check: the route layer also surfaces an early
    503 with the same detail, but this guard makes the failure
    deterministic even when the route is reached by a path that
    bypasses the early check (a future seam, a test that mounts the
    APIRouter directly without lifespan startup, etc.).
    """
    if not settings.ui_keycloak_client_id or not settings.ui_keycloak_client_secret:
        raise OAuthFlowConfigurationError(MISSING_CLIENT_SECRET_DETAIL)


def _build_oauth_client(settings: Settings, *, redirect_uri: str) -> AsyncOAuth2Client:
    """Construct a fresh :class:`AsyncOAuth2Client` bound to the BFF client.

    A new instance per call: authlib's client carries per-flow state
    on the instance, so reusing one across concurrent flows would
    cross-contaminate. Construction is cheap.

    ``scope='openid profile email'`` is the standard OIDC set that
    yields ``sub`` / ``name`` / ``email`` / tenant claims on the
    access token.

    ``code_challenge_method='S256'`` is the only acceptable method;
    OAuth 2.1 forbids plain.

    The token-endpoint auth method defaults to
    ``client_secret_basic`` (the authlib default when a secret is
    supplied) -- carries the credentials in the ``Authorization``
    header. Keycloak accepts both this and ``client_secret_post``.
    """
    return AsyncOAuth2Client(
        client_id=settings.ui_keycloak_client_id,
        client_secret=settings.ui_keycloak_client_secret,
        redirect_uri=redirect_uri,
        scope="openid profile email",
        code_challenge_method="S256",
        timeout=_HTTP_TIMEOUT_SECONDS,
    )


def _resource_indicator(settings: Settings) -> str:
    """Derive the RFC 8707 ``resource`` parameter from ``Settings.backplane_url``.

    Per work-item #2 on Initiative #337, the BFF requests a token
    bound to ``<backplane_url>/api`` -- the chassis API surface,
    distinct from the MCP-bound resource URI
    (:attr:`Settings.mcp_resource_uri`). Treating the two as separate
    audiences keeps an MCP-bound token from being accepted on a
    ``/api/v1/*`` request and vice versa.

    Falls back to an empty string when :attr:`Settings.backplane_url`
    is unset; authlib forwards the parameter verbatim and Keycloak
    silently ignores an empty value.
    """
    base = settings.backplane_url.rstrip("/")
    return f"{base}/api" if base else ""


async def build_authorization_request(
    *,
    redirect_uri: str,
    return_to: str,
) -> tuple[str, str]:
    """Mint a PKCE-protected authorization URL and register the verifier.

    Returns ``(authorization_url, state)``. The route handler 302s the
    browser at *authorization_url* and stores nothing client-side --
    the verifier (and the originally-requested *return_to* URL) live
    in :class:`PKCEVerifierStore`, keyed on the returned *state*.

    Parameters
    ----------
    redirect_uri
        Absolute URL the IdP redirects back to on completion. Must
        exactly match the redirect URI registered on the ``meho-web``
        Keycloak client (Keycloak enforces exact-match on the
        callback per OAuth 2.1).
    return_to
        The path the operator was trying to reach when the middleware
        redirected them to login. Stored alongside the verifier so
        the callback handler can finish the round-trip cleanly.

    Raises
    ------
    OAuthFlowConfigurationError
        :attr:`Settings.ui_keycloak_client_id` /
        :attr:`Settings.ui_keycloak_client_secret` are unset.
    """
    settings = get_settings()
    _ensure_client_configured(settings)
    endpoints = await resolve_oidc_endpoints()
    # PKCE verifier: authlib's :func:`generate_token` is an RFC-7636-
    # compliant URL-safe random string. Length 48 yields ~64 chars of
    # base64 -- comfortably above the spec's 43-128 char floor.
    code_verifier = generate_token(48)
    resource = _resource_indicator(settings)
    async with _build_oauth_client(settings, redirect_uri=redirect_uri) as client:
        url, state = client.create_authorization_url(
            endpoints.authorization_endpoint,
            code_verifier=code_verifier,
            resource=resource,
        )
    await get_verifier_store().put(state, code_verifier=code_verifier, return_to=return_to)
    return url, state


async def _post_to_token_endpoint(
    settings: Settings,
    endpoints: OIDCEndpoints,
    *,
    redirect_uri: str,
    authorization_response: str,
    state: str,
    pending: PendingFlow,
) -> dict[str, Any]:
    """Make the token-endpoint POST and return the raw token dict.

    Extracted from :func:`exchange_code_for_tokens` to keep the public
    entry-point under the 100-line code-quality cap. Encapsulates the
    one authlib seam that actually issues the HTTP call.
    """
    async with _build_oauth_client(settings, redirect_uri=redirect_uri) as client:
        # ``state`` is passed explicitly so authlib enforces the
        # CSRF cross-check against the response, not just the stored
        # client.state attribute. Mismatch raises authlib's own
        # ``MismatchingStateError``.
        token: Any = await client.fetch_token(
            endpoints.token_endpoint,
            authorization_response=authorization_response,
            code_verifier=pending.code_verifier,
            state=state,
            resource=_resource_indicator(settings),
        )
    if not isinstance(token, dict):
        raise OAuthFlowError("token_response_unexpected_shape")
    return token


def _project_token_response(token: dict[str, Any], return_to: str) -> TokenExchangeResult:
    """Validate the token-endpoint response and project it into the dataclass.

    Token endpoints are not infallible -- a Keycloak with
    offline_access disabled may omit ``refresh_token``, an upstream
    proxy may strip ``expires_in``. Each surface is mapped to a
    structured error so the route handler can render a stable
    operator-facing shape.

    Raises
    ------
    OAuthFlowError
        Token response missing ``access_token`` or ``refresh_token``.
        Missing ``expires_in`` is non-fatal -- defaults to 3600.
    """
    access = token.get("access_token")
    refresh = token.get("refresh_token")
    expires_in = token.get("expires_in")
    if not isinstance(access, str) or not access:
        raise OAuthFlowError("token_response_missing_access_token")
    if not isinstance(refresh, str) or not refresh:
        # Keycloak with offline_access disabled may omit refresh_token.
        # The BFF deliberately rejects that shape -- without a refresh
        # token the session cannot survive access-token expiry.
        raise OAuthFlowError("token_response_missing_refresh_token")
    # ``expires_in`` is an RFC 6749 §4.2.2 OPTIONAL field; default to
    # one hour when absent so the session lifetime calculation still
    # produces a sensible value.
    if not isinstance(expires_in, int) or expires_in <= 0:
        expires_in = 3600
    return TokenExchangeResult(
        access_token=access,
        refresh_token=refresh,
        expires_in=expires_in,
        return_to=return_to,
    )


async def exchange_code_for_tokens(
    *,
    redirect_uri: str,
    authorization_response: str,
    state: str | None,
) -> TokenExchangeResult:
    """Exchange the callback's ``code`` + stored ``code_verifier`` for tokens.

    Parameters
    ----------
    redirect_uri
        Same value passed to :func:`build_authorization_request`. The
        token endpoint cross-checks this against the redirect URI
        registered on the ``meho-web`` client.
    authorization_response
        Full callback URL including query string. authlib parses
        ``code``, ``state``, and any ``error`` query parameter out of
        this value.
    state
        Optional explicit ``state`` value. When passed, authlib
        cross-checks the response's ``state`` against this value and
        raises :class:`MismatchingStateError` on mismatch -- the
        belt-and-braces CSRF guard.

    Returns
    -------
    TokenExchangeResult
        Access + refresh tokens, expiry (seconds), and the
        ``return_to`` URL the original login flow stashed.

    Raises
    ------
    OAuthFlowConfigurationError
        Client knobs are unset.
    OAuthFlowError
        ``state`` is unknown / expired / the response is malformed.
    httpx.HTTPError
        Network failure on the token-endpoint POST.
    """
    settings = get_settings()
    _ensure_client_configured(settings)
    endpoints = await resolve_oidc_endpoints()
    if state is None:
        # No state means we cannot recover the verifier even if the
        # IdP echoed code+state back. Fail-closed.
        raise OAuthFlowError("missing_state")
    pending = await get_verifier_store().pop(state)
    if pending is None:
        raise OAuthFlowError("unknown_or_expired_state")
    token = await _post_to_token_endpoint(
        settings,
        endpoints,
        redirect_uri=redirect_uri,
        authorization_response=authorization_response,
        state=state,
        pending=pending,
    )
    result = _project_token_response(token, pending.return_to)
    log = structlog.get_logger(__name__)
    # Deliberately log NO token material -- only structural facts about
    # the exchange. The state is the per-flow CSRF token, not a
    # credential; safe to surface for correlation.
    log.info(
        "ui_auth_token_exchange_succeeded",
        state=state,
        expires_in=result.expires_in,
    )
    return result
