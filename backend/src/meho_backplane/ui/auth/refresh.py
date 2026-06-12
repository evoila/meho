# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Inline access-token refresh for BFF sessions (G0.25 #1694).

The v0.14.0 cycle-10 dogfood (FE-3) found the BFF dead-ends on raw
JSON when Keycloak's access token expires mid-session: the sliding
extension (#869) keeps the ``web_session`` row alive long past the
~5-minute token TTL, so :func:`require_ui_admin` happily loads the
row, presents the stale token to
:func:`~meho_backplane.auth.jwt.verify_jwt_for_audience`, and the
operator's browser renders ``{"detail": "token_expired"}`` instead of
a page. The :func:`~meho_backplane.ui.auth.session_store.rotate_refresh`
primitive existed from day one (T3 #864) but nothing on the request
hot path ever invoked it.

This module closes the loop with three seams:

* :func:`refresh_session_tokens` -- the locked refresh primitive.
  Re-loads the row under ``SELECT ... FOR UPDATE``, skips the
  network round-trip when a concurrent request already rotated,
  otherwise POSTs the RFC 6749 § 6 refresh grant via
  :func:`~meho_backplane.ui.auth.flow.refresh_access_token` and
  rotates the row through ``rotate_refresh`` (RFC 9700 § 4.14
  one-time-use semantics) in the same transaction. Every failure
  class maps to a structured ``ui_auth_token_refresh_failed`` event
  and a fail-closed 401 with :data:`SESSION_EXPIRED_DETAIL`.

* :func:`load_fresh_session` -- proactive leg. Loads the session and
  refreshes when the row is within
  :data:`REFRESH_EXPIRY_MARGIN_SECONDS` of ``expires_at`` (the
  issue-#1694 contract). Under the default sliding-extension config
  the row's expiry runs far ahead of the token's, so this leg mostly
  matters for ``ui_session_sliding_extension_seconds=0`` deploys and
  for sessions butting against the absolute cap.

* :func:`verify_access_token_with_refresh` -- reactive leg, and the
  one that actually clears the dogfood scenario. Verifies the stored
  access token through the chassis JWT chain; on the specific
  ``token_expired`` 401 it refreshes once and re-verifies. Any other
  verification failure propagates untouched -- a bad signature or
  audience is not an expiry problem and must stay loud.

Why both legs: the session row's ``expires_at`` is a *session*
clock, not a *token* clock. After one sliding extension the two
decouple entirely (row: hours; token: minutes), so a proactive check
keyed on the row alone can never observe token expiry. The reactive
leg keys on the authoritative signal -- the JWT chain's own verdict
-- without decoding token internals a second time.

Cookie stability (CSRF-desync guard)
------------------------------------

A refresh mutates **only** the encrypted token columns and
``expires_at`` inside the existing ``web_session`` row. The
``meho_session`` cookie value (the row id) and the ``meho_csrf``
token (HMAC-keyed on that same session id,
:func:`meho_backplane.ui.csrf.mint_csrf_token`) are untouched -- the
refresh path performs zero ``Set-Cookie`` operations. Pages rendered
before a refresh therefore keep working after it: their session
cookie still resolves the same row, and their CSRF token still
verifies. This deliberately avoids the cookie-rotation desync class
the create-modal fix (#1706) diagnosed, where re-minting a cookie
mid-session stranded already-rendered forms. The only cookie
mutation in the lifecycle is the **clear** on terminal refresh
failure (:mod:`meho_backplane.ui.auth.errors`), at which point the
session is dead and the operator re-authenticates anyway.

Failure policy
--------------

Single attempt, no retry, no backoff knobs (dumb-substrate
philosophy; the issue pins this). A failed refresh raises
``HTTPException(401, detail="session_expired")`` -- a code distinct
from the JWT chain's ``token_expired`` so the exception handler in
:mod:`meho_backplane.ui.auth.errors` can map it to a login redirect
for HTML requests without touching unrelated 401s.

References
----------

* RFC 6749 § 6 (refresh grant):
  https://www.rfc-editor.org/rfc/rfc6749#section-6
* RFC 9700 § 4.14 (refresh-token rotation BCP):
  https://datatracker.ietf.org/doc/rfc9700/
* OAuth 2.0 for Browser-Based Apps § 6 (BFF silent refresh):
  https://datatracker.ietf.org/doc/draft-ietf-oauth-browser-based-apps/
* OWASP ASVS v4 § 3.3 (idle vs absolute session timeout):
  https://owasp.org/www-project-application-security-verification-standard/
"""

from __future__ import annotations

import time
import uuid
from datetime import UTC, datetime, timedelta
from typing import Final, NoReturn

import httpx
import structlog
from authlib.integrations.base_client.errors import OAuthError
from fastapi import HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.auth.jwt import verify_jwt_for_audience
from meho_backplane.auth.operator import Operator
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.ui.auth.flow import (
    OAuthFlowConfigurationError,
    OAuthFlowError,
    TokenExchangeResult,
    refresh_access_token,
)
from meho_backplane.ui.auth.routes import SESSION_TTL_MARGIN_SECONDS
from meho_backplane.ui.auth.session_store import (
    DecryptedSession,
    RefreshReplayError,
    load_session,
    load_session_for_update,
    rotate_refresh,
)

__all__ = [
    "REFRESH_EXPIRY_MARGIN_SECONDS",
    "SESSION_EXPIRED_DETAIL",
    "load_fresh_session",
    "refresh_session_tokens",
    "verify_access_token_with_refresh",
]


#: 401 ``detail`` code raised on terminal refresh failure. Distinct
#: from the JWT chain's ``token_expired`` so the error handler in
#: :mod:`meho_backplane.ui.auth.errors` can map exactly this code to
#: a ``/ui/auth/login`` redirect for HTML requests; ``token_expired``
#: keeps its raw-JSON contract for ``/api/*`` Bearer callers.
SESSION_EXPIRED_DETAIL: Final[str] = "session_expired"

#: Proactive-refresh threshold: when the session row is within this
#: many seconds of ``expires_at``, a refresh is attempted before the
#: stored token is presented to the JWT chain. Hard-coded per the
#: issue-#1694 out-of-scope list (no operator knob); the value
#: mirrors :data:`~meho_backplane.ui.auth.routes.SESSION_TTL_MARGIN_SECONDS`
#: so the proactive window matches the slack trimmed at login.
REFRESH_EXPIRY_MARGIN_SECONDS: Final[int] = 60

#: The JWT chain's expiry detail code
#: (:func:`meho_backplane.auth.jwt._classify_decode_error`). The
#: reactive leg matches on exactly this; every other 401 code
#: (signature, audience, structure) propagates untouched.
_TOKEN_EXPIRED_DETAIL: Final[str] = "token_expired"


def _raise_session_expired(cause: BaseException | None = None) -> NoReturn:
    """Raise the fail-closed 401 every terminal refresh failure maps to."""
    exc = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=SESSION_EXPIRED_DETAIL,
    )
    if cause is not None:
        raise exc from cause
    raise exc


def _classify_refresh_failure(exc: Exception) -> tuple[str, dict[str, str]]:
    """Map a refresh-leg exception to the structured failure reason.

    Returns ``(reason, extra_log_fields)`` with ``reason`` drawn from
    the issue-#1694 contract -- ``invalid_grant`` / ``network_error``
    / ``timeout`` / ``malformed_response`` -- plus one off-contract
    code, ``oauth_not_configured``, for the (config drifted after
    login) edge where the client credentials vanished from settings.
    Ordering is load-bearing twice over: ``httpx.TimeoutException``
    subclasses ``httpx.HTTPError``, and
    :class:`OAuthFlowConfigurationError` subclasses
    :class:`OAuthFlowError` (both verified against the installed
    httpx 0.28 / flow module).

    ``extra_log_fields`` never carries token material -- only the
    exception class and, for IdP rejections, the RFC 6749 § 5.2
    ``error`` code authlib parsed off the response.
    """
    fields = {"error_class": type(exc).__name__}
    if isinstance(exc, httpx.TimeoutException):
        return "timeout", fields
    if isinstance(exc, httpx.HTTPError):
        return "network_error", fields
    if isinstance(exc, OAuthError):
        # authlib surfaces the IdP's error code (``invalid_grant`` on
        # an expired / revoked / replayed refresh token per RFC 6749
        # § 5.2). Keep the contract reason stable and put the exact
        # IdP code in the log fields.
        idp_error = getattr(exc, "error", None)
        if isinstance(idp_error, str) and idp_error:
            fields["idp_error"] = idp_error
        return "invalid_grant", fields
    if isinstance(exc, OAuthFlowConfigurationError):
        return "oauth_not_configured", fields
    # Residual: OAuthFlowError -- the response parsed but is missing
    # ``access_token`` (or is not an object at all).
    return "malformed_response", fields


async def refresh_session_tokens(
    session_id: uuid.UUID,
    *,
    stale_access_token: str,
) -> DecryptedSession:
    """Refresh the session's token pair under a row lock, fail closed.

    The single chokepoint every refresh attempt (proactive or
    reactive) funnels through. The flow, in one transaction:

    1. ``SELECT ... FOR UPDATE`` the row via
       :func:`~meho_backplane.ui.auth.session_store.load_session_for_update`.
       Concurrent refreshes on the same session serialise here -- the
       AC's "first one wins" contract.
    2. **Skip check**: when the stored access token no longer matches
       *stale_access_token*, a concurrent request already rotated
       while this one waited on the lock. Return the stored (fresh)
       pair without any network round-trip.
    3. POST the RFC 6749 § 6 refresh grant (single attempt, 5 s
       timeout, via :func:`~meho_backplane.ui.auth.flow.refresh_access_token`).
    4. Re-check ``expires_at`` against the wall clock. The row was
       alive at step 1 but the token-endpoint round-trip takes real
       time; entering ``rotate_refresh`` with an expired row would
       fire its replay branch, which commits revoke + audit side
       effects on a *dedicated* DB session -- an UPDATE that would
       wait on the very row lock this transaction holds. Failing
       closed here keeps that branch unreachable from this call site.
    5. Rotate via
       :func:`~meho_backplane.ui.auth.session_store.rotate_refresh`
       (one-time-use semantics) and extend ``expires_at`` by the new
       token's ``expires_in`` minus the login margin, capped at the
       absolute lifetime ceiling.

    Emits ``ui_auth_token_refresh_succeeded`` (session_id,
    old_expires_at, new_expires_at, time_cost_ms) after the commit,
    or ``ui_auth_token_refresh_failed`` (session_id, reason) before
    raising. No token material ever reaches a log line.

    Raises
    ------
    HTTPException(401, detail="session_expired")
        The session vanished / was revoked / expired under the lock,
        or the refresh attempt failed for any reason. The error
        handler in :mod:`meho_backplane.ui.auth.errors` maps this to
        a login redirect for HTML requests.
    """
    log = structlog.get_logger(__name__)
    started = time.monotonic()
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as db_session, db_session.begin():
        locked = await load_session_for_update(db_session, session_id)
        if locked is None:
            # Revoked (logout), hard-deleted, or past expires_at --
            # nothing to refresh. Not a refresh *failure*: no token
            # endpoint was contacted. The 401 + redirect contract is
            # the same either way.
            log.info(
                "ui_auth_token_refresh_skipped_session_gone",
                session_id=str(session_id),
            )
            _raise_session_expired()
        if locked.access_token != stale_access_token:
            # A concurrent request rotated while this one waited on
            # the row lock. The stored pair is fresh; presenting the
            # old refresh token to Keycloak now would trip the replay
            # defences for no reason.
            log.info(
                "ui_auth_token_refresh_skipped_concurrent_winner",
                session_id=str(session_id),
            )
            return locked
        old_expires_at = locked.expires_at
        tokens = await _refresh_via_idp(session_id, locked, log)
        rotated = await _rotate_or_fail(db_session, session_id, locked, tokens, log)
    # Success is logged after the transaction commits -- a commit
    # failure must not leave a phantom "succeeded" event -- and
    # time_cost_ms therefore includes the commit, which is the honest
    # end-to-end cost of the refresh on the request path.
    elapsed_ms = (time.monotonic() - started) * 1000.0
    log.info(
        "ui_auth_token_refresh_succeeded",
        session_id=str(session_id),
        old_expires_at=old_expires_at.isoformat(),
        new_expires_at=rotated.expires_at.isoformat(),
        time_cost_ms=round(elapsed_ms, 2),
    )
    return rotated


async def _refresh_via_idp(
    session_id: uuid.UUID,
    locked: DecryptedSession,
    log: structlog.stdlib.BoundLogger,
) -> TokenExchangeResult:
    """Step 3: the single token-endpoint attempt, failure-mapped.

    Every exception class the grant can raise collapses to one
    ``ui_auth_token_refresh_failed`` event (structured ``reason`` per
    :func:`_classify_refresh_failure`) and the fail-closed
    ``session_expired`` 401. No retry.
    """
    try:
        return await refresh_access_token(refresh_token=locked.refresh_token)
    except (httpx.HTTPError, OAuthError, OAuthFlowError) as exc:
        reason, extra = _classify_refresh_failure(exc)
        log.warning(
            "ui_auth_token_refresh_failed",
            session_id=str(session_id),
            reason=reason,
            **extra,
        )
        _raise_session_expired(exc)


async def _rotate_or_fail(
    db_session: AsyncSession,
    session_id: uuid.UUID,
    locked: DecryptedSession,
    tokens: TokenExchangeResult,
    log: structlog.stdlib.BoundLogger,
) -> DecryptedSession:
    """Steps 4-5: wall-clock re-check, then the RFC 9700 rotation.

    The re-check keeps ``rotate_refresh``'s replay branch unreachable
    from this call site: that branch commits revoke + audit side
    effects on a *dedicated* DB session -- an UPDATE that would wait
    on the very row lock this transaction holds (see the
    :func:`refresh_session_tokens` docstring, step 4).
    """
    if locked.expires_at <= datetime.now(UTC):
        # The session crossed expires_at during the token-endpoint
        # round-trip.
        log.warning(
            "ui_auth_token_refresh_failed",
            session_id=str(session_id),
            reason="session_expired_during_refresh",
        )
        _raise_session_expired()
    new_lifetime = timedelta(
        seconds=max(tokens.expires_in - SESSION_TTL_MARGIN_SECONDS, 60),
    )
    try:
        return await rotate_refresh(
            db_session,
            session_id,
            presented_refresh=locked.refresh_token,
            new_access_token=tokens.access_token,
            new_refresh_token=tokens.refresh_token,
            new_lifetime=new_lifetime,
        )
    except RefreshReplayError as exc:
        # Defensive only: mismatch is impossible (the presented
        # value was decrypted under this very lock), revocation
        # is blocked by the lock, and expiry was re-checked
        # above. Kept so a future regression fails closed as a
        # 401 instead of a 500.
        log.warning(
            "ui_auth_token_refresh_failed",
            session_id=str(session_id),
            reason="rotation_replay",
            audit_id=str(exc.audit_id),
        )
        _raise_session_expired(exc)


async def load_fresh_session(session_id: uuid.UUID) -> DecryptedSession | None:
    """Load the session, proactively refreshing when near expiry.

    The drop-in replacement for the bare ``load_session`` call in
    :func:`~meho_backplane.ui.auth.middleware.require_ui_admin` (and
    the seam the dashboard-feed session proxy, #1696, builds on):
    returns the decrypted session, refreshing the token pair first
    when the row is within :data:`REFRESH_EXPIRY_MARGIN_SECONDS` of
    ``expires_at``.

    Returns ``None`` when the session is missing / revoked / expired
    -- the caller keeps its existing "treat as unauthenticated"
    branch. A near-expiry row whose refresh *fails* raises the
    ``session_expired`` 401 from :func:`refresh_session_tokens`
    instead of returning the soon-to-die row: serving one last page
    on a token about to expire mid-render is exactly the half-broken
    state #1694 exists to remove.

    Note the deliberately small blast radius: the proactive leg keys
    on the **row's** clock. Under the default sliding extension the
    row outlives the access token by design, so callers that need the
    token to actually verify must pair this with
    :func:`verify_access_token_with_refresh` (the reactive leg).
    """
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as db_session, db_session.begin():
        decrypted = await load_session(db_session, session_id)
    if decrypted is None:
        return None
    margin = timedelta(seconds=REFRESH_EXPIRY_MARGIN_SECONDS)
    if decrypted.expires_at - datetime.now(UTC) <= margin:
        return await refresh_session_tokens(
            session_id,
            stale_access_token=decrypted.access_token,
        )
    return decrypted


async def verify_access_token_with_refresh(
    decrypted: DecryptedSession,
    *,
    expected_audience: str,
) -> tuple[DecryptedSession, Operator]:
    """Verify the stored access token, silently refreshing on expiry.

    The reactive leg -- the one that clears the FE-3 dogfood
    scenario. Presents the stored token to the chassis JWT chain; on
    the specific ``token_expired`` 401 it refreshes once via
    :func:`refresh_session_tokens` and re-verifies the rotated token.
    Returns the (possibly rotated) session alongside the verified
    :class:`~meho_backplane.auth.operator.Operator` so the caller's
    role gate runs against claims that match the stored token.

    Every other verification failure (bad signature, wrong audience,
    structural break) propagates untouched: those are not expiry
    conditions, a refresh would not repair them, and masking them
    behind a login redirect would hide a real misconfiguration.

    If the **re**-verification still reports ``token_expired`` --
    Keycloak clock skew, or a concurrent-winner token that itself
    aged out -- the failure converts to the ``session_expired`` 401
    rather than leaking raw JSON at an HTML browser; the JWT chain
    has already logged the precise classifier event by then.
    """
    bearer = f"Bearer {decrypted.access_token}"
    try:
        operator = await verify_jwt_for_audience(
            bearer,
            expected_audience=expected_audience,
        )
        return decrypted, operator
    except HTTPException as exc:
        if exc.status_code != status.HTTP_401_UNAUTHORIZED or exc.detail != _TOKEN_EXPIRED_DETAIL:
            raise
    refreshed = await refresh_session_tokens(
        decrypted.id,
        stale_access_token=decrypted.access_token,
    )
    log = structlog.get_logger(__name__)
    try:
        operator = await verify_jwt_for_audience(
            f"Bearer {refreshed.access_token}",
            expected_audience=expected_audience,
        )
    except HTTPException as exc:
        if exc.status_code == status.HTTP_401_UNAUTHORIZED and exc.detail == _TOKEN_EXPIRED_DETAIL:
            log.warning(
                "ui_auth_token_refresh_stale_after_rotation",
                session_id=str(refreshed.id),
            )
            _raise_session_expired(exc)
        raise
    return refreshed, operator
