# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Drift-gated access-token revalidation for the BFF read path.

The ``/ui/*`` read surfaces authenticate off the decrypted
``web_session`` row alone: :class:`~meho_backplane.ui.auth.middleware.
UISessionMiddleware` loads the row and every read-render handler takes
its identity from :func:`~meho_backplane.ui.auth.middleware.
require_ui_session` without ever re-presenting the stored access token
to the IdP-backed JWT chain. Because the row's ``expires_at`` slides
forward on active use (up to the absolute lifetime ceiling,
``ui_session_absolute_lifetime_seconds``, default 10 h), a session
whose IdP-side grant has been revoked keeps rendering authorized reads
until the absolute ceiling -- token re-validation only ran on the
write/admin path (``require_ui_admin``) and the per-route operator
lifts.

This module bounds that lag. :func:`revalidate_read_session` is called
by the session middleware after every successful row load and re-runs
the stored token through
:func:`~meho_backplane.ui.auth.refresh.verify_access_token_with_refresh`
-- the same reactive-refresh seam the write path uses -- whenever the
session has not been revalidated within
``ui_session_read_revalidation_seconds`` (default 300). A failed
revalidation is treated exactly like a missing session: the middleware
redirects to login.

Accepted lag window
-------------------

On a JWKS cache hit the revalidation is a local signature + claims
check -- **no outbound Keycloak call**. Revocation at the IdP does not
invalidate an already-issued JWT, so revocation (and role demotion)
becomes visible only when the refresh grant is next presented -- which
happens the first time revalidation observes the access token past its
``exp``. The worst-case read-path revocation lag is therefore::

    access-token TTL  +  ui_session_read_revalidation_seconds

(~5 min Keycloak default + 300 s default = ~10 minutes), instead of
the previous bound of ``ui_session_absolute_lifetime_seconds`` (10 h).
Deployments wanting a tighter bound set
``UI_SESSION_READ_REVALIDATION_SECONDS=0`` (revalidate on every read
request) and/or shorten the IdP access-token TTL.

Why the drift anchor is *last validated*, not ``last_seen_at``
--------------------------------------------------------------

``web_session.last_seen_at`` is bumped on **every** successful load,
so a gate keyed on the gap since the previous request never fires for
a continuously active session -- exactly the session whose revocation
lag needs bounding. The anchor is instead the last time *this process*
proved the stored token valid, held in a process-local map. The map is
deliberately not persisted, and it is bounded: a session with no
anchor in this process -- first sight after a restart, or an anchor
dropped when the map was full -- is revalidated on its next read. A
missing anchor therefore only ever adds a check (one in-memory
signature check on a JWKS cache hit), never skips one. Creating a
session row seeds its anchor -- the login callback creates rows only
from a token it verified -- so the first reads after login stay
token-free. Multi-replica deployments revalidate per replica, which
only tightens the bound.

References
----------

* OWASP ASVS v4 § 3.3 (idle vs absolute session timeout, prompt
  revocation): https://owasp.org/www-project-application-security-verification-standard/
* OWASP Session Management Cheat Sheet (session expiration):
  https://cheatsheetseries.owasp.org/cheatsheets/Session_Management_Cheat_Sheet.html
* OAuth 2.0 for Browser-Based Apps BCP (BFF token handling):
  https://datatracker.ietf.org/doc/draft-ietf-oauth-browser-based-apps/
"""

from __future__ import annotations

import heapq
import time
import uuid
from typing import TYPE_CHECKING, Final

import structlog
from fastapi import HTTPException

from meho_backplane.settings import get_settings
from meho_backplane.ui.auth.refresh import verify_access_token_with_refresh
from meho_backplane.ui.auth.session_ref import session_ref
from meho_backplane.ui.auth.tenant import TenantRemovedError

if TYPE_CHECKING:
    from meho_backplane.ui.auth.session_store import DecryptedSession

__all__ = [
    "mark_session_validated",
    "reset_read_revalidation_cache_for_testing",
    "revalidate_read_session",
]


#: Upper bound on tracked sessions. Sized an order of magnitude above
#: any realistic concurrent-operator count; the map only ever grows by
#: one entry per *live* session id that this process validated. When
#: the bound is exceeded the oldest anchors are dropped, never the
#: whole map, and a session without an anchor is revalidated on its
#: next read -- so a dropped anchor only adds a check, never skips one.
_MAX_TRACKED_SESSIONS: Final[int] = 4096

#: session id -> ``time.monotonic()`` instant of the last successful
#: token validation in this process. Monotonic (not wall-clock) so the
#: drift gate is immune to system-clock steps. Process-local by design
#: -- see the module docstring's anchor rationale.
_last_validated: dict[uuid.UUID, float] = {}


def reset_read_revalidation_cache_for_testing() -> None:
    """Drop every revalidation anchor. Test-only."""
    _last_validated.clear()


def mark_session_validated(session_id: uuid.UUID) -> None:
    """Record that this process just proved the session's token valid.

    The session store calls this when it creates a row: the login
    callback creates rows only from a token it verified through the
    JWT chain, so the first reads after login render without a second
    check. Read-path revalidation records its own successes itself.
    """
    _mark_validated(session_id, time.monotonic())


def _mark_validated(session_id: uuid.UUID, now_mono: float) -> None:
    """Record a validation instant; drop the oldest anchors on overflow."""
    _last_validated[session_id] = now_mono
    excess = len(_last_validated) - _MAX_TRACKED_SESSIONS
    if excess <= 0:
        return
    for sid in heapq.nsmallest(excess, _last_validated, key=_last_validated.__getitem__):
        del _last_validated[sid]


async def revalidate_read_session(
    decrypted: DecryptedSession,
) -> DecryptedSession | None:
    """Re-validate the stored access token when the drift gate is due.

    Called by :class:`~meho_backplane.ui.auth.middleware.
    UISessionMiddleware` after every successful session-row load.
    Behaviour, keyed on ``ui_session_read_revalidation_seconds``:

    * Anchor fresher than the threshold -> return *decrypted*
      unchanged (zero added cost beyond one dict read).
    * No anchor in this process (first sight after a restart, or the
      anchor was dropped when the map was full), anchor stale, or
      threshold == 0 (the strict revalidate-every-request mode) ->
      present the stored token to
      :func:`~meho_backplane.ui.auth.refresh.verify_access_token_with_refresh`.
      On a JWKS cache hit that is an in-memory signature check; only
      when the token is past ``exp`` does the reactive leg round-trip
      Keycloak's token endpoint -- the point where IdP-side revocation
      or role demotion surfaces as ``invalid_grant``.

    Returns the (possibly rotated) session on success, or ``None``
    when validation failed for any reason -- the caller treats ``None``
    exactly like a missing session and redirects to login. Failing
    closed on *every* ``HTTPException`` (not just ``session_expired``)
    is deliberate: on the read path a signature or audience failure of
    the *stored* token means the session is unusable, and bouncing to
    login is strictly safer than rendering; a systemic misconfiguration
    still surfaces loudly because the login round-trip itself validates
    the fresh token through the same chain.

    One exception passes through: :class:`~meho_backplane.ui.auth.tenant.TenantRemovedError`
    (#5240). The token no longer allows the session's tenant, the row
    was already moved, and the middleware sends the person to the
    tenant picker instead of to login.
    """
    settings = get_settings()
    threshold = float(settings.ui_session_read_revalidation_seconds)
    now_mono = time.monotonic()
    anchor = _last_validated.get(decrypted.id)
    if anchor is not None and now_mono - anchor < threshold:
        return decrypted

    log = structlog.get_logger(__name__)
    try:
        refreshed, _operator = await verify_access_token_with_refresh(
            decrypted,
            expected_audience=settings.keycloak_audience,
        )
    except TenantRemovedError:
        _last_validated.pop(decrypted.id, None)
        raise
    except HTTPException as exc:
        _last_validated.pop(decrypted.id, None)
        # No token material -- only the classifier's detail code.
        log.warning(
            "ui_read_session_revalidation_failed",
            session_ref=session_ref(decrypted.id),
            status_code=exc.status_code,
            detail=str(exc.detail),
        )
        return None
    _mark_validated(decrypted.id, now_mono)
    return refreshed
