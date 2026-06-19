# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Docs-corpus UI routes: collection picker + ask-the-corpus + cited chunks.

Initiative #1775 (G10.7 Docs-corpus console surface), Task #1777.

``GET /ui/corpus`` renders the page: a collection ``<select>`` populated
from the entitled, tenant-scoped catalogue (pre-selected when exactly one
collection is entitled), a query input, and an empty ``#corpus-results``
region. ``POST /ui/corpus/search`` is the HTMX fragment endpoint -- it
reconstructs the session operator, runs the in-process ``search_docs``
service, and swaps ``corpus/_results.html`` (one card per cited chunk)
into ``#corpus-results``.

Reusing the backends (no new ``/api/v1`` endpoint)
--------------------------------------------------

The page does not add a REST endpoint. The collection list reuses the
same tenant-first dedupe + per-collection entitlement filter the catalogue
list route runs (``api/v1/doc_collections.py``); the search reuses the
shared :func:`~meho_backplane.docs_search.search_docs` service the
``POST /api/v1/search_docs`` route fronts, composed from the same
primitives (:func:`~meho_backplane.docs_search.build_docs_scope` +
:func:`~meho_backplane.docs_search.resolve_entitled_ready_collection`) and
mapping the same typed failures to the same status classes (403 / 409 /
503 / 422). Routing through the service -- not a self-HTTP call -- keeps
the in-process audit binding and avoids a network hop.

Operator reconstruction
-----------------------

The BFF :class:`~meho_backplane.ui.auth.middleware.UISessionContext` the
``require_ui_session`` dependency hands route handlers carries only
``operator_sub`` / ``tenant_id`` -- not the capability set the
per-collection entitlement gate needs. The handlers reconstruct the full
:class:`~meho_backplane.auth.operator.Operator` from the session via the
same proven seam :func:`~meho_backplane.ui.auth.middleware.require_ui_admin`
uses: load the decrypted session, present its (silently-refreshed) access
token to the chassis JWT chain via
:func:`~meho_backplane.ui.auth.refresh.verify_access_token_with_refresh`.
A dead session (revoked / expired between the middleware check and here)
surfaces as the ``session_expired`` 401 the BFF error handler maps to a
login redirect.

CSRF
----

The search form declares its own ``hx-headers`` ``X-CSRF-Token`` (HTMX
does not propagate ``hx-headers`` to child elements -- the #1693 class --
so the token rides the form, not an ancestor). ``GET /ui/corpus`` mints
the token and sets the ``meho_csrf`` cookie to establish the double-submit
pair. The search fragment swaps only ``#corpus-results`` and leaves that
form in place, so it **reuses** the live cookie token (validated, cookie
untouched) rather than rotating a fresh one out from under the un-swapped
form -- the cookie-rotation desync the memory-poll fix (#1754) diagnosed.
A missing / invalid cookie on a fragment fetch falls back to a fresh mint
+ re-set so the pair is always restorable. See :func:`_resolve_search_csrf`.

Tenant scoping + RBAC
---------------------

``operator`` role minimum (enforced by ``require_ui_session``; matches the
OPERATOR floor both backend routes carry). Tenant identity is derived from
``session.tenant_id`` only -- no query parameter or form field overrides
it, and the reconstructed operator's capability set (also tenant-shaped)
gates which collections are listed and searchable.
"""

from __future__ import annotations

from typing import Final

import structlog
from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.auth.corpus import CorpusUnavailable
from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import DocCollection as DocCollectionORM
from meho_backplane.docs_collections import (
    DocCollection,
    DocCollectionSummary,
    project_doc_collection_to_summary,
)
from meho_backplane.docs_search import (
    CollectionDisabledError,
    CollectionForbiddenError,
    CollectionNotReadyError,
    DocsChunk,
    MissingDocsFilterError,
    UnknownCollectionError,
    build_docs_scope,
    collection_capability_key,
    resolve_entitled_ready_collection,
    search_docs,
)
from meho_backplane.settings import get_settings
from meho_backplane.ui.auth.middleware import UISessionContext, require_ui_session
from meho_backplane.ui.auth.refresh import (
    load_fresh_session,
    verify_access_token_with_refresh,
)
from meho_backplane.ui.csrf import CSRF_COOKIE_NAME, mint_csrf_token, verify_csrf_token
from meho_backplane.ui.templating import get_templates

__all__ = ["build_corpus_search_router"]

log = structlog.get_logger(__name__)

#: Maximum query length accepted by the search fragment. The backend
#: ``search_docs`` request schema caps ``query`` at 2000 characters; the
#: UI caps earlier so an oversized free-form query is rejected at the form
#: boundary (FastAPI 422) rather than forwarded.
_MAX_QUERY_LENGTH: Final[int] = 2000

#: Maximum collection-key length accepted by the search fragment. Mirrors
#: the backend ``SearchDocsRequest.collection`` cap so an oversized value
#: is rejected at the form boundary.
_MAX_COLLECTION_LENGTH: Final[int] = 128

#: Number of cited chunks requested per search. Matches the backend
#: ``search_docs`` request default; the page shows a single ranked list
#: (pagination is a corpus follow-up, out of scope per #1777).
_SEARCH_LIMIT: Final[int] = 10

#: Module-level ``Depends`` closure for the operator-session gate. Built
#: once (rather than inline) to satisfy ruff B008, matching the convention
#: the kb / topology / dashboard routes established.
_require_session = Depends(require_ui_session)


async def _resolve_operator(session: UISessionContext) -> Operator:
    """Reconstruct the full :class:`Operator` from the BFF session.

    Loads the decrypted session row and presents its access token to the
    chassis JWT chain (silently refreshing on the ``token_expired`` 401)
    via :func:`~meho_backplane.ui.auth.refresh.verify_access_token_with_refresh`
    -- the same seam :func:`~meho_backplane.ui.auth.middleware.require_ui_admin`
    uses to surface the operator's claims. The returned operator carries
    the capability set the per-collection entitlement gate reads.

    Raises :class:`fastapi.HTTPException` 401 when the session has been
    revoked / expired in the gap since the middleware check (the BFF error
    handler maps the ``session_expired`` detail to a login redirect for
    HTML requests).
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


async def _list_entitled_collections(
    operator: Operator,
) -> tuple[list[DocCollectionSummary], list[str]]:
    """List the doc collections *operator* is entitled to search.

    Mirrors the ``GET /api/v1/doc_collections`` catalogue query: reads
    ``doc_collections`` tenant-scoped (global + this tenant's rows),
    de-duplicates a shadowed global key in favour of the tenant row
    (tenant wins), and filters to the collections the operator holds
    ``meho-docs:<collection_key>`` for -- the same per-collection
    entitlement ``search_docs`` enforces, so every listed key is one the
    search path will accept. An unprovisioned tenant gets an empty list.

    Returns ``(entitled_summaries, unentitled_keys)``. ``unentitled_keys``
    is the sorted set of visible collection keys the operator is **not**
    entitled to (no ``meho-docs:<key>`` capability). It is the signal the
    empty-state diagnostic uses to tell a "no docs corpus exists at all"
    tenant apart from a "a corpus exists but your identity is missing the
    capability" one — the diagnosability gap T2 (#1802) closes. The keys
    are never rendered as a picker (an un-entitled collection stays hidden
    from the search surface); they only name a concrete missing capability
    in the diagnostic.
    """
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as db_session:
        stmt = select(DocCollectionORM).where(
            (DocCollectionORM.tenant_id == operator.tenant_id)
            | (DocCollectionORM.tenant_id.is_(None)),
        )
        result = await db_session.execute(stmt)
        rows = list(result.scalars().all())

    # Tenant-first dedupe: a key present as both a global and a tenant row
    # collapses to the tenant row (it overrides the global backend binding
    # / metadata), order-independently. Mirrors the catalogue route.
    by_key: dict[str, DocCollectionORM] = {}
    for row in rows:
        existing = by_key.get(row.collection_key)
        if existing is None or row.tenant_id is not None:
            by_key[row.collection_key] = row

    entitled: list[DocCollectionORM] = []
    unentitled_keys: list[str] = []
    for row in by_key.values():
        if collection_capability_key(row.collection_key) in operator.capabilities:
            entitled.append(row)
        else:
            unentitled_keys.append(row.collection_key)
    entitled.sort(key=lambda row: row.collection_key)
    return (
        [project_doc_collection_to_summary(row) for row in entitled],
        sorted(unentitled_keys),
    )


def _entitlement_diagnostic(
    operator: Operator,
    unentitled_keys: list[str],
) -> dict[str, object] | None:
    """Build the empty-picker entitlement diagnostic, or ``None``.

    Returns a context dict naming a concrete missing ``meho-docs:<key>``
    capability and the identity it was checked against (``operator_sub`` +
    ``tenant_id``) **only** when the catalogue holds at least one collection
    the operator cannot see. That distinguishes the two empty-picker causes
    the operator otherwise cannot tell apart (T2 #1802):

    * **A corpus exists, the identity is missing the capability** — the
      reported symptom (the ``vmware`` collection is attached + searchable
      via MCP, but the UI session identity lacks ``meho-docs:vmware``). The
      diagnostic names the first such key so the operator knows *exactly*
      which claim to grant on *which* identity — turning the opaque
      "No doc collections available" into an actionable next step. The
      asymmetry's root cause is a per-audience Keycloak claim divergence; the
      remediation lives in ``deploy/values-examples/README.md``.
    * **No corpus exists at all** (``unentitled_keys`` empty) — the genuine
      unprovisioned case. Returns ``None`` so the template keeps the plain
      "ask an administrator to register and entitle a collection" copy.

    The un-entitled keys are never surfaced as searchable options; only the
    first (sorted, deterministic) key names the missing capability.
    """
    if not unentitled_keys:
        return None
    missing_key = unentitled_keys[0]
    return {
        "required_capability": collection_capability_key(missing_key),
        "collection_key": missing_key,
        "operator_sub": operator.sub,
        "tenant_id": str(operator.tenant_id),
        "unentitled_count": len(unentitled_keys),
    }


def _set_csrf_cookie(response: HTMLResponse, csrf_token: str) -> None:
    """Set the ``meho_csrf`` double-submit cookie on *response*.

    The cookie value MUST equal the token the rendered markup echoes via
    ``hx-headers`` -- the CSRF middleware rejects a mismatch
    (``value_mismatch``). Mirrors the SameSite=Strict + Secure +
    non-HttpOnly posture every UI surface's CSRF cookie carries (HTMX must
    read it to populate ``X-CSRF-Token``; the HMAC binding to the
    session id defeats the cookie-injection vector JS-read would otherwise
    open).
    """
    response.set_cookie(
        key=CSRF_COOKIE_NAME,
        value=csrf_token,
        httponly=False,
        secure=True,
        samesite="strict",
        path="/ui",
    )


def _resolve_search_csrf(request: Request, session_id: str) -> tuple[str, bool]:
    """Pick the CSRF token the search fragment echoes + whether to set the cookie.

    Returns ``(token, set_cookie)``. The fragment swaps only
    ``#corpus-results`` -- the search ``<form>`` that carries the
    ``hx-headers`` ``X-CSRF-Token`` lives in ``index.html`` and is **not**
    re-rendered. So the rule (mirroring the #1754 memory-poll fix) is:

    * **Live cookie present + valid** -- *reuse* that token and do **not**
      re-set the cookie. Minting a fresh token + ``Set-Cookie`` here would
      rotate the cookie out from under the un-swapped form, whose still-
      rendered ``hx-headers`` snapshot then carries the old token while the
      cookie holds the new one -- the ``value_mismatch`` 403 the
      cookie-rotation desync class (#1693 / #1706 / #1754) produces. The
      CSRF token is stateless and HMAC-bound to the session, so the same
      token validates on every search until logout.
    * **Cookie missing / invalid** -- defensive fallback: mint a fresh
      token and re-set the cookie so a direct fragment fetch with no prior
      full-page load (or a tampered cookie) still establishes a working
      double-submit pair.

    The reuse is gated on :func:`verify_csrf_token` so a foreign or
    tampered cookie value never gets echoed back as the fragment's token.
    """
    existing = request.cookies.get(CSRF_COOKIE_NAME)
    if existing and verify_csrf_token(session_id, existing):
        return existing, False
    return mint_csrf_token(session_id), True


def _search_or_error_context(exc: HTTPException) -> dict[str, object]:
    """Project a search HTTPException into the error-card template context.

    The ``corpus/_results.html`` fragment renders an ``error`` block when
    ``error_status`` is set. The detail string is surfaced verbatim
    (the backend service guarantees no corpus response body leaks through
    a 503 detail); a structured ``dict`` detail is flattened to a human
    string, preferring the actionable ``message`` (the ``not_entitled``
    shape names the missing ``meho-docs:<key>`` capability + the identity it
    checked, T2 #1802) and falling back to the ``error`` code for the
    code-only shapes (``collection_disabled`` / ``unknown_collection``).
    """
    detail = exc.detail
    if isinstance(detail, dict):
        message = str(detail.get("message") or detail.get("error") or detail)
    else:
        message = str(detail)
    return {"error_status": exc.status_code, "error_message": message}


def build_corpus_search_router() -> APIRouter:
    """Construct the ``GET /ui/corpus`` + ``POST /ui/corpus/search`` router.

    Factory function (not a module-level constant) so a test app can
    construct parallel routers without shared route state -- the same
    convention :func:`meho_backplane.ui.routes.kb.build_kb_router` and the
    other surface routers follow. The admin Collections lifecycle routes
    (``/ui/corpus/collections*``, #1882) live on a sibling router built by
    :func:`meho_backplane.ui.routes.corpus.collections.build_corpus_collections_router`;
    :func:`meho_backplane.ui.routes.corpus.build_corpus_router` aggregates
    both with the load-bearing literal-before-param include order.
    """
    router = APIRouter(tags=["ui-corpus"])

    @router.get("/ui/corpus", response_class=HTMLResponse)
    async def corpus_index(
        request: Request,
        session: UISessionContext = _require_session,
    ) -> HTMLResponse:
        """Render the docs-corpus page (delegates to :func:`_render_corpus_index`)."""
        return await _render_corpus_index(request, session)

    # NOTE: POST /ui/corpus/search is registered here. There is no
    # /ui/corpus/{slug} route on this surface today, but the search route
    # is kept ahead of any future slug route so first-match-wins routing
    # never binds the literal "search" segment as a slug parameter -- the
    # same ordering discipline the kb router documents.

    @router.post("/ui/corpus/search", response_class=HTMLResponse)
    async def corpus_search(
        request: Request,
        session: UISessionContext = _require_session,
        collection: str = Form(default="", max_length=_MAX_COLLECTION_LENGTH),
        q: str = Form(default="", max_length=_MAX_QUERY_LENGTH),
    ) -> HTMLResponse:
        """Run the search fragment (delegates to :func:`_render_corpus_search`)."""
        return await _render_corpus_search(request, session, collection, q)

    return router


async def _render_corpus_index(
    request: Request,
    session: UISessionContext,
) -> HTMLResponse:
    """Render the docs-corpus page for ``GET /ui/corpus``.

    Populates the collection ``<select>`` from the operator's entitled,
    tenant-scoped catalogue and pre-selects the sole option when the
    operator is entitled to exactly one collection. Mints a CSRF token and
    sets the ``meho_csrf`` cookie so the search form's ``hx-headers`` echo
    passes the double-submit check.
    """
    operator = await _resolve_operator(session)
    collections, unentitled_keys = await _list_entitled_collections(operator)
    # Default-if-one: pre-select the sole entitled collection so an operator
    # with a single corpus can search without first opening the dropdown.
    # With zero or many, no collection is pre-selected.
    selected_collection = collections[0].collection_key if len(collections) == 1 else ""
    # When the picker is empty, tell the operator *why*: a corpus exists but
    # their identity lacks the capability (actionable), vs. no corpus at all.
    entitlement_diagnostic = (
        _entitlement_diagnostic(operator, unentitled_keys) if not collections else None
    )

    csrf_token = mint_csrf_token(str(session.session_id))
    context: dict[str, object] = {
        "collections": collections,
        "selected_collection": selected_collection,
        "query": "",
        "chunks": [],
        "searched": False,
        "operator_sub": session.operator_sub,
        "entitlement_diagnostic": entitlement_diagnostic,
        # Gates the unprovisioned empty-state's in-console register affordance
        # (T2 #1883): a tenant_admin gets a "Register a collection" CTA, a
        # plain operator gets a "a tenant administrator can register one" hint.
        # Derived from the already-resolved operator (no extra JWT round-trip).
        "is_tenant_admin": operator.tenant_role == TenantRole.TENANT_ADMIN,
        "csrf_token": csrf_token,
        "active_surface": "corpus",
        "page_title": "Docs Corpus",
    }
    response = get_templates().TemplateResponse(request, "corpus/index.html", context)
    _set_csrf_cookie(response, csrf_token)
    return response


async def _render_corpus_search(
    request: Request,
    session: UISessionContext,
    collection: str,
    q: str,
) -> HTMLResponse:
    """Run the HTMX search fragment for ``POST /ui/corpus/search``.

    Swaps ``corpus/_results.html`` into ``#corpus-results``. A successful
    search renders one card per chunk (content + source_url link + formatted
    score, plus a collection tag when present); an empty hit list renders a
    "no results" state; a 403 / 409 / 503 / 422 from the service renders a
    typed error card with the detail.

    CSRF handling defers to :func:`_resolve_search_csrf`: the live session
    token is reused (cookie left untouched) so the un-swapped search form's
    ``hx-headers`` echo stays aligned with the cookie across repeated
    searches; a missing / invalid cookie triggers a defensive re-mint +
    re-set so the double-submit pair is restored.
    """
    query = q.strip()
    collection_key = collection.strip()

    chunks: list[DocsChunk] = []
    error_context: dict[str, object] = {}
    if query:
        operator = await _resolve_operator(session)
        try:
            chunks = await _run_search(operator, query, collection_key)
        except HTTPException as exc:
            error_context = _search_or_error_context(exc)

    csrf_token, set_csrf = _resolve_search_csrf(request, str(session.session_id))
    context: dict[str, object] = {
        "collections": [],
        "selected_collection": collection_key,
        "query": query,
        "chunks": chunks,
        "searched": bool(query),
        "csrf_token": csrf_token,
        **error_context,
    }
    response = get_templates().TemplateResponse(request, "corpus/_results.html", context)
    if set_csrf:
        _set_csrf_cookie(response, csrf_token)
    return response


async def _run_search(operator: Operator, query: str, collection_key: str) -> list[DocsChunk]:
    """Run the in-process ``search_docs`` service, mapping failures to HTTP.

    Composes the same primitives the ``POST /api/v1/search_docs`` route
    fronts and maps each typed failure to the same status class:

    * missing / blank ``collection`` -> 422 (the mandatory binary scope)
    * unknown ``collection`` -> 422
    * not entitled -> 403; ``disabled`` -> 403 (terminal); not-ready
      (``provisioning`` / ``rebuilding``) -> 409 (retryable)
    * backend unavailable -> 503 (fail-closed; never an empty list)

    Returns the ranked cited-chunk list on success.
    """
    try:
        docs_scope = build_docs_scope(collection_key or None)
    except MissingDocsFilterError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        ) from exc

    sessionmaker = get_sessionmaker()
    async with sessionmaker() as db_session, db_session.begin():
        collection = await _resolve_collection_or_http_error(
            db_session, operator, docs_scope.collection_key
        )

    try:
        result = await search_docs(
            operator,
            query,
            scope=docs_scope,
            collection=collection,
            limit=_SEARCH_LIMIT,
        )
    except CorpusUnavailable as exc:
        # Fail-closed: an unconfigured / unreachable / non-2xx backend is a
        # 503, never an empty result. The transport never attaches the raw
        # corpus body to the exception, so nothing leaks through the card.
        log.warning(
            "ui_corpus_search_backend_unavailable",
            operator_sub=operator.sub,
            collection=docs_scope.collection_key,
            corpus_status=exc.status,
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
        ) from exc

    return list(result.chunks)


async def _resolve_collection_or_http_error(
    db_session: AsyncSession,
    operator: Operator,
    collection_key: str,
) -> DocCollection:
    """Resolve + entitle + readiness-gate the collection, mapping to HTTP.

    Mirrors the identical helper on the ``search_docs`` REST route: each
    typed access failure maps to its own status -- unknown -> 422, not
    entitled -> 403, disabled -> 403 (terminal), transiently not-ready ->
    409 (retryable).
    """
    try:
        return await resolve_entitled_ready_collection(db_session, operator, collection_key)
    except UnknownCollectionError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={
                "error": "unknown_collection",
                "collection": exc.collection_key,
            },
        ) from exc
    except CollectionForbiddenError as exc:
        # Structured 403 mirroring the REST route: the error card names the
        # missing ``meho-docs:<key>`` capability and the identity it checked
        # so the operator sees *why* the search was denied, not just "Not
        # permitted" (T2 #1802).
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "error": "not_entitled",
                "collection": exc.collection_key,
                "required_capability": exc.required_capability,
                "operator_sub": exc.operator_sub,
                "tenant_id": exc.tenant_id,
                "message": str(exc),
            },
        ) from exc
    except CollectionDisabledError as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"error": "collection_disabled", "collection": exc.collection_key},
        ) from exc
    except CollectionNotReadyError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
        ) from exc
