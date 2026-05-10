# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Keycloak JWT validation — JWKS fetch + cache + ``verify_jwt`` dependency.

This module is the load-bearing security primitive for every protected
route in the backplane. Every authenticated request flows through
:func:`verify_jwt`, which:

1. Reads the ``Authorization: Bearer <token>`` header.
2. Resolves the JWKS for the configured Keycloak issuer (cached
   in-memory; bounded by a TTL and refreshed on a kid-miss).
3. Verifies the JWT's signature, ``exp`` / ``nbf`` (with leeway),
   ``iss``, and ``aud`` against the configured values.
4. Returns an :class:`~meho_backplane.auth.operator.Operator` model
   carrying the validated claims plus the original token string (the
   raw JWT is needed verbatim by G2.2-T2's Vault forward-auth).

Library choice (per ADR 0004 — see Initiative #21 / Task #13):
``authlib.jose``. The module emits a deprecation warning recommending
``joserfc`` (authlib's own successor); we keep ``authlib.jose`` for v0.1
because the published API is stable until authlib 2.0 and the issue
body's reference sketch targets it. Migration to ``joserfc`` is tracked
as a v0.2 candidate.

JWKS-cache shape: a single module-level tuple of ``(jwks_dict,
fetched_at_monotonic)``. v0.1 runs single-worker uvicorn so the in-process
cache is fine; multi-worker deployments would need a Redis-backed shared
cache to avoid N worker-local round trips against Keycloak. That upgrade
is recorded in #21's "Out of scope" section.

Concurrency: the module-level cache is guarded by an ``asyncio.Lock`` so
the first wave of authenticated requests after startup doesn't fan out N
concurrent JWKS fetches.

Readiness probe: :func:`keycloak_readiness_probe` performs a *synchronous*
fetch against the same JWKS endpoint to bridge the registry's
``Callable[[], ProbeResult]`` contract (Task #19) to the network reality.
It is deliberately independent of the async cache — readiness must report
"is the dependency reachable right now?", not "did we last reach it
during a JWT verify?".

This module never logs token contents or claim values — the
``request_id`` correlation in :mod:`meho_backplane.middleware` is the
only crumb left on a 401, by design (no leaks of bearer secrets, no
identity leaks before authentication completes).
"""

from __future__ import annotations

import asyncio
import time
import warnings
from typing import Any

import httpx
import pydantic

# ``authlib.jose`` emits an ``AuthlibDeprecationWarning`` at first
# import, recommending ``joserfc`` (authlib's own successor). The
# published API stays compatible until authlib 2.0, and the issue
# body's reference sketch targets it; v0.1 commits to ``authlib.jose``
# and tracks migration as a v0.2 candidate. We deliberately do *not*
# suppress this import-time warning — ``authlib.deprecate`` calls
# ``warnings.simplefilter("always", AuthlibDeprecationWarning)`` which
# overrides any nested ``catch_warnings`` context. The single warning
# in pytest output is intentional and serves as the migration breadcrumb.
from authlib.jose import JsonWebKey, JsonWebToken
from authlib.jose.errors import (
    BadSignatureError,
    DecodeError,
    ExpiredTokenError,
    InvalidClaimError,
    InvalidTokenError,
    JoseError,
    MissingClaimError,
)
from fastapi import Header, HTTPException, status

from meho_backplane.auth.operator import Operator
from meho_backplane.health import ProbeResult
from meho_backplane.settings import Settings, get_settings

__all__ = [
    "clear_jwks_cache",
    "keycloak_readiness_probe",
    "verify_jwt",
]

#: HTTP timeout for both the OIDC discovery hit and the JWKS hit. Keep
#: tight: a hung Keycloak should fail-closed quickly rather than
#: starving request capacity.
_HTTP_TIMEOUT_SECONDS: float = 5.0

#: Algorithms accepted on the JWS header. Pinning to ``RS256`` mitigates
#: the algorithm-confusion class of attacks (CVE-2016-10555); Keycloak
#: defaults to ``RS256`` for ID/access tokens. Add other RS/ES variants
#: here only when a specific Keycloak realm requires them.
_ACCEPTED_ALGORITHMS: tuple[str, ...] = ("RS256",)

# Module-level JWKS cache. ``_jwks_cache`` is None until the first fetch
# succeeds; ``_jwks_fetched_at`` is the monotonic clock value at the
# successful fetch. monotonic time is used (not wall clock) so a system
# clock jump never silently extends or invalidates the cache.
_jwks_cache: dict[str, Any] | None = None
_jwks_fetched_at: float = 0.0
_jwks_lock: asyncio.Lock = asyncio.Lock()


def clear_jwks_cache() -> None:
    """Invalidate the JWKS cache. Test-only — never call from production."""
    global _jwks_cache, _jwks_fetched_at
    _jwks_cache = None
    _jwks_fetched_at = 0.0


async def _http_get_json(url: str) -> dict[str, Any]:
    """Fetch *url* and return the parsed JSON body.

    Wrapped in its own helper so tests can monkey-patch a single seam,
    and so the timeout / error-mapping policy lives in one place.
    Network failures are re-raised as :class:`httpx.HTTPError` for the
    caller to translate into a probe failure or a 401.
    """
    async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT_SECONDS) as client:
        response = await client.get(url)
        response.raise_for_status()
        data: Any = response.json()
        if not isinstance(data, dict):
            raise httpx.HTTPError(f"expected JSON object from {url}, got {type(data).__name__}")
        return data


async def _resolve_jwks_uri(settings: Settings) -> str:
    """Fetch the OIDC discovery doc and return the ``jwks_uri`` field.

    Keycloak guarantees the well-known endpoint at
    ``{issuer}/.well-known/openid-configuration``. We deliberately
    re-resolve discovery on every cache miss rather than caching the
    URI long-term; the cost is one extra round trip per cache miss
    (rare), and it lets operators rotate Keycloak realms (and the
    associated ``jwks_uri``) without restarting the backplane.
    """
    issuer = str(settings.keycloak_issuer_url).rstrip("/")
    discovery_url = f"{issuer}/.well-known/openid-configuration"
    discovery = await _http_get_json(discovery_url)
    jwks_uri = discovery.get("jwks_uri")
    if not isinstance(jwks_uri, str) or not jwks_uri:
        raise httpx.HTTPError(f"discovery doc at {discovery_url} missing 'jwks_uri'")
    return jwks_uri


async def _fetch_jwks(*, force_refresh: bool = False) -> dict[str, Any]:
    """Return a JWKS dict, fetching from Keycloak when the cache is stale.

    Cache-hit conditions:

    * ``_jwks_cache`` is populated, AND
    * ``force_refresh`` is False, AND
    * ``time.monotonic() - _jwks_fetched_at`` is below the configured TTL.

    Otherwise the function fetches discovery → JWKS, repopulates the
    cache atomically under ``_jwks_lock``, and returns the new dict.
    The lock prevents a thundering herd on first request after startup
    or after a TTL expiry.
    """
    global _jwks_cache, _jwks_fetched_at

    settings = get_settings()
    ttl = settings.keycloak_jwks_cache_ttl_seconds

    if (
        not force_refresh
        and _jwks_cache is not None
        and (time.monotonic() - _jwks_fetched_at) < ttl
    ):
        return _jwks_cache

    async with _jwks_lock:
        # Re-check inside the lock — a sibling coroutine may have
        # populated the cache while we were waiting.
        if (
            not force_refresh
            and _jwks_cache is not None
            and (time.monotonic() - _jwks_fetched_at) < ttl
        ):
            return _jwks_cache

        jwks_uri = await _resolve_jwks_uri(settings)
        jwks = await _http_get_json(jwks_uri)
        if not isinstance(jwks.get("keys"), list):
            raise httpx.HTTPError(f"JWKS at {jwks_uri} missing 'keys' array")

        _jwks_cache = jwks
        _jwks_fetched_at = time.monotonic()
        return jwks


def _decode_with_jwks(token: str, jwks: dict[str, Any], settings: Settings) -> Any:
    """Verify *token* against *jwks* and return the validated claims.

    Wrapped behind the ``warnings.catch_warnings`` block to suppress the
    authlib-jose deprecation noise on every request — the deprecation is
    a v0.2 migration item, not a per-request signal.
    """
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        jwt = JsonWebToken(list(_ACCEPTED_ALGORITHMS))
        key_set = JsonWebKey.import_key_set(jwks)
        claims = jwt.decode(
            token,
            key_set,
            claims_options={
                "iss": {
                    "essential": True,
                    "value": str(settings.keycloak_issuer_url).rstrip("/"),
                },
                "aud": {
                    "essential": True,
                    "value": settings.keycloak_audience,
                },
            },
        )
        claims.validate(leeway=settings.keycloak_jwt_leeway_seconds)
        return claims


# Exception classes that authlib raises for any kind of token-content
# failure (signature, claims, structure). Grouped so ``verify_jwt`` can
# treat them uniformly as 401 ``invalid_token``. ``_DECODE_ERRORS_WITH_VALUEERROR``
# adds ``ValueError`` for the post-refresh retry path where a residual
# kid-miss must also be classified as ``invalid_token`` rather than
# triggering another infinite refresh loop.
_AUTHLIB_DECODE_ERRORS: tuple[type[Exception], ...] = (
    BadSignatureError,
    DecodeError,
    ExpiredTokenError,
    InvalidClaimError,
    InvalidTokenError,
    MissingClaimError,
    JoseError,
)
_DECODE_ERRORS_WITH_VALUEERROR: tuple[type[Exception], ...] = (
    ValueError,
    *_AUTHLIB_DECODE_ERRORS,
)


def _http_401(detail: str) -> HTTPException:
    """Build the 401 the dependency raises on any failure path.

    Centralised so the contract is one-line-changeable and so detail
    strings don't drift across call sites — the dispatch table tests
    in Task #25 will assert on these tokens verbatim.
    """
    return HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=detail)


def _extract_bearer_token(authorization: str | None) -> str:
    """Return the bare token from ``Authorization: Bearer <token>``.

    Raises 401 ``missing_token`` for any header shape that isn't a
    well-formed Bearer credential, including the empty-token edge case
    (``Bearer    `` with whitespace only).
    """
    if not authorization or not authorization.startswith("Bearer "):
        raise _http_401("missing_token")
    token = authorization.removeprefix("Bearer ").strip()
    if not token:
        raise _http_401("missing_token")
    return token


async def _decode_with_kid_rotation(token: str, settings: Settings) -> Any:
    """Decode *token* against the cached JWKS, refreshing once on a kid miss.

    The first ``_fetch_jwks`` call serves the cached keyset (or fetches
    one if the cache is empty). authlib raises ``ValueError`` (currently
    with the message ``"Key not found"``) when the JWT's ``kid`` is
    absent from the keyset — but the message is an internal detail and
    has changed across authlib releases. We deliberately do **not**
    string-match on it: any ``ValueError`` from ``_decode_with_jwks``
    triggers a single, bounded refresh-and-retry. Other decoding errors
    (signature, claims, structure) fail fast as 401 ``invalid_token``.

    The retry budget is exactly one — a second ``ValueError`` after the
    forced JWKS refresh is treated as a hard 401, preventing an
    infinite-refresh loop on a token whose ``kid`` truly does not exist.
    """
    try:
        jwks = await _fetch_jwks()
    except (httpx.HTTPError, KeyError):
        # JWKS unreachable surfaces as a credentials failure (401),
        # not 5xx; /ready will flap on the same root cause via the
        # readiness probe.
        raise _http_401("jwks_unavailable") from None

    try:
        return _decode_with_jwks(token, jwks, settings)
    except ValueError:
        # Treat *any* ValueError as a kid-miss signal — the message
        # ("Key not found") is authlib-internal and not part of any
        # stable contract. Fall through to refresh-and-retry.
        pass
    except _AUTHLIB_DECODE_ERRORS as exc:
        raise _http_401("invalid_token") from exc

    # Kid miss → refresh once and retry.
    try:
        jwks = await _fetch_jwks(force_refresh=True)
    except (httpx.HTTPError, KeyError):
        raise _http_401("jwks_unavailable") from None

    try:
        return _decode_with_jwks(token, jwks, settings)
    except _DECODE_ERRORS_WITH_VALUEERROR as retry_exc:
        raise _http_401("invalid_token") from retry_exc


def _operator_from_claims(claims: Any, raw_jwt: str) -> Operator:
    """Project the validated claims into the public :class:`Operator` shape.

    A signature-valid JWT can still carry malformed claim values — most
    notably an ``email`` that fails the ``EmailStr`` validator on
    :class:`Operator`. Pydantic raises ``ValidationError`` in that case;
    the security contract is that *every* failure to materialise a
    trusted operator from a token surfaces as 401 ``invalid_token``,
    never an unhandled 500. The try/except converts the validation
    failure into the same 401 the rest of the dependency emits.
    """
    sub = claims.get("sub")
    if not isinstance(sub, str) or not sub:
        # ``sub`` is mandated by OIDC core §2; a token without it is
        # malformed regardless of signature validity.
        raise _http_401("invalid_token")
    name = claims.get("name")
    email = claims.get("email")
    try:
        return Operator(
            sub=sub,
            name=name if isinstance(name, str) else None,
            email=email if isinstance(email, str) else None,
            raw_jwt=raw_jwt,
        )
    except pydantic.ValidationError as exc:
        raise _http_401("invalid_token") from exc


async def verify_jwt(authorization: str | None = Header(default=None)) -> Operator:
    """FastAPI dependency: validate the Bearer token and return an Operator.

    Raises 401 on every failure mode — missing header, malformed
    ``Authorization`` shape, JWKS unreachable, signature mismatch,
    expired token, audience mismatch, issuer mismatch, malformed JWT.
    The error body is intentionally terse (``{"detail": "<reason>"}``)
    and never echoes claim values; that prevents an unauthenticated
    caller from probing the backplane for token shape.

    Kid-rotation handling lives in :func:`_decode_with_kid_rotation`:
    on the canonical "Key not found" ValueError from authlib, the JWKS
    cache is refreshed exactly once and the verify is retried. A
    second miss is a hard 401 ``invalid_token``.
    """
    token = _extract_bearer_token(authorization)
    settings = get_settings()
    claims = await _decode_with_kid_rotation(token, settings)
    return _operator_from_claims(claims, token)


def _http_get_json_sync(url: str) -> dict[str, Any]:
    """Synchronous twin of :func:`_http_get_json` for the readiness probe.

    The probe registry from Task #19 expects a synchronous callable, so
    a sync HTTP client lives here. We don't share the async cache —
    readiness is "Keycloak reachable *now*", not "we last reached it
    inside some request".
    """
    with httpx.Client(timeout=_HTTP_TIMEOUT_SECONDS) as client:
        response = client.get(url)
        response.raise_for_status()
        data: Any = response.json()
        if not isinstance(data, dict):
            raise httpx.HTTPError(f"expected JSON object from {url}, got {type(data).__name__}")
        return data


def keycloak_readiness_probe() -> ProbeResult:
    """Readiness probe: confirm Keycloak's JWKS endpoint is fetchable.

    Registered with :mod:`meho_backplane.health` at app startup.

    The probe issues a fresh discovery + JWKS fetch on every call. That
    is intentional: ``/ready`` should reflect *current* dependency
    health, not a stale cache state. Probes are cheap by registry
    contract (Task #19), and Keycloak's JWKS endpoint is a single
    HTTP GET against a CDN-able JSON document — a few hundred
    milliseconds in the worst case.

    Failure detail surfaces the exception class name (not its
    message) so the probe payload never leaks issuer URLs or other
    operator-controllable strings into a 503 response.
    """
    try:
        settings = get_settings()
    except Exception as exc:
        return ProbeResult(
            name="keycloak",
            ok=False,
            detail=f"settings_unavailable: {type(exc).__name__}",
        )

    issuer = str(settings.keycloak_issuer_url).rstrip("/")
    discovery_url = f"{issuer}/.well-known/openid-configuration"

    try:
        discovery = _http_get_json_sync(discovery_url)
        jwks_uri = discovery.get("jwks_uri")
        if not isinstance(jwks_uri, str) or not jwks_uri:
            return ProbeResult(
                name="keycloak",
                ok=False,
                detail="jwks_uri_missing",
            )
        jwks = _http_get_json_sync(jwks_uri)
        if not isinstance(jwks.get("keys"), list):
            return ProbeResult(
                name="keycloak",
                ok=False,
                detail="jwks_malformed",
            )
    except Exception as exc:
        return ProbeResult(
            name="keycloak",
            ok=False,
            detail=f"jwks_fetch_failed: {type(exc).__name__}",
        )

    return ProbeResult(name="keycloak", ok=True, detail="jwks_fetched")
