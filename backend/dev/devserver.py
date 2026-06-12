# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Local-dev wrapper around the backplane app: real app + ``/dev/login``.

The ``/ui/*`` surface is gated by ``UISessionMiddleware`` — a session
row in ``web_session`` referenced by the ``meho_session`` cookie, minted
by the Keycloak BFF callback. Running a local Keycloak just to look at
templates is absurd, so this wrapper imports the real ``app`` and bolts
on one extra route OUTSIDE the ``/ui/`` prefix (so the middleware never
sees it):

    GET /dev/login  →  inserts a real ``web_session`` row (dev operator,
                       fixed dev tenant, dummy tokens), sets the
                       ``meho_session`` cookie WITHOUT the ``Secure``
                       attribute (plain-http localhost works in every
                       browser, including Safari), 303-redirects to /ui/.

Everything past the cookie check is the production code path — same
templates, same routes, same CSRF middleware.

Run it (from ``backend/``):

    set -a; source dev/.env.dev; set +a
    uv run python -m uvicorn dev.devserver:app --port 8800 --reload

Never deploy this module; it lives outside ``src/`` so the package
build can't pick it up.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from datetime import timedelta

from fastapi import Request, Response
from fastapi.responses import RedirectResponse

from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.main import app
from meho_backplane.ui.auth.session_store import create_session

DEV_OPERATOR_SUB = "dev-operator"
# The ``rdc-internal`` tenant migration 0018 seeds into every fresh DB —
# using it means tenant-scoped UI queries resolve a real tenant row.
DEV_TENANT_ID = uuid.UUID("71cd1935-1017-4601-9fd2-cd21b83497f1")


@app.middleware("http")
async def _dev_login_instead_of_keycloak(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    """Reroute the BFF login bounce to ``/dev/login``.

    ``UISessionMiddleware`` 302s sessionless ``/ui/*`` requests to
    ``/ui/auth/login``, which raises ``ui_oauth_not_configured`` without
    real Keycloak client credentials. Added LAST via the decorator, this
    middleware sits OUTERMOST — it catches that path before the session
    middleware runs, so opening any ``/ui/...`` URL in a fresh browser
    auto-logs-in instead of erroring.
    """
    if request.url.path == "/ui/auth/login":
        return_to = request.query_params.get("return_to", "/ui/")
        if not return_to.startswith("/ui/"):
            return_to = "/ui/"
        return RedirectResponse(f"/dev/login?return_to={return_to}", status_code=303)
    return await call_next(request)


@app.get("/dev/login", include_in_schema=False)
async def dev_login(return_to: str = "/ui/") -> RedirectResponse:
    maker = get_sessionmaker()
    async with maker() as session, session.begin():
        sess = await create_session(
            session,
            operator_sub=DEV_OPERATOR_SUB,
            tenant_id=DEV_TENANT_ID,
            access_token="dev-access-token",
            refresh_token="dev-refresh-token",
            lifetime=timedelta(days=30),
        )
    if not return_to.startswith("/ui/"):
        return_to = "/ui/"
    response = RedirectResponse(return_to, status_code=303)
    response.set_cookie(
        "meho_session",
        str(sess.id),
        httponly=True,
        samesite="lax",
        path="/",
        max_age=30 * 24 * 3600,
    )
    return response
