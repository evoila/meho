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

T5 (#866) mounts the router and the middleware onto the FastAPI app;
this subpackage exposes only the build-time surface.
"""

from meho_backplane.ui.auth.middleware import (
    AUTH_PREFIX,
    STATIC_PREFIX,
    UISessionContext,
    UISessionMiddleware,
    require_ui_admin,
    require_ui_session,
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
    "STATIC_PREFIX",
    "UISessionContext",
    "UISessionMiddleware",
    "build_router",
    "require_ui_admin",
    "require_ui_session",
]
