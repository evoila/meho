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

Token contents and bearer secrets are *never* logged. The tenant-claim
extraction failure paths log only the configured claim name (an
operator-controlled config string) and, for malformed values, the bad
value verbatim — that value has already been signed by the trusted
issuer and is therefore part of the issuer's claim namespace, never a
caller-controlled secret. The ``request_id`` correlation bound by
:mod:`meho_backplane.middleware` ties each log line back to the
originating request without exposing identity before authentication
completes.
"""

from __future__ import annotations

import asyncio
import re
import time
import warnings
from typing import Any, NoReturn
from uuid import UUID

import httpx
import pydantic
import structlog

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

from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.health import ProbeResult
from meho_backplane.settings import Settings, get_settings

# NOTE: structlog logger is resolved per-call inside the helpers below
# rather than held as a module-level proxy. Production sets
# ``cache_logger_on_first_use=True`` in
# ``meho_backplane.logging.configure_logging``; a cached BoundLogger
# pins a reference to the ``_CONFIG.default_processors`` list it was
# built with, and later ``structlog.configure(...)`` calls *replace*
# (not mutate) that reference — so test fixtures that swap the
# processor chain (``structlog.testing.capture_logs``, per-test
# ``configure`` + ``reset_defaults``) cannot reach the orphaned
# reference the cached logger still holds. Same precedent + rationale
# as ``meho_backplane.retrieval.embedding.EmbeddingService`` (see
# ``docs/codebase/backend.md``). Per-call cost is a few microseconds;
# acceptable on these 401 paths which are not latency-critical.
# Originally exposed by the ``-n 3 --dist loadscope`` flake in #738.

__all__ = [
    "AUDIENCE_NOT_CONFIGURED_REMEDIATION",
    "clear_jwks_cache",
    "keycloak_readiness_probe",
    "verify_jwt",
    "verify_jwt_for_audience",
]

#: HTTP timeout for both the OIDC discovery hit and the JWKS hit. Keep
#: tight: a hung Keycloak should fail-closed quickly rather than
#: starving request capacity.
_HTTP_TIMEOUT_SECONDS: float = 5.0

#: Operator-facing remediation appended to the ``audience_not_configured``
#: 401. The bare ``audience_not_configured`` token told an operator *that*
#: the MCP audience was unset but not *what to do* — the consumer dogfood
#: signal (#633) was an operator staring at a context-free 401 with the
#: ``/mcp`` surface dark. The text names the two settings that resolve the
#: audience and the Keycloak audience-mapper step, and links the operator
#: runbook. Static (no token/claim/secret interpolation) so it stays safe
#: to surface in an unauthenticated 401 body and to log verbatim from
#: :func:`meho_backplane.mcp.auth.verify_mcp_jwt`.
AUDIENCE_NOT_CONFIGURED_REMEDIATION: str = (
    "audience_not_configured: the MCP resource URI is unset, so every /mcp "
    "request fails closed. Set MCP_RESOURCE_URI (e.g. https://<host>/mcp, no "
    "trailing slash) or BACKPLANE_URL (the URI is then derived as "
    "${BACKPLANE_URL}/mcp), and add a Keycloak oidc-audience-mapper carrying "
    "that exact value on the client that issues the caller's token. See "
    "docs/cross-repo/mcp-client-setup.md Step 1."
)

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


def _decode_with_jwks(
    token: str,
    jwks: dict[str, Any],
    settings: Settings,
    *,
    expected_audience: str,
) -> Any:
    """Verify *token* against *jwks* and return the validated claims.

    Wrapped behind the ``warnings.catch_warnings`` block to suppress the
    authlib-jose deprecation noise on every request — the deprecation is
    a v0.2 migration item, not a per-request signal.

    ``expected_audience`` is passed by the caller so MCP routes (G0.5-T2)
    can validate against the MCP canonical URI rather than the chassis
    ``KEYCLOAK_AUDIENCE``. The chassis ``verify_jwt`` passes
    ``settings.keycloak_audience`` to preserve existing behaviour.
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
                    "value": expected_audience,
                },
                # ``sub`` is REQUIRED by OIDC core §2 / RFC 9068 §2.2.1
                # on access tokens. Marking it essential here pushes the
                # check into authlib's structured ``MissingClaimError``
                # path so :func:`_classify_decode_error` can surface a
                # specific ``missing_sub`` code instead of letting the
                # claim drop through to :func:`_operator_from_claims`'s
                # generic fallback (where it would collapse to the
                # opaque ``invalid_token`` code v0.3.1 shipped — see
                # G0.9.1-T12, consumer Addendum II walls #2 / #3).
                "sub": {"essential": True},
            },
        )
        claims.validate(leeway=settings.keycloak_jwt_leeway_seconds)
        return claims


# Exception classes that authlib raises for any kind of token-content
# failure (signature, claims, structure). Grouped so ``verify_jwt`` can
# dispatch them through :func:`_classify_decode_error` into a structured
# 401 detail code. ``_DECODE_ERRORS_WITH_VALUEERROR`` adds ``ValueError``
# for the post-refresh retry path where a residual kid-miss must also
# fail-closed as 401 rather than triggering another infinite refresh
# loop.
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


# ``MissingClaimError`` does not expose the claim name as an attribute
# the way ``InvalidClaimError`` does (``InvalidClaimError.claim_name``);
# the canonical authlib 1.7 constructor stores the name only in the
# ``description`` field as ``"Missing '<claim>' claim"``. Parse it back
# out so :func:`_classify_decode_error` can branch on the specific
# missing claim (``sub`` / ``aud`` / ``iss``) rather than collapsing
# every missing essential claim into one opaque code. The pattern is
# anchored to the documented authlib message format; if a future
# authlib release changes the wording, the fallback branch logs the
# raw description as ``detail`` so an operator still has the
# diagnostic value in the log line.
_MISSING_CLAIM_NAME_RE: re.Pattern[str] = re.compile(r"Missing '([^']+)' claim")


def _http_401(detail: str) -> HTTPException:
    """Build the 401 the dependency raises on any failure path.

    Centralised so the contract is one-line-changeable and so detail
    strings don't drift across call sites — the dispatch table tests
    in Task #25 will assert on these tokens verbatim.
    """
    return HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=detail)


def _classify_invalid_claim(
    exc: InvalidClaimError,
    *,
    expected_audience: str,
    expected_issuer: str,
) -> tuple[str, dict[str, Any]]:
    """``InvalidClaimError`` → specific code per `claim_name`.

    Split out of :func:`_classify_decode_error` so the parent stays
    inside the code-quality function-length budget. ``InvalidClaimError``
    is the only authlib decode exception that carries the offending
    claim name as a structured attribute (``claim_name``); the parent
    dispatches all other exception classes inline.
    """
    claim_name = getattr(exc, "claim_name", None) or "unknown"
    if claim_name == "aud":
        return "invalid_audience", {
            "claim_name": "aud",
            "expected_audience": expected_audience,
        }
    if claim_name == "iss":
        return "invalid_issuer", {
            "claim_name": "iss",
            "expected_issuer": expected_issuer,
        }
    return "invalid_claim", {"claim_name": claim_name}


def _classify_missing_claim(
    exc: MissingClaimError,
) -> tuple[str, dict[str, Any]]:
    """``MissingClaimError`` → specific code per parsed claim name.

    Authlib 1.7's ``MissingClaimError`` does not expose the claim name
    as an attribute (only ``InvalidClaimError`` does); the constructor
    stores it in ``description`` as ``"Missing '<claim>' claim"``. The
    regex parse falls back to the raw description in the log line if a
    future authlib release changes the wording — the diagnostic value
    is preserved even if the specific code lands as the generic
    ``missing_claim``.
    """
    description = getattr(exc, "description", "") or ""
    match = _MISSING_CLAIM_NAME_RE.search(description)
    claim_name = match.group(1) if match else None
    if claim_name == "sub":
        # RFC 9068 §2.2.1 makes ``sub`` REQUIRED on access tokens.
        # Promoted from ``invalid_token`` (the v0.3.1 catch-all in
        # :func:`_operator_from_claims`) to a specific code per
        # G0.9.1-T12.
        return "missing_sub", {"claim_name": "sub"}
    if claim_name == "aud":
        return "missing_audience", {"claim_name": "aud"}
    if claim_name == "iss":
        return "missing_issuer", {"claim_name": "iss"}
    return "missing_claim", {
        "claim_name": claim_name,
        "detail": description,
    }


def _classify_decode_error(
    exc: BaseException,
    settings: Settings,
    *,
    expected_audience: str,
) -> tuple[str, dict[str, Any]]:
    """Map an authlib decode/claims exception to a structured 401 code.

    Returns ``(detail_code, log_fields)``:

    * ``detail_code`` — value put into the 401 body. Machine-readable
      and low info-leak: ``invalid_audience`` / ``invalid_issuer`` /
      ``missing_sub`` / ``token_expired`` /
      ``signature_verification_failed`` / ``token_not_yet_valid``, with
      a residual ``invalid_token`` for genuinely-unclassifiable
      structural failures (truncated JWS, ``alg: none`` rejection,
      post-refresh kid miss).
    * ``log_fields`` — diagnostic value(s) for the structlog event
      (expected audience / issuer, missing-claim name, exception class).
      **Never** echoed in the response body. Mirrors the
      ``malformed_tenant_claim`` body-vs-log split: public callers see
      the code, operators with log access see the full picture
      (RFC 6750 §3.1 — the resource server SHOULD NOT include details
      that aren't well-defined error codes in the body).

    Pre-G0.9.1-T12 every authlib decode failure collapsed to one
    opaque ``invalid_token``; an operator chasing a 401 had to mint
    side-by-side tokens to discover which check fired (consumer
    Addendum II walls #2 + #3). The classifier mirrors the existing
    tenant-claim extractors' pattern.
    """
    # ExpiredTokenError comes first so a token that is *both* expired
    # and (e.g.) audience-mismatched surfaces the expiry — operators
    # rotate tokens far more often than they reconfigure audiences and
    # the expired-token path is by far the most common 401 reason in
    # healthy production.
    if isinstance(exc, ExpiredTokenError):
        return "token_expired", {"reason": "exp_in_past_beyond_leeway"}
    if isinstance(exc, BadSignatureError):
        return "signature_verification_failed", {"reason": "jws_signature_mismatch"}
    if isinstance(exc, InvalidClaimError):
        return _classify_invalid_claim(
            exc,
            expected_audience=expected_audience,
            expected_issuer=str(settings.keycloak_issuer_url).rstrip("/"),
        )
    if isinstance(exc, MissingClaimError):
        return _classify_missing_claim(exc)
    if isinstance(exc, InvalidTokenError):
        # Authlib raises bare ``InvalidTokenError`` (no claim name) for
        # ``nbf`` / ``iat`` in the future. Surface as a distinct
        # ``token_not_yet_valid`` — operationally that means a clock
        # somewhere is wrong (Keycloak or the client), a very different
        # remediation from any other failure mode.
        return "token_not_yet_valid", {"reason": "nbf_or_iat_in_future_beyond_leeway"}
    if isinstance(exc, DecodeError):
        # Structural break (malformed compact JWS, base64 garbage). No
        # claim semantics — body and log both name ``invalid_token``.
        return "invalid_token", {"reason": "jws_decode_error"}
    # Catch-all for ``JoseError`` (unknown algorithm header, alg=none
    # rejection, future authlib subclasses). Exception class name lands
    # in the log so an operator can grep the specific authlib failure
    # even though the public code collapses to ``invalid_token``.
    return "invalid_token", {"reason": "jose_error", "exception": type(exc).__name__}


def _raise_decode_401(
    exc: BaseException,
    settings: Settings,
    *,
    expected_audience: str,
) -> NoReturn:
    """Classify *exc*, emit the structured structlog event, raise 401.

    The detail code goes into the 401 response body; the diagnostic
    fields go into the structlog event. Mirrors the
    ``malformed_tenant_claim`` body-vs-log split: public callers see
    only the code, operators with log access see the full picture.
    """
    detail_code, log_fields = _classify_decode_error(
        exc,
        settings,
        expected_audience=expected_audience,
    )
    log = structlog.get_logger(__name__)
    log.warning(detail_code, **log_fields)
    raise _http_401(detail_code) from exc


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


async def _decode_with_kid_rotation(
    token: str,
    settings: Settings,
    *,
    expected_audience: str,
) -> Any:
    """Decode *token* against the cached JWKS, refreshing once on a kid miss.

    The first ``_fetch_jwks`` call serves the cached keyset (or fetches
    one if the cache is empty). authlib raises ``ValueError`` (currently
    with the message ``"Key not found"``) when the JWT's ``kid`` is
    absent from the keyset — but the message is an internal detail and
    has changed across authlib releases. We deliberately do **not**
    string-match on it: any ``ValueError`` from ``_decode_with_jwks``
    triggers a single, bounded refresh-and-retry. Other decoding errors
    (signature, claims, structure) fail fast through
    :func:`_raise_decode_401`, which classifies the authlib exception
    into a specific 401 ``detail`` code (G0.9.1-T12 / #797).

    The retry budget is exactly one — a second ``ValueError`` after the
    forced JWKS refresh is treated as a hard 401, preventing an
    infinite-refresh loop on a token whose ``kid`` truly does not exist.

    ``expected_audience`` is forwarded to :func:`_decode_with_jwks` so
    callers (chassis ``verify_jwt`` vs MCP ``verify_mcp_jwt``) can
    enforce different audiences against the same JWKS + issuer pair.
    """
    try:
        jwks = await _fetch_jwks()
    except (httpx.HTTPError, KeyError):
        # JWKS unreachable surfaces as a credentials failure (401),
        # not 5xx; /ready will flap on the same root cause via the
        # readiness probe.
        raise _http_401("jwks_unavailable") from None

    try:
        return _decode_with_jwks(token, jwks, settings, expected_audience=expected_audience)
    except ValueError:
        # Treat *any* ValueError as a kid-miss signal — the message
        # ("Key not found") is authlib-internal and not part of any
        # stable contract. Fall through to refresh-and-retry.
        pass
    except _AUTHLIB_DECODE_ERRORS as exc:
        _raise_decode_401(exc, settings, expected_audience=expected_audience)

    # Kid miss → refresh once and retry.
    try:
        jwks = await _fetch_jwks(force_refresh=True)
    except (httpx.HTTPError, KeyError):
        raise _http_401("jwks_unavailable") from None

    try:
        return _decode_with_jwks(token, jwks, settings, expected_audience=expected_audience)
    except ValueError as retry_exc:
        # A second ValueError after the forced refresh means the kid
        # truly is not in the JWKS — fail-closed as a structural
        # ``invalid_token`` (no claim-level diagnostic to surface).
        raise _http_401("invalid_token") from retry_exc
    except _AUTHLIB_DECODE_ERRORS as retry_exc:
        _raise_decode_401(retry_exc, settings, expected_audience=expected_audience)


def _extract_tenant_id(claims: Any, settings: Settings) -> UUID:
    """Pull ``tenant_id`` out of *claims* and parse it as a :class:`UUID`.

    Two distinct failure modes, each surfaced with its own structlog
    event so an operator chasing a 401 can tell whether the issuer's
    protocol-mapper is missing the claim entirely (``missing_tenant_claim``,
    fix the Keycloak realm) or is emitting a value that isn't a UUID
    (``malformed_tenant_claim``, fix the mapper's value expression).

    The bare claim value is included in the malformed log line — it is
    a value the trusted issuer signed, so it belongs to the issuer's
    claim namespace, not to the caller. The configured claim *name* is
    always logged so operators with non-default ``JWT_TENANT_CLAIM_NAME``
    can grep their settings without re-deriving the contract.
    """
    log = structlog.get_logger(__name__)
    claim_name = settings.jwt_tenant_claim_name
    raw = claims.get(claim_name)
    if raw is None:
        log.warning("missing_tenant_claim", claim_name=claim_name)
        raise _http_401("missing_tenant_claim")
    try:
        return UUID(raw) if isinstance(raw, str) else UUID(str(raw))
    except (ValueError, TypeError, AttributeError) as exc:
        log.warning(
            "malformed_tenant_claim",
            claim_name=claim_name,
            value=raw,
        )
        raise _http_401("malformed_tenant_claim") from exc


def _extract_tenant_role(claims: Any, settings: Settings) -> TenantRole:
    """Pull ``tenant_role`` out of *claims* and resolve it to a :class:`TenantRole`.

    Like :func:`_extract_tenant_id`, distinguishes the two failure
    modes via separate structlog events
    (``missing_tenant_role_claim`` vs ``unknown_tenant_role``) so the
    on-call telemetry maps cleanly to remediation: the first means the
    Keycloak role-mapper isn't installed; the second means the realm
    is emitting a role string outside the closed v0.2 enum (likely a
    typo or a future role that needs the enum widened first).
    """
    log = structlog.get_logger(__name__)
    claim_name = settings.jwt_tenant_role_claim_name
    raw = claims.get(claim_name)
    if raw is None:
        log.warning("missing_tenant_role_claim", claim_name=claim_name)
        raise _http_401("missing_tenant_role_claim")
    try:
        return TenantRole(raw)
    except ValueError as exc:
        log.warning(
            "unknown_tenant_role",
            claim_name=claim_name,
            value=raw,
        )
        raise _http_401("unknown_tenant_role") from exc


def _extract_principal_kind(claims: Any, settings: Settings) -> PrincipalKind:
    """Extract ``principal_kind`` from *claims* with a ``user`` default.

    G11.2-T1 (#815): the ``principal_kind`` claim is **optional** — a
    JWT that carries no ``principal_kind`` claim is treated as a human
    user (the pre-G11.2 default). This keeps all existing human-operator
    tokens working without a Keycloak mapper update.

    Only three values are accepted: ``user``, ``service``, ``agent``.
    An unrecognised value is logged and defaults to ``user`` (not a 401)
    — an unknown principal kind should not lock out an operator whose
    realm emits a custom value; G11.2-T3 will enforce per-kind permission
    restrictions and can surface a more targeted error at that point.

    The claim name is configurable via ``JWT_PRINCIPAL_KIND_CLAIM_NAME``
    (default ``principal_kind``) in :class:`~meho_backplane.settings.Settings`
    so realms that surface the discriminator under a different attribute
    can be accommodated without code changes.
    """
    claim_name = settings.jwt_principal_kind_claim_name
    raw = claims.get(claim_name)
    if raw is None:
        # Claim absent → legacy human-operator token. Graceful fallback.
        return PrincipalKind.USER
    try:
        return PrincipalKind(raw)
    except ValueError:
        log = structlog.get_logger(__name__)
        log.warning(
            "unknown_principal_kind",
            claim_name=claim_name,
            value=raw,
        )
        return PrincipalKind.USER


def _operator_from_claims(claims: Any, raw_jwt: str, settings: Settings) -> Operator:
    """Project the validated claims into the public :class:`Operator` shape.

    A signature-valid JWT can still carry malformed claim values — most
    notably an ``email`` that fails the ``EmailStr`` validator on
    :class:`Operator`, or a tenant claim shape that the issuer hasn't
    been configured to populate. The security contract is that *every*
    failure to materialise a trusted operator from a token surfaces as
    401 — never an unhandled 500 — but the failure *reason* must be
    distinguishable in logs so on-call doesn't have to guess between a
    misconfigured issuer, a tampered token, and a legitimate
    ``tenant_role`` the v0.2 enum doesn't yet model.

    Tenant-claim extraction runs *before* the :class:`Operator`
    constructor: each failure mode has its own structlog event and 401
    detail token (``missing_tenant_claim`` / ``malformed_tenant_claim``
    / ``missing_tenant_role_claim`` / ``unknown_tenant_role``) so the
    bare ``invalid_token`` fallback is reserved for unexpected
    pydantic validation failures (the malformed-email regression case).

    ``principal_kind`` extraction is graceful (unknown value → ``user``
    default) per G11.2-T1 — an unrecognised kind must not break existing
    human-operator flows.
    """
    sub = claims.get("sub")
    if not isinstance(sub, str) or not sub:
        # ``sub`` is mandated by OIDC core §2 / RFC 9068 §2.2.1. The
        # primary enforcement point is the ``sub: essential: True``
        # entry in :func:`_decode_with_jwks`'s ``claims_options`` —
        # a token without ``sub`` raises ``MissingClaimError('sub')``
        # during decode, which :func:`_classify_decode_error` surfaces
        # as the ``missing_sub`` code. This defensive check covers the
        # one residual edge case authlib doesn't catch: a ``sub`` that
        # is *present but not a non-empty string* (e.g. ``{"sub": null}``
        # or ``{"sub": ""}`` — neither violates "essential" in authlib
        # 1.7's ``_validate_essential_claims`` quite the way intuition
        # suggests; ``null`` passes the ``k in self`` check). Surface
        # the same ``missing_sub`` code so on-call telemetry sees one
        # event class regardless of which mode fired.
        log = structlog.get_logger(__name__)
        log.warning("missing_sub", claim_name="sub", reason="empty_or_non_string")
        raise _http_401("missing_sub")
    name = claims.get("name")
    email = claims.get("email")
    tenant_id = _extract_tenant_id(claims, settings)
    tenant_role = _extract_tenant_role(claims, settings)
    principal_kind = _extract_principal_kind(claims, settings)
    try:
        return Operator(
            sub=sub,
            name=name if isinstance(name, str) else None,
            email=email if isinstance(email, str) else None,
            raw_jwt=raw_jwt,
            tenant_id=tenant_id,
            tenant_role=tenant_role,
            principal_kind=principal_kind,
        )
    except pydantic.ValidationError as exc:
        raise _http_401("invalid_token") from exc


async def verify_jwt_for_audience(
    authorization: str | None,
    *,
    expected_audience: str,
) -> Operator:
    """Validate a Bearer token against an explicit ``aud`` claim value.

    The full JWT-validation chain (Bearer extraction → JWKS fetch +
    kid-rotation retry → signature/claims/structure validation →
    Operator projection) parametrised by audience. This is the public
    seam the chassis :func:`verify_jwt` dependency uses with
    ``settings.keycloak_audience``; MCP routes (G0.5-T2) use the same
    seam with the MCP canonical URI per RFC 8707 §2 / RFC 9728 §7.4
    audience-binding semantics. Keeping both surfaces on a single chain
    avoids drift between "what the chassis validates" and "what MCP
    validates" — issuer / kid-rotation / signature handling stays
    identical; only the audience differs.

    Raises 401 on every failure mode the chassis chain raises. Each
    decode-stage failure surfaces a *specific* ``detail`` code so an
    operator chasing the 401 can name the failed check (see
    :func:`_classify_decode_error` for the full mapping):
    ``invalid_audience`` / ``invalid_issuer`` / ``missing_sub`` /
    ``token_expired`` / ``signature_verification_failed`` /
    ``token_not_yet_valid``, with the residual ``invalid_token`` kept
    only for structural failures that don't admit a more specific code
    (truncated JWS, ``alg: none`` rejection, post-refresh kid miss).
    The expected-vs-received diagnostic values land in the structlog
    event only — never in the unauthenticated 401 body — mirroring the
    existing ``malformed_tenant_claim`` precedent (G0.9.1-T12).

    Defence in depth: an empty or whitespace-only ``expected_audience``
    short-circuits to a 401 whose ``detail`` is
    :data:`AUDIENCE_NOT_CONFIGURED_REMEDIATION` — it still starts with
    the ``audience_not_configured`` token (callers that match the prefix
    keep working) but now also names ``MCP_RESOURCE_URI`` /
    ``BACKPLANE_URL`` + the Keycloak audience-mapper step and links the
    operator runbook, so the failure is actionable rather than opaque
    (#633). The MCP route derives the audience from ``MCP_RESOURCE_URI``
    / ``BACKPLANE_URL`` and returns an empty string when neither is set;
    without this guard the fail-closed property would rely on the
    *issuer* never emitting a token with an empty ``aud`` claim — a true
    assumption against any real Keycloak deployment but one external
    invariant away from a bypass. Failing the check locally to the
    verifier makes the property auditable in one place.
    """
    if not expected_audience or not expected_audience.strip():
        raise _http_401(AUDIENCE_NOT_CONFIGURED_REMEDIATION)
    token = _extract_bearer_token(authorization)
    settings = get_settings()
    claims = await _decode_with_kid_rotation(
        token,
        settings,
        expected_audience=expected_audience,
    )
    return _operator_from_claims(claims, token, settings)


async def verify_jwt(authorization: str | None = Header(default=None)) -> Operator:
    """FastAPI dependency: validate the Bearer token and return an Operator.

    Raises 401 on every failure mode — missing header, malformed
    ``Authorization`` shape, JWKS unreachable, signature mismatch,
    expired token, audience mismatch, issuer mismatch, malformed JWT.
    Each decode-stage failure surfaces a *specific* ``detail`` code
    (``invalid_audience`` / ``invalid_issuer`` / ``missing_sub`` /
    ``token_expired`` / ``signature_verification_failed`` /
    ``token_not_yet_valid``) so an operator chasing the 401 can name
    the failed check; the expected-vs-received diagnostic values land
    in the structlog event only — never in the response body. The
    error body is intentionally terse (``{"detail": "<reason>"}``)
    and never echoes claim values; that prevents an unauthenticated
    caller from probing the backplane for token shape.

    Kid-rotation handling lives in :func:`_decode_with_kid_rotation`:
    on the canonical "Key not found" ValueError from authlib, the JWKS
    cache is refreshed exactly once and the verify is retried. A
    second miss is a hard 401 ``invalid_token``.
    """
    settings = get_settings()
    return await verify_jwt_for_audience(
        authorization,
        expected_audience=settings.keycloak_audience,
    )


async def keycloak_readiness_probe() -> ProbeResult:
    """Readiness probe: confirm Keycloak's JWKS endpoint is fetchable.

    Registered with :mod:`meho_backplane.health` at app startup.

    The probe issues a fresh discovery + JWKS fetch on every call. That
    is intentional: ``/ready`` should reflect *current* dependency
    health, not a stale cache state. Probes are cheap by registry
    contract (Task #19), and Keycloak's JWKS endpoint is a single
    HTTP GET against a CDN-able JSON document — a few hundred
    milliseconds in the worst case.

    Async because the JWKS fetch goes through :func:`_http_get_json`'s
    :class:`httpx.AsyncClient`. The previous sync twin
    (``_http_get_json_sync``) blocked the FastAPI event loop on every
    ``/ready`` poll — on a busy worker that's enough to starve request
    handling while the discovery + JWKS round-trips complete. Sharing
    the async client path with the request hot path also keeps a
    single transport configured the same way (timeouts, retries when
    they land in v0.2).

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
        discovery = await _http_get_json(discovery_url)
        jwks_uri = discovery.get("jwks_uri")
        if not isinstance(jwks_uri, str) or not jwks_uri:
            return ProbeResult(
                name="keycloak",
                ok=False,
                detail="jwks_uri_missing",
            )
        jwks = await _http_get_json(jwks_uri)
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
