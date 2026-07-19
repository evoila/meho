# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Retrieval UI routes: in-process diagnostics + usage + eval + retire, no REST endpoint.

Initiative #1840 (G10.14 Retrieval diagnostics & quality console), Tasks #1888
(Diagnostics) + #1889 (Usage Analytics + Eval Quality) + #1890 (Retire
Checklist).

``GET /ui/retrieval`` renders the page: a tabbed shell (Diagnostics tab
default-active; Usage Analytics + Eval Quality + Retire Checklist all live) with
the diagnostics query form and an empty ``#retrieval-diagnostics-results``
region. Four HTMX fragment endpoints front the four in-process services -- none
adds a ``/api/v1`` endpoint, exactly as ``ui/routes/corpus/routes.py`` fronts
``search_docs``:

* ``POST /ui/retrieval/diagnostics`` -- reconstructs the session operator, binds
  the privacy ``audit_*`` contextvars, runs in-process
  :func:`~meho_backplane.retrieval.retriever.retrieve`, and swaps
  ``retrieval/_diagnostics_results.html`` (one card per hit, each with the
  ``fused_score`` and the per-signal RRF score/rank breakdown) into
  ``#retrieval-diagnostics-results``.
* ``POST /ui/retrieval/usage`` (#1889) -- runs in-process
  :func:`~meho_backplane.retrieval.usage.compute_usage` own-tenant over a
  ``since`` window (opening a read-only session via
  :func:`~meho_backplane.db.engine.get_sessionmaker`, which ``compute_usage``
  requires), and swaps ``retrieval/_usage_results.html`` with the per-day /
  per-surface buckets, ``total_searches``, and -- load-bearing -- the
  ``rest_excluded`` / ``counted_surfaces`` honesty-gap explainer so a
  ``total_searches=0`` reads as "REST excluded", not "no activity". A malformed
  ``since`` (:class:`~meho_backplane.retrieval.usage.SinceValueError`) renders an
  inline 400 error card, never a 500.
* ``POST /ui/retrieval/eval`` (#1889) -- runs in-process
  :func:`~meho_backplane.retrieval.eval.eval_all` own-tenant with **no
  baseline** (the server-side baseline path is 501 today), and swaps
  ``retrieval/_eval_results.html`` with per-surface ``precision_at_5`` / ``mrr``
  / ``coverage`` + a green/yellow/red verdict pill (mapped in the template from
  the verdict token the service returns verbatim) + the ``overall_verdict``.
* ``POST /ui/retrieval/retire-checklist`` (#1890) -- runs in-process
  :func:`~meho_backplane.retrieval.retire.compute_retire_checklist` over every
  :data:`~meho_backplane.retrieval.retire.SURFACE_VERDICT_ORDER` surface (kb,
  memory, operations), opening a read-only session via
  :func:`~meho_backplane.db.engine.get_sessionmaker` (the service is keyword-only
  and **requires** a ``session``; it runs ``eval_all`` internally for criteria
  3+4, so the handler does not). Swaps ``retrieval/_retire_results.html`` with
  the three-state ``overall_verdict`` pill heading the fragment and, per surface,
  a three-state verdict pill + the five-criterion table (``name``,
  green/yellow/red dot, ``observed_value``, ``threshold_summary``, ``notes``
  rendered verbatim). The surface is **read-only** -- no purge / dry-run /
  execute-retirement affordance exists; the only POST is this checklist run.

  Tenant scoping differs from the sibling tabs: a **platform admin** may audit
  any tenant's retirement readiness via a ``tenant_filter`` UUID selector. The
  selector renders only when ``operator.platform_admin`` is true (a soft-hide UX
  hint); the service stays the authority --
  :func:`~meho_backplane.auth.rbac.authorize_tenant_scope` raises HTTP 403
  ``cross_tenant_requires_platform_admin`` on a forged cross-tenant ``tenant_filter``
  from a non-platform-admin, which this handler surfaces as an inline error card
  (never a 500). The UI sends **no** ``blocker_counts`` / ``baseline_overrides``
  -- those are CLI-fill-only, so criterion 4 (``meho_vs_baseline``) reads yellow
  ("baseline did not run") and "READY TO RETIRE" is effectively unreachable via
  the bare UI: the honest v0.2 posture, which the template surfaces with an
  explanatory note + the ``rest_excluded`` / ``counted_surfaces`` honesty-gap
  explainer (criteria 1+2 are fed only by the audited ``/mcp`` search tools).

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
fresh mint + re-set. See :func:`_resolve_fragment_csrf`.

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
import uuid
from datetime import UTC, datetime
from typing import Final

import structlog
from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse

from meho_backplane.auth.operator import Operator
from meho_backplane.auth.rbac import authorize_tenant_scope
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.retrieval.eval import EvalResult, eval_all
from meho_backplane.retrieval.retire import (
    SURFACE_VERDICT_ORDER,
    RetireChecklistReport,
    compute_retire_checklist,
)
from meho_backplane.retrieval.retriever import (
    CANDIDATE_LIMIT,
    RetrievalFacets,
    RetrievalHit,
    list_retrieval_facets,
    retrieve,
)
from meho_backplane.retrieval.usage import (
    DEFAULT_SINCE,
    SUPPORTED_SURFACES,
    SinceValueError,
    UsageReport,
    compute_usage,
    parse_since,
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

#: Maximum ``since`` value length accepted by the usage fragment. Mirrors the
#: backend ``GET /api/v1/retrieve/usage`` ``since`` query cap (max 32 chars,
#: ``retrieve_usage.py``) so an oversized free-form window is rejected at the
#: form boundary rather than forwarded to :func:`parse_since`.
_MAX_SINCE_LENGTH: Final[int] = 32

#: Maximum ``tenant_filter`` value length accepted by the retire fragment.
#: A UUID string is 36 chars; the small slack tolerates surrounding whitespace
#: a paste might carry. An over-long value is rejected at the form boundary
#: rather than forwarded to :class:`uuid.UUID` parsing.
_MAX_TENANT_FILTER_LENGTH: Final[int] = 64

#: The ``since`` presets the usage selector offers. ``30d`` (the backend
#: :data:`~meho_backplane.retrieval.usage.DEFAULT_SINCE`) is the retire-decision
#: window Goal #215 keys off; the others are common shorter lookbacks. An
#: operator can still type any ``<N>d`` / ``<N>h`` / ISO-8601 value the backend
#: parser accepts.
_SINCE_OPTIONS: Final[tuple[str, ...]] = ("24h", "7d", "30d", "90d")

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


async def _resolve_operator_softly(session: UISessionContext) -> Operator | None:
    """Reconstruct the operator for the read-only page render, failing **soft**.

    Two page-render concerns need the operator on the ``GET /ui/retrieval``
    render: the retire tab's cross-tenant ``tenant_filter`` selector (shown only
    to a platform admin) and the diagnostics Source / Kind datalists (#2458,
    enumerated own-tenant + own-principal). Both must keep rendering even when
    the JWT round-trip can't complete (a transient JWKS outage, a session
    revoked in the gap since the middleware check). So any failure projects to
    ``None`` -- the selector hides and the datalists render empty -- mirroring
    the soft-hide posture
    :func:`~meho_backplane.ui.routes.connectors.operator.resolve_role_probe`
    uses. The hide is only a UX hint: every ``POST`` handler reconstructs the
    operator for real and the server-side authorities
    (:func:`~meho_backplane.auth.rbac.authorize_tenant_scope`, the substrate's
    own tenant + per-principal predicates) stay authoritative.
    """
    try:
        return await _resolve_operator(session)
    except Exception as exc:
        log.info(
            "ui_retrieval_operator_probe_unavailable",
            session_id=str(session.session_id),
            reason=type(exc).__name__,
        )
        return None


async def _load_retrieval_facets(operator: Operator | None) -> RetrievalFacets:
    """Enumerate the tenant's distinct Source / Kind values for the datalists.

    Fails **soft** to empty facets: a datalist is a discovery aid, not a gate,
    so a DB hiccup (or an unresolved operator) must never 500 the read-only page
    render. Delegates to
    :func:`~meho_backplane.retrieval.retriever.list_retrieval_facets`, own-tenant
    + own-principal scoped, so no cross-tenant source/kind leaks into the
    suggestions and a user-scoped memory kind another principal owns is excluded
    -- the values offered are exactly the ones the diagnostics ``retrieve`` call
    can actually match.
    """
    if operator is None:
        return RetrievalFacets(sources=(), kinds=())
    try:
        return await list_retrieval_facets(operator.tenant_id, principal_sub=operator.sub)
    except Exception as exc:
        log.info(
            "ui_retrieval_facets_unavailable",
            operator_sub=operator.sub,
            tenant_id=str(operator.tenant_id),
            reason=type(exc).__name__,
        )
        return RetrievalFacets(sources=(), kinds=())


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


def _resolve_fragment_csrf(request: Request, session_id: str) -> tuple[str, bool]:
    """Pick the CSRF token a retrieval fragment echoes + whether to set the cookie.

    Returns ``(token, set_cookie)``. Shared by every ``/ui/retrieval`` HTMX
    fragment (Diagnostics / Usage / Eval) -- each swaps only its own results
    region; the ``<form>`` that carries the ``hx-headers`` ``X-CSRF-Token``
    lives in ``index.html`` and is **not** re-rendered. So the rule (mirroring
    the corpus surface's ``_resolve_search_csrf``, the #1754 fix) is:

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

    @router.post("/ui/retrieval/usage", response_class=HTMLResponse)
    async def retrieval_usage(
        request: Request,
        session: UISessionContext = _require_session,
        since: str = Form(default=DEFAULT_SINCE, max_length=_MAX_SINCE_LENGTH),
    ) -> HTMLResponse:
        """Run the Usage Analytics fragment (delegates to :func:`_render_usage`)."""
        return await _render_usage(request, session, since)

    @router.post("/ui/retrieval/eval", response_class=HTMLResponse)
    async def retrieval_eval(
        request: Request,
        session: UISessionContext = _require_session,
    ) -> HTMLResponse:
        """Run the Eval Quality fragment (delegates to :func:`_render_eval`)."""
        return await _render_eval(request, session)

    @router.post("/ui/retrieval/retire-checklist", response_class=HTMLResponse)
    async def retrieval_retire_checklist(
        request: Request,
        session: UISessionContext = _require_session,
        tenant_filter: str = Form(default="", max_length=_MAX_TENANT_FILTER_LENGTH),
    ) -> HTMLResponse:
        """Run the Retire Checklist fragment (delegates to :func:`_render_retire`)."""
        return await _render_retire(request, session, tenant_filter)

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

    Resolves ``operator.platform_admin`` (soft-hiding on any JWT hiccup) so the
    Retire-checklist tab renders the cross-tenant ``tenant_filter`` selector only
    for a platform admin -- a UX hint, not the authority (the POST handler
    re-checks server-side via ``authorize_tenant_scope``).
    """
    csrf_token = mint_csrf_token(str(session.session_id))
    operator = await _resolve_operator_softly(session)
    platform_admin = operator.platform_admin if operator is not None else False
    facets = await _load_retrieval_facets(operator)
    context: dict[str, object] = {
        **_diagnostics_form_context(
            query="",
            source="",
            kind="",
            limit=_DEFAULT_LIMIT,
            csrf_token=csrf_token,
        ),
        # Source / Kind diagnostics datalists (#2458): the distinct
        # retrieval-visible values so an operator picks a filter the substrate
        # holds instead of guessing. Page-render only -- the POST diagnostics
        # fragment re-renders only the results region, not the form, so these
        # are not threaded through ``_diagnostics_form_context``.
        "source_options": facets.sources,
        "kind_options": facets.kinds,
        "hits": [],
        "searched": False,
        "candidate_limit": CANDIDATE_LIMIT,
        # Usage Analytics tab (#1889): the lookback form defaults to the
        # retire-decision window; the partials read their data off
        # ``report`` / ``result`` (both ``None`` on the initial render, so the
        # included fragments fall through to their quiet prompts).
        "since": DEFAULT_SINCE,
        "since_options": _SINCE_OPTIONS,
        "report": None,
        "result": None,
        # Retire Checklist tab (#1890): ``checklist`` is ``None`` on the initial
        # render so the included partial falls through to its quiet prompt;
        # ``platform_admin`` gates the cross-tenant ``tenant_filter`` selector
        # (a soft-hide hint -- the POST handler re-checks server-side).
        "checklist": None,
        "platform_admin": platform_admin,
        "tenant_filter": "",
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
    hit (body clamped to a snippet with expand-on-click so ranked hits stay
    comparable at a glance, plus ``source`` / ``source_id`` / ``kind``, the
    ``fused_score``, and a per-signal breakdown where a ``None`` rank renders
    an explicit "absent from this signal's top-N" marker); an empty hit list
    renders a "no matches" state.

    The privacy ``audit_*`` contextvars are bound **before** the ``retrieve``
    call (and ``audit_hit_count`` after) so the in-process audit row carries the
    SHA-256 query trace even on a mid-retrieval exception. CSRF handling defers
    to :func:`_resolve_fragment_csrf`.
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

    csrf_token, set_csrf = _resolve_fragment_csrf(request, str(session.session_id))
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


# ---------------------------------------------------------------------------
# Usage Analytics tab (#1889)
# ---------------------------------------------------------------------------


async def _render_usage(
    request: Request,
    session: UISessionContext,
    since: str,
) -> HTMLResponse:
    """Run the HTMX Usage Analytics fragment for ``POST /ui/retrieval/usage``.

    Reconstructs the operator and runs the in-process
    :func:`~meho_backplane.retrieval.usage.compute_usage` own-tenant (no
    ``tenant_filter`` -- the ``platform_admin`` cross-tenant selector is the
    sibling T3 concern), then swaps ``retrieval/_usage_results.html`` into
    ``#retrieval-usage-results``.

    ``compute_usage`` is keyword-only and **requires** a ``session:
    AsyncSession``, so this handler opens one via
    :func:`~meho_backplane.db.engine.get_sessionmaker` exactly as the corpus
    console does -- the helper is read-only and does not commit or close the
    session (lifetime is the caller's concern). The ``since`` string is
    resolved with :func:`~meho_backplane.retrieval.usage.parse_since`; a
    :class:`~meho_backplane.retrieval.usage.SinceValueError` renders an inline
    400 error card (mirroring the REST route's
    ``raise HTTPException(status_code=400, ...)``) rather than escaping as a
    500.

    The honesty-gap explainer is load-bearing: a ``total_searches == 0`` with
    ``rest_excluded`` / ``counted_surfaces`` must read as "REST excluded -- only
    audited /mcp search tools are counted", not "no activity". The template
    renders that badge from the :class:`~meho_backplane.retrieval.usage.UsageReport`
    fields verbatim.
    """
    since_value = since.strip() or DEFAULT_SINCE

    report: UsageReport | None = None
    error_message: str | None = None
    now = datetime.now(UTC)
    try:
        since_dt = parse_since(since_value, now=now)
    except SinceValueError as exc:
        # Surface the parser's rejection as an inline 400-class error card --
        # the same shape the REST route maps to ``HTTPException(400, ...)`` --
        # rather than letting it escape as a 500.
        error_message = str(exc)
    else:
        operator = await _resolve_operator(session)
        report = await _run_usage(operator, since_dt, now)

    csrf_token, set_csrf = _resolve_fragment_csrf(request, str(session.session_id))
    context: dict[str, object] = {
        "since": since_value,
        "since_options": _SINCE_OPTIONS,
        "report": report,
        "error_message": error_message,
        "csrf_token": csrf_token,
    }
    response = get_templates().TemplateResponse(request, "retrieval/_usage_results.html", context)
    if set_csrf:
        _set_csrf_cookie(response, csrf_token)
    return response


async def _run_usage(
    operator: Operator,
    since_dt: datetime,
    now: datetime,
) -> UsageReport:
    """Open a read-only session and run in-process ``compute_usage`` own-tenant.

    Scoped to ``operator.tenant_id`` (no ``tenant_filter``) across every
    :data:`~meho_backplane.retrieval.usage.SUPPORTED_SURFACES`. The session is
    opened via :func:`~meho_backplane.db.engine.get_sessionmaker` and closed by
    the ``async with`` -- ``compute_usage`` neither commits nor closes it.
    """
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as db_session:
        report = await compute_usage(
            session=db_session,
            since=since_dt,
            until=now,
            surfaces=SUPPORTED_SURFACES,
            tenant_id=operator.tenant_id,
        )
    log.info(
        "ui_retrieval_usage_completed",
        operator_sub=operator.sub,
        tenant_id=str(operator.tenant_id),
        total_searches=report.total_searches,
        bucket_count=len(report.buckets),
    )
    return report


# ---------------------------------------------------------------------------
# Eval Quality tab (#1889)
# ---------------------------------------------------------------------------


async def _render_eval(
    request: Request,
    session: UISessionContext,
) -> HTMLResponse:
    """Run the HTMX Eval Quality fragment for ``POST /ui/retrieval/eval``.

    Reconstructs the operator and runs the in-process
    :func:`~meho_backplane.retrieval.eval.eval_all` own-tenant with **no
    baseline** -- the server-side baseline path is rejected 501
    (``retrieve_eval.py``) since v0.2 ships no checked-in corpus snapshot, so
    this surface never offers a baseline toggle and never reaches that path.
    Unlike usage, ``eval_all`` takes **no session** (it owns its own retrieval
    plumbing), so it is called as ``await eval_all(tenant_id=operator.tenant_id)``.

    Swaps ``retrieval/_eval_results.html`` into ``#retrieval-eval-results``: per
    surface, ``precision_at_5`` / ``mrr`` / ``coverage`` and a green/yellow/red
    verdict pill mapped (in the template) from the verdict token the service
    returns verbatim, plus the ``overall_verdict``. An empty-corpus surface
    legitimately returns ``green``; the verdict is rendered as-is, never
    recomputed.
    """
    operator = await _resolve_operator(session)
    result = await _run_eval(operator)

    csrf_token, set_csrf = _resolve_fragment_csrf(request, str(session.session_id))
    context: dict[str, object] = {
        "result": result,
        "csrf_token": csrf_token,
    }
    response = get_templates().TemplateResponse(request, "retrieval/_eval_results.html", context)
    if set_csrf:
        _set_csrf_cookie(response, csrf_token)
    return response


async def _run_eval(operator: Operator) -> EvalResult:
    """Run in-process ``eval_all`` own-tenant with no baseline.

    Scoped to ``operator.tenant_id``; ``eval_all`` runs every shipped surface's
    corpus (the default) and computes MEHO-only metrics -- no ``baseline``
    argument, so the 501 baseline path is never reached.
    """
    result = await eval_all(tenant_id=operator.tenant_id)
    log.info(
        "ui_retrieval_eval_completed",
        operator_sub=operator.sub,
        tenant_id=str(operator.tenant_id),
        overall_verdict=result.overall_verdict,
        surface_count=len(result.surfaces),
    )
    return result


# ---------------------------------------------------------------------------
# Retire Checklist tab (#1890)
# ---------------------------------------------------------------------------


async def _render_retire(
    request: Request,
    session: UISessionContext,
    tenant_filter: str,
) -> HTMLResponse:
    """Run the HTMX Retire Checklist fragment for ``POST /ui/retrieval/retire-checklist``.

    Reconstructs the operator and runs the in-process
    :func:`~meho_backplane.retrieval.retire.compute_retire_checklist` over every
    :data:`~meho_backplane.retrieval.retire.SURFACE_VERDICT_ORDER` surface, then
    swaps ``retrieval/_retire_results.html`` into ``#retrieval-retire-results``.

    Tenant scoping mirrors the REST route (``retrieve_retire.py``):

    * A blank ``tenant_filter`` (the common case, and the only shape a
      non-platform-admin can produce from the rendered form) resolves to the
      operator's own tenant.
    * A platform admin may name a different tenant; the resolution defers to
      :func:`~meho_backplane.auth.rbac.authorize_tenant_scope`, which returns the
      requested tenant for a platform admin and raises HTTP 403
      ``cross_tenant_requires_platform_admin`` otherwise. A forged cross-tenant
      ``tenant_filter`` from a non-platform-admin therefore surfaces as an inline
      error card (the 403 detail), **not** a 500.
    * A malformed (non-UUID) ``tenant_filter`` renders an inline error card too,
      rather than escaping as a 422/500.

    The UI sends **no** ``blocker_counts`` / ``baseline_overrides`` (CLI-fill-only),
    so criterion 4 reads yellow and the template surfaces that honest-posture
    note. ``compute_retire_checklist`` is keyword-only and **requires** a
    ``session: AsyncSession``, opened here via
    :func:`~meho_backplane.db.engine.get_sessionmaker` exactly as the corpus
    console does; it runs ``eval_all`` internally for criteria 3+4, so this
    handler never calls ``eval_all`` itself.
    """
    requested = tenant_filter.strip()

    checklist: RetireChecklistReport | None = None
    error_message: str | None = None
    operator = await _resolve_operator(session)
    try:
        target_tenant = _resolve_target_tenant(operator, requested)
        checklist = await _run_retire(operator, target_tenant)
    except HTTPException as exc:
        # A 401 from the operator-reconstruction seam is an auth condition that
        # must propagate (the BFF maps it to a login redirect); a 403 from the
        # cross-tenant gate is a denied-but-handled condition that renders inline.
        if exc.status_code == status.HTTP_403_FORBIDDEN:
            error_message = str(exc.detail)
        else:
            raise
    except ValueError as exc:
        # A malformed (non-UUID) tenant_filter -- render inline, never a 500.
        error_message = f"Invalid tenant filter: {exc}"

    csrf_token, set_csrf = _resolve_fragment_csrf(request, str(session.session_id))
    context: dict[str, object] = {
        "checklist": checklist,
        "error_message": error_message,
        "platform_admin": operator.platform_admin,
        "tenant_filter": requested,
        "csrf_token": csrf_token,
    }
    response = get_templates().TemplateResponse(request, "retrieval/_retire_results.html", context)
    if set_csrf:
        _set_csrf_cookie(response, csrf_token)
    return response


def _resolve_target_tenant(operator: Operator, requested: str) -> uuid.UUID:
    """Resolve the effective tenant for a retire-checklist run.

    A blank *requested* (the own-tenant path) skips the cross-tenant gate and
    returns ``operator.tenant_id`` directly. A non-blank value is parsed to a
    :class:`uuid.UUID` (a malformed value raises :class:`ValueError`, which the
    caller renders inline) and then run through
    :func:`~meho_backplane.auth.rbac.authorize_tenant_scope`, which is the
    server-side authority on the cross-tenant claim (403 for a non-platform-admin).
    """
    if not requested:
        return operator.tenant_id
    parsed = uuid.UUID(requested)
    return authorize_tenant_scope(operator, parsed)


async def _run_retire(
    operator: Operator,
    target_tenant: uuid.UUID,
) -> RetireChecklistReport:
    """Open a read-only session and run in-process ``compute_retire_checklist``.

    Scoped to *target_tenant* (the operator's own tenant, or the
    platform-admin-authorized ``tenant_filter`` tenant) over every
    :data:`~meho_backplane.retrieval.retire.SURFACE_VERDICT_ORDER` surface. The
    session is opened via :func:`~meho_backplane.db.engine.get_sessionmaker` and
    closed by the ``async with``. No ``blocker_counts`` / ``baseline_overrides``
    are passed (CLI-fill-only), so criterion 5 reads yellow ("unknown") and
    criterion 4 reads yellow ("baseline did not run") -- the honest v0.2 posture.
    """
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as db_session:
        checklist = await compute_retire_checklist(
            session=db_session,
            surfaces=list(SURFACE_VERDICT_ORDER),
            tenant_id=target_tenant,
        )
    log.info(
        "ui_retrieval_retire_checklist_completed",
        operator_sub=operator.sub,
        operator_tenant_id=str(operator.tenant_id),
        target_tenant_id=str(target_tenant),
        cross_tenant=target_tenant != operator.tenant_id,
        overall_verdict=checklist.overall_verdict,
        surface_count=len(checklist.surfaces),
    )
    return checklist
