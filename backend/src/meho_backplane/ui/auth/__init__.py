# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""BFF (Backend-for-Frontend) auth for the operator console.

Initiative #337 (G10.0 Frontend chassis) splits the BFF surface
across four submodules so the layering is grep-explicit:

* :mod:`meho_backplane.ui.auth.session_store` (Task #864) -- the
  encrypted token-custody substrate. ``create_session`` /
  ``load_session`` / ``revoke_session`` /
  ``rotate_refresh`` against the ``web_session`` Postgres table.
* :mod:`meho_backplane.ui.auth.flow` (Task #865) -- OAuth 2.1
  Authorization Code + PKCE client primitives.
  ``build_authorization_request`` mints the IdP redirect URL +
  registers the PKCE verifier server-side; ``exchange_code_for_tokens``
  finishes the round-trip at the token endpoint.
  ``resolve_oidc_endpoints`` exposes the cached discovery doc.
* :mod:`meho_backplane.ui.auth.routes` (Task #865) -- the FastAPI
  :class:`APIRouter` carrying ``GET /ui/auth/{login,callback,logout}``.
  ``build_router`` returns the router; ``SESSION_COOKIE_NAME`` /
  ``LOGIN_PATH`` are exported for the middleware to share.
* :mod:`meho_backplane.ui.auth.middleware` (Task #865) -- the pure-ASGI
  :class:`UISessionMiddleware` that loads operator identity from the
  ``meho_session`` cookie on every ``/ui/*`` request and 302-redirects
  to login on missing/expired session.
* :mod:`meho_backplane.ui.auth.refresh` (G0.25 #1694) -- the inline
  token-refresh lifecycle. ``load_fresh_session`` (proactive,
  row-near-expiry), ``verify_access_token_with_refresh`` (reactive,
  on ``token_expired``), and ``refresh_session_tokens`` (the locked
  RFC 6749 § 6 + RFC 9700 § 4.14 chokepoint both legs share).
* :mod:`meho_backplane.ui.auth.revalidation` -- drift-gated read-path
  token revalidation. The session middleware calls
  ``revalidate_read_session`` after every successful row load; past
  ``ui_session_read_revalidation_seconds`` without a validation, the
  stored access token is re-presented to the JWT chain, bounding
  IdP revocation / role-demotion lag on the read surfaces to
  ~(access-token TTL + threshold) instead of the absolute session
  lifetime.
* :mod:`meho_backplane.ui.auth.errors` (G0.25 #1694) -- the
  app-level exception handler that maps the refresh path's terminal
  ``session_expired`` 401 to a ``/ui/auth/login`` redirect for HTML
  requests (JSON callers keep the structured body).

T5 (#866) mounts the router and the middleware onto the FastAPI app;
:mod:`meho_backplane.main` additionally registers the exception
handler; this subpackage exposes only the build-time surface.
"""

from meho_backplane.ui.auth.errors import ui_session_expired_exception_handler
from meho_backplane.ui.auth.middleware import (
    AUTH_PREFIX,
    STATIC_PREFIX,
    UISessionContext,
    UISessionMiddleware,
    require_ui_admin,
    require_ui_session,
)
from meho_backplane.ui.auth.refresh import (
    SESSION_EXPIRED_DETAIL,
    load_fresh_session,
    refresh_session_tokens,
    verify_access_token_with_refresh,
)
from meho_backplane.ui.auth.routes import (
    LOGIN_PATH,
    SESSION_COOKIE_NAME,
    build_router,
)

__all__ = [
    "AUTH_PREFIX",
    "LOGIN_PATH",
    "SESSION_COOKIE_NAME",
    "SESSION_EXPIRED_DETAIL",
    "STATIC_PREFIX",
    "UISessionContext",
    "UISessionMiddleware",
    "build_router",
    "load_fresh_session",
    "refresh_session_tokens",
    "require_ui_admin",
    "require_ui_session",
    "ui_session_expired_exception_handler",
    "verify_access_token_with_refresh",
]
