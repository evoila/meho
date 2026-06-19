# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Retrieval-diagnostics UI routes: in-process hybrid query + per-signal breakdown.

Initiative #1840 (G10.14 Retrieval diagnostics & quality console), Task #1888.

``GET /ui/retrieval`` renders the page: a tabbed shell (Diagnostics tab
default-active; the Usage / Eval / Retire tabs are placeholders T2/T3 fill
in) with a query form (a query textarea, optional ``source`` / ``kind``
inputs, a ``limit`` selector) and an empty ``#retrieval-diagnostics-results``
region. ``POST /ui/retrieval/diagnostics`` is the HTMX fragment endpoint --
it reconstructs the session operator, binds the privacy ``audit_*``
contextvars, runs the in-process :func:`~meho_backplane.retrieval.retriever.retrieve`
service, and swaps ``retrieval/_diagnostics_results.html`` (one card per hit,
each with the ``fused_score`` and the per-signal RRF score/rank breakdown)
into ``#retrieval-diagnostics-results``.

Reusing the substrate (no new ``/api/v1`` endpoint)
---------------------------------------------------

The page does not add a REST endpoint. It calls the same in-process
:func:`~meho_backplane.retrieval.retriever.retrieve` service the
``POST /api/v1/retrieve`` route fronts (``api/v1/retrieve.py``), tenant-scoped
to ``operator.tenant_id`` with the mandatory per-principal isolation predicate
(``principal_sub=operator.sub``). Routing through the service -- not a
self-HTTP call -- keeps the in-process audit binding and avoids a network hop,
exactly as ``ui/routes/corpus/routes.py`` fronts ``search_docs``.

Audit / privacy binding (load-bearing)
--------------------------------------

The REST handler binds the privacy ``audit_*`` contextvars **before** calling
``retrieve`` so the audit_log row carries a privacy-preserving query trace even
on a mid-retrieval exception. This handler reproduces that binding: it binds
``audit_query_hash`` (the SHA-256 hex digest of the raw query via the same
:func:`~meho_backplane.api.v1.retrieve._compute_query_hash` encoding contract
-- never the raw query), ``audit_source``, ``audit_kind`` before the call and
``audit_hit_count`` after. The raw query is deliberately ephemeral.

Operator reconstruction
-----------------------

The :class:`~meho_backplane.ui.auth.middleware.UISessionContext` the
``require_ui_session`` dependency hands route handlers carries only
``operator_sub`` / ``tenant_id`` -- not the full
:class:`~meho_backplane.auth.operator.Operator` (with its ``sub`` for the
per-principal predicate). The handler reconstructs the operator from the
session via the same proven seam :func:`_resolve_operator` mirrors from the
corpus console: load the decrypted session, present its (silently-refreshed)
access token to the chassis JWT chain via
:func:`~meho_backplane.ui.auth.refresh.verify_access_token_with_refresh`.

CSRF
----

The diagnostics form declares its own ``hx-headers`` ``X-CSRF-Token`` (HTMX
does not propagate ``hx-headers`` to child elements). ``GET /ui/retrieval``
mints the token and sets the ``meho_csrf`` cookie. The fragment swaps only
``#retrieval-diagnostics-results`` and leaves the form in place, so it
**reuses** the live cookie token (cookie untouched) rather than rotating a
fresh one out from under the un-swapped form -- the cookie-rotation desync the
corpus surface diagnosed (#1754). A missing / invalid cookie falls back to a
fresh mint + re-set. See :func:`_resolve_diagnostics_csrf`.

Tenant scoping + RBAC
---------------------

``operator`` role minimum (enforced by ``require_ui_session``; matches the
OPERATOR floor the ``POST /api/v1/retrieve`` route carries). Diagnostics is
**own-tenant only** -- tenant identity is derived from ``session.tenant_id``
via the reconstructed operator, there is no ``tenant_filter`` (that is the
sibling T3 concern). The whole surface is read-only -- no confirm gates.
"""

from __future__ import annotations

import hashlib
import time
from typing import Final

import structlog
from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse

from meho_backplane.auth.operator import Operator
from meho_backplane.retrieval.retriever import (
    CANDIDATE_LIMIT,
    RetrievalHit,
    retrieve,
)
from meho_backplane.settings import get_settings
from meho_backplane.ui.auth.middleware import UISessionContext, require_ui_session
from meho_backplane.ui.auth.refresh import (
    load_fresh_session,
    verify_access_token_with_refresh,
)
from meho_backplane.ui.csrf import CSRF_COOKIE_NAME, mint_csrf_token, verify_csrf_token
from meho_backplane.ui.templating import get_templates

__all__ = ["build_retrieval_router"]

log = structlog.get_logger(__name__)

#: Maximum query length accepted by the diagnostics fragment. Mirrors the
#: backend ``RetrieveRequest.query`` cap (max 2000 chars) so an oversized
#: free-form query is rejected at the form boundary (FastAPI 422) rather than
#: forwarded.
_MAX_QUERY_LENGTH: Final[int] = 2000

#: Maximum ``source`` / ``kind`` filter length. Mirrors the backend
#: ``RetrieveRequest.source`` / ``.kind`` caps (max 64 chars).
_MAX_FILTER_LENGTH: Final[int] = 64

#: Maximum ``limit`` the selector offers. Mirrors the backend
#: ``RetrieveRequest.limit`` cap (1--50). The form renders a fixed set of
#: options; an out-of-range value is clamped at the handler boundary.
_MAX_LIMIT: Final[int] = 50

#: Default ``limit``. Mirrors the backend ``RetrieveRequest.limit`` default.
_DEFAULT_LIMIT: Final[int] = 10

#: The ``limit`` options the selector renders.
_LIMIT_OPTIONS: Final[tuple[int, ...]] = (5, 10, 20, 50)

#: Module-level ``Depends`` closure for the operator-session gate. Built once
#: (rather than inline) to satisfy ruff B008, matching the convention the
#: corpus / kb / dashboard routes established.
_require_session = Depends(require_ui_session)


async def _resolve_operator(session: UISessionContext) -> Operator:
    """Reconstruct the full :class:`Operator` from the BFF session.

    Loads the decrypted session row and presents its access token to the
    chassis JWT chain (silently refreshing on the ``token_expired`` 401) via
    :func:`~meho_backplane.ui.auth.refresh.verify_access_token_with_refresh`
    -- the same seam the corpus console + ``require_ui_admin`` use to surface
    the operator's claims. The returned operator carries the ``sub`` the
    per-principal isolation predicate reads and the ``tenant_id`` retrieval is
    scoped to.

    Raises :class:`fastapi.HTTPException` 401 when the session has been
    revoked / expired in the gap since the middleware check (the BFF error
    handler maps the ``session_expired`` detail to a login redirect for HTML
    requests).
    """
    decrypted = await load_fresh_session(session.session_id)
    if decrypted is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="ui_session_required",
        )
    settings = get_settings()
    _refreshed, operator = await verify_access_token_with_refresh(
        decrypted,
        expected_audience=settings.keycloak_audience,
    )
    return operator


def _compute_query_hash(query: str) -> str:
    """SHA-256 hex digest of *query* (UTF-8 encoded).

    Byte-equivalent to :func:`meho_backplane.api.v1.retrieve._compute_query_hash`
    so the in-process audit row this UI surface produces correlates with the
    REST surface's ``audit_log.payload.query_hash`` for the same query string.
    The raw query is never bound to the audit payload -- only this hash.
    """
    return hashlib.sha256(query.encode("utf-8")).hexdigest()


def _set_csrf_cookie(response: HTMLResponse, csrf_token: str) -> None:
    """Set the ``meho_csrf`` double-submit cookie on *response*.

    The cookie value MUST equal the token the rendered markup echoes via
    ``hx-headers`` -- the CSRF middleware rejects a mismatch
    (``value_mismatch``). Mirrors the SameSite=Strict + Secure + non-HttpOnly
    posture every UI surface's CSRF cookie carries (HTMX must read it to
    populate ``X-CSRF-Token``; the HMAC binding to the session id defeats the
    cookie-injection vector JS-read would otherwise open).
    """
    response.set_cookie(
        key=CSRF_COOKIE_NAME,
        value=csrf_token,
        httponly=False,
        secure=True,
        samesite="strict",
        path="/ui",
    )


def _resolve_diagnostics_csrf(request: Request, session_id: str) -> tuple[str, bool]:
    """Pick the CSRF token the diagnostics fragment echoes + whether to set the cookie.

    Returns ``(token, set_cookie)``. The fragment swaps only
    ``#retrieval-diagnostics-results`` -- the diagnostics ``<form>`` that
    carries the ``hx-headers`` ``X-CSRF-Token`` lives in ``index.html`` and is
    **not** re-rendered. So the rule (mirroring the corpus surface's
    ``_resolve_search_csrf``, the #1754 fix) is:

    * **Live cookie present + valid** -- *reuse* that token and do **not**
      re-set the cookie. Minting a fresh token + ``Set-Cookie`` here would
      rotate the cookie out from under the un-swapped form, whose still-rendered
      ``hx-headers`` snapshot then carries the old token while the cookie holds
      the new one -- the ``value_mismatch`` 403 the cookie-rotation desync class
      produces. The CSRF token is stateless and HMAC-bound to the session, so
      the same token validates on every diagnostics run until logout.
    * **Cookie missing / invalid** -- defensive fallback: mint a fresh token and
      re-set the cookie so a direct fragment fetch with no prior full-page load
      (or a tampered cookie) still establishes a working double-submit pair.

    The reuse is gated on :func:`verify_csrf_token` so a foreign or tampered
    cookie value never gets echoed back as the fragment's token.
    """
    existing = request.cookies.get(CSRF_COOKIE_NAME)
    if existing and verify_csrf_token(session_id, existing):
        return existing, False
    return mint_csrf_token(session_id), True


def _clamp_limit(raw: int) -> int:
    """Clamp a submitted ``limit`` into the backend-accepted 1--50 range.

    The selector only renders in-range options, but a forged form post could
    carry an out-of-range value; clamping (rather than 422-ing) keeps the
    diagnostics tool forgiving for an operator while never forwarding a value
    the substrate would reject.
    """
    if raw < 1:
        return 1
    if raw > _MAX_LIMIT:
        return _MAX_LIMIT
    return raw


def build_retrieval_router() -> APIRouter:
    """Construct the ``GET /ui/retrieval`` + ``POST /ui/retrieval/diagnostics`` router.

    Factory function (not a module-level constant) so a test app can construct
    parallel routers without shared route state -- the same convention the
    corpus / kb / dashboard surface routers follow.
    """
    router = APIRouter(tags=["ui-retrieval"])

    @router.get("/ui/retrieval", response_class=HTMLResponse)
    async def retrieval_index(
        request: Request,
        session: UISessionContext = _require_session,
    ) -> HTMLResponse:
        """Render the retrieval page (delegates to :func:`_render_retrieval_index`)."""
        return await _render_retrieval_index(request, session)

    # NOTE: the literal ``POST /ui/retrieval/diagnostics`` route is registered
    # here, ahead of any future ``/ui/retrieval/{param}`` route on this
    # surface, so first-match-wins routing never binds the literal
    # ``diagnostics`` segment as a slug parameter -- the same ordering
    # discipline the corpus / kb / scheduler routers document. The T2/T3 tabs
    # are client-side toggles on this one page, so no per-tab param route
    # exists today, but the ordering invariant is pinned now for when one does.

    @router.post("/ui/retrieval/diagnostics", response_class=HTMLResponse)
    async def retrieval_diagnostics(
        request: Request,
        session: UISessionContext = _require_session,
        query: str = Form(default="", max_length=_MAX_QUERY_LENGTH),
        source: str = Form(default="", max_length=_MAX_FILTER_LENGTH),
        kind: str = Form(default="", max_length=_MAX_FILTER_LENGTH),
        limit: int = Form(default=_DEFAULT_LIMIT),
    ) -> HTMLResponse:
        """Run the diagnostics fragment (delegates to :func:`_render_diagnostics`)."""
        return await _render_diagnostics(request, session, query, source, kind, limit)

    return router


def _diagnostics_form_context(
    *,
    query: str,
    source: str,
    kind: str,
    limit: int,
    csrf_token: str,
) -> dict[str, object]:
    """Shared form-state context for the index render + the fragment re-render."""
    return {
        "query": query,
        "source": source,
        "kind": kind,
        "limit": limit,
        "limit_options": _LIMIT_OPTIONS,
        "csrf_token": csrf_token,
    }


async def _render_retrieval_index(
    request: Request,
    session: UISessionContext,
) -> HTMLResponse:
    """Render the retrieval page for ``GET /ui/retrieval``.

    Renders the tabbed shell with the Diagnostics tab active and an empty
    results region. Mints a CSRF token and sets the ``meho_csrf`` cookie so the
    diagnostics form's ``hx-headers`` echo passes the double-submit check.
    """
    csrf_token = mint_csrf_token(str(session.session_id))
    context: dict[str, object] = {
        **_diagnostics_form_context(
            query="",
            source="",
            kind="",
            limit=_DEFAULT_LIMIT,
            csrf_token=csrf_token,
        ),
        "hits": [],
        "searched": False,
        "candidate_limit": CANDIDATE_LIMIT,
        "operator_sub": session.operator_sub,
        "active_surface": "retrieval",
        "page_title": "Retrieval",
    }
    response = get_templates().TemplateResponse(request, "retrieval/index.html", context)
    _set_csrf_cookie(response, csrf_token)
    return response


async def _render_diagnostics(
    request: Request,
    session: UISessionContext,
    query: str,
    source: str,
    kind: str,
    limit: int,
) -> HTMLResponse:
    """Run the HTMX diagnostics fragment for ``POST /ui/retrieval/diagnostics``.

    Swaps ``retrieval/_diagnostics_results.html`` into
    ``#retrieval-diagnostics-results``. A successful run renders one card per
    hit (body excerpt + ``source`` / ``source_id`` / ``kind``, the
    ``fused_score``, and a per-signal breakdown where a ``None`` rank renders
    an explicit "absent from this signal's top-N" marker); an empty hit list
    renders a "no matches" state.

    The privacy ``audit_*`` contextvars are bound **before** the ``retrieve``
    call (and ``audit_hit_count`` after) so the in-process audit row carries the
    SHA-256 query trace even on a mid-retrieval exception. CSRF handling defers
    to :func:`_resolve_diagnostics_csrf`.
    """
    query_text = query.strip()
    source_filter = source.strip()
    kind_filter = kind.strip()
    clamped_limit = _clamp_limit(limit)

    hits: list[RetrievalHit] = []
    duration_ms: float | None = None
    error_message: str | None = None
    if query_text:
        operator = await _resolve_operator(session)
        try:
            hits, duration_ms = await _run_diagnostics(
                operator, query_text, source_filter, kind_filter, clamped_limit
            )
        except HTTPException:
            # A 401 from the operator-reconstruction seam is an auth condition,
            # not a search failure -- let it propagate (the BFF maps it to a
            # login redirect), matching the corpus surface.
            raise

    csrf_token, set_csrf = _resolve_diagnostics_csrf(request, str(session.session_id))
    context: dict[str, object] = {
        **_diagnostics_form_context(
            query=query_text,
            source=source_filter,
            kind=kind_filter,
            limit=clamped_limit,
            csrf_token=csrf_token,
        ),
        "hits": hits,
        "searched": bool(query_text),
        "duration_ms": duration_ms,
        "candidate_limit": CANDIDATE_LIMIT,
        "error_message": error_message,
    }
    response = get_templates().TemplateResponse(
        request, "retrieval/_diagnostics_results.html", context
    )
    if set_csrf:
        _set_csrf_cookie(response, csrf_token)
    return response


async def _run_diagnostics(
    operator: Operator,
    query: str,
    source: str,
    kind: str,
    limit: int,
) -> tuple[list[RetrievalHit], float]:
    """Bind the privacy audit contextvars, then run in-process ``retrieve``.

    Reproduces the ``POST /api/v1/retrieve`` audit binding (``retrieve.py``):
    binds ``audit_query_hash`` (SHA-256 hex of the raw query, never the raw
    query), ``audit_source``, ``audit_kind`` **before** the retrieval call so
    the audit_log row's ``payload`` carries the privacy-preserving query trace
    even on a mid-retrieval exception, then ``audit_hit_count`` after. Tenant-
    scoped to ``operator.tenant_id`` with the mandatory per-principal isolation
    predicate (``principal_sub=operator.sub``) -- own-tenant only, no
    tenant_filter. Returns ``(hits, duration_ms)``.
    """
    start = time.monotonic()
    query_hash = _compute_query_hash(query)
    structlog.contextvars.bind_contextvars(
        audit_query_hash=query_hash,
        audit_source=source or None,
        audit_kind=kind or None,
    )
    hits = await retrieve(
        tenant_id=operator.tenant_id,
        query=query,
        source=source or None,
        kind=kind or None,
        limit=limit,
        principal_sub=operator.sub,
    )
    structlog.contextvars.bind_contextvars(audit_hit_count=len(hits))
    duration_ms = round((time.monotonic() - start) * 1000, 2)
    log.info(
        "ui_retrieval_diagnostics_completed",
        operator_sub=operator.sub,
        tenant_id=str(operator.tenant_id),
        source=source or None,
        kind=kind or None,
        hit_count=len(hits),
        duration_ms=duration_ms,
    )
    return hits, duration_ms
