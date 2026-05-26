# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""KB UI routes: list/search + entry detail + hover preview partial.

Initiative #339 (G10.2 Knowledge base UI), Task #870 (T1). Mounts three
routes:

* ``GET /ui/kb`` — main KB surface. Empty query renders a paginated
  entry list (slug-sorted). Non-empty query renders ranked search
  results (BM25 + cosine + fused score) via
  :class:`~meho_backplane.kb.KbService`. HTMX partial: when
  ``HX-Request`` header is present the handler returns only the
  ``kb/_results.html`` fragment (no base-shell chrome).

* ``POST /ui/kb/search`` (HTMX search partial) — debounced keyup
  endpoint. Accepts ``q`` as a form field, calls
  :meth:`~meho_backplane.kb.KbService.search_entries` (``query``
  non-empty) or :meth:`~meho_backplane.kb.KbService.list_entries`
  (``query`` empty/blank), and returns the ``kb/_results.html``
  fragment. The ``hx-trigger="keyup changed delay:300ms"`` binding
  on the search input fires this endpoint; the HTMX form shape
  uses POST so the query string stays out of server logs.

* ``GET /ui/kb/<slug>`` — entry detail. Renders the Markdown body
  server-side via :func:`~meho_backplane.ui.routes.kb.render.render_markdown`
  (markdown-it-py GFM + pygments syntax highlight). Returns 404 for
  unknown or cross-tenant slugs.

* ``GET /ui/kb/<slug>/preview`` — HTMX hover-preview partial.
  Returns the ``kb/_preview.html`` fragment with the matched-snippet
  and query-term highlight markup. Called by
  ``hx-trigger="mouseenter delay:200ms"`` on result cards.

Tenant scoping
--------------

Every handler derives tenant identity from
:class:`~meho_backplane.ui.auth.middleware.UISessionContext`. There is
no query parameter or form field that overrides tenant — a cross-tenant
slug probe surfaces as 404, not 403, matching the ``/api/v1/kb``
surface's posture.

RBAC
----

``operator`` role minimum (enforced by
:func:`~meho_backplane.ui.auth.middleware.require_ui_session`). Upload
verbs (T2, T3) add a ``tenant_admin`` gate; this module is read-only.

HTMX conventions
----------------

* ``hx-trigger="keyup changed delay:300ms"`` on the search input so a
  keystroke that doesn't change the value doesn't fire a request.
* ``hx-push-url="true"`` on the search form updates the browser URL
  with the current query so operators can copy/paste search URLs.
* All state-changing forms (none in T1) would include the CSRF token
  via ``hx-headers``; T1 is read-only so the CSRF cookie is minted on
  page load only.
* The ``_results.html`` partial carries its own DaisyUI card markup
  and is swapped into ``#kb-results`` via ``hx-target`` + ``hx-swap="outerHTML"``.

Pagination
----------

``GET /ui/kb`` with empty query uses ``limit`` / ``offset`` query
params (defaults: limit=50, offset=0). The pagination bar is rendered
server-side inside ``_results.html`` so HTMX swaps work transparently.

References
----------

* HTMX debounced search: https://htmx.org/examples/active-search/
* markdown-it-py: https://markdown-it-py.readthedocs.io/en/latest/
* DaisyUI card: https://daisyui.com/components/card/
"""

from __future__ import annotations

import re
from typing import Final

import structlog
from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse

from meho_backplane.kb import KbEntry, KbEntrySearchHit, KbService
from meho_backplane.ui.auth.middleware import UISessionContext, require_ui_session
from meho_backplane.ui.csrf import CSRF_COOKIE_NAME, mint_csrf_token
from meho_backplane.ui.routes.kb.render import pygments_css, render_markdown
from meho_backplane.ui.templating import get_templates

__all__ = ["build_kb_router"]

log = structlog.get_logger(__name__)

#: Default number of entries per page for the empty-query list view.
_DEFAULT_PAGE_LIMIT: Final[int] = 50

#: Maximum page size to prevent absurdly large list renders.
_MAX_PAGE_LIMIT: Final[int] = 200

#: Maximum query length accepted by the search partial. The underlying
#: ``/api/v1/retrieve`` accepts up to 2000 characters; we cap earlier
#: at the UI layer to keep the URL representable and avoid embedding an
#: operator's free-form query verbatim in server logs.
_MAX_QUERY_LENGTH: Final[int] = 500

#: Module-level Depends closure for the require_ui_session gate.
#: Matches the ruff B008 idiom the topology and dashboard routes use.
_require_session = Depends(require_ui_session)


def _highlight_query_terms(snippet: str, query: str) -> str:
    """Wrap *query* terms in the *snippet* with ``<mark>`` tags.

    Server-side highlight for hover-preview and search-card snippets.
    Each whitespace-separated term in *query* is independently wrapped
    in ``<mark class="kb-term">`` (case-insensitive). Overlapping
    matches are handled by left-to-right non-overlapping replacement.

    The snippet is already HTML-escaped by Jinja2's autoescape before
    this function is called — this function operates on the plain-text
    snippet value and returns plain text that the template will wrap in
    ``{{ ... | safe }}`` after calling this helper at the route level.
    We use :class:`markupsafe.Markup` as the return type so the caller
    can pass it directly into a Jinja2 context with autoescape without
    double-escaping.
    """
    from markupsafe import Markup, escape

    if not query.strip():
        return Markup(escape(snippet))
    terms = [t for t in query.split() if t]
    escaped_snippet = str(escape(snippet))
    for term in terms:
        pattern = re.compile(re.escape(term), re.IGNORECASE)
        escaped_snippet = pattern.sub(
            lambda m: f'<mark class="kb-term">{escape(m.group(0))}</mark>',
            escaped_snippet,
        )
    return Markup(escaped_snippet)


def _make_snippet(body: str, max_chars: int = 200) -> str:
    """Return the first *max_chars* characters of *body* as a plain-text snippet.

    Strips leading/trailing whitespace. If the body is longer than
    *max_chars* the snippet is truncated at the nearest word boundary
    and an ellipsis appended so the card preview reads cleanly.
    """
    body = body.strip()
    if len(body) <= max_chars:
        return body
    truncated = body[:max_chars]
    last_space = truncated.rfind(" ")
    if last_space > max_chars // 2:
        truncated = truncated[:last_space]
    return truncated + "…"


def build_kb_router() -> APIRouter:
    """Construct the ``/ui/kb*`` :class:`APIRouter`.

    Factory function (not module-level constant) so a test app can
    construct parallel routers without shared state — same convention
    as :func:`meho_backplane.ui.routes.topology.build_router`.
    """
    router = APIRouter(tags=["ui-kb"])
    kb = KbService()

    @router.get("/ui/kb", response_class=HTMLResponse)
    async def kb_index(
        request: Request,
        session: UISessionContext = _require_session,
        q: str = Query(default="", max_length=_MAX_QUERY_LENGTH),
        limit: int = Query(default=_DEFAULT_PAGE_LIMIT, ge=1, le=_MAX_PAGE_LIMIT),
        offset: int = Query(default=0, ge=0),
    ) -> HTMLResponse:
        """Render the KB main page or HTMX results fragment.

        Empty *q* → paginated entry list.
        Non-empty *q* → hybrid BM25+cosine search results.
        ``HX-Request: true`` → return only the ``_results.html`` fragment.
        """
        csrf_token = mint_csrf_token(str(session.session_id))
        is_htmx = request.headers.get("HX-Request") == "true"
        query = q.strip()

        entries: list[KbEntry] = []
        hits: list[KbEntrySearchHit] = []
        has_more = False

        if query:
            hits = await kb.search_entries(
                session.tenant_id,
                query,
                limit=min(limit, 50),
            )
        else:
            entries = await kb.list_entries(
                session.tenant_id,
                limit=limit + 1,
                offset=offset,
            )
            has_more = len(entries) > limit
            entries = entries[:limit]

        context = {
            "query": query,
            "hits": hits,
            "entries": entries,
            "has_more": has_more,
            "limit": limit,
            "offset": offset,
            "next_offset": offset + limit,
            "prev_offset": max(0, offset - limit),
            "operator_sub": session.operator_sub,
            "csrf_token": csrf_token,
            "active_surface": "knowledge",
            "page_title": "Knowledge",
            "ready": False,
        }

        if is_htmx:
            response = get_templates().TemplateResponse(request, "kb/_results.html", context)
        else:
            response = get_templates().TemplateResponse(request, "kb/index.html", context)

        response.set_cookie(
            key=CSRF_COOKIE_NAME,
            value=csrf_token,
            httponly=False,
            secure=True,
            samesite="strict",
            path="/ui",
        )
        return response

    @router.post("/ui/kb/search", response_class=HTMLResponse)
    async def kb_search(
        request: Request,
        session: UISessionContext = _require_session,
        q: str = Form(default=""),
    ) -> HTMLResponse:
        """HTMX keyup-debounced search partial.

        Returns the ``kb/_results.html`` fragment swapped into
        ``#kb-results`` via ``hx-target``. The ``q`` field comes from
        the search form body; empty/blank → paginated list view (same
        behaviour as ``GET /ui/kb`` with empty query).
        """
        query = q.strip()
        entries: list[KbEntry] = []
        hits: list[KbEntrySearchHit] = []

        if query:
            hits = await kb.search_entries(
                session.tenant_id,
                query,
                limit=_DEFAULT_PAGE_LIMIT,
            )
        else:
            entries = await kb.list_entries(
                session.tenant_id,
                limit=_DEFAULT_PAGE_LIMIT,
                offset=0,
            )

        context = {
            "query": query,
            "hits": hits,
            "entries": entries,
            "has_more": False,
            "limit": _DEFAULT_PAGE_LIMIT,
            "offset": 0,
            "next_offset": _DEFAULT_PAGE_LIMIT,
            "prev_offset": 0,
            "operator_sub": session.operator_sub,
            "csrf_token": "",
            "active_surface": "knowledge",
        }
        return get_templates().TemplateResponse(request, "kb/_results.html", context)

    @router.get("/ui/kb/{slug}", response_class=HTMLResponse)
    async def kb_entry_detail(
        slug: str,
        request: Request,
        session: UISessionContext = _require_session,
    ) -> HTMLResponse:
        """Render the KB entry detail page with server-side Markdown.

        Fetches the entry via :meth:`KbService.get_entry`, renders the
        body via :func:`render_markdown` (markdown-it-py GFM + pygments
        code highlight), and passes the rendered HTML + pygments CSS to
        the ``kb/detail.html`` template.

        Cross-tenant or missing slugs → 404 (not 403, matching the
        ``/api/v1/kb/{slug}`` surface posture).
        """
        entry = await kb.get_entry(session.tenant_id, slug)
        if entry is None:
            raise HTTPException(status_code=404, detail="kb_entry_not_found")

        csrf_token = mint_csrf_token(str(session.session_id))
        rendered_body = render_markdown(entry.body)
        code_css = pygments_css()

        # Extract source_path from metadata if present (set by ingest pipeline).
        source_path: str | None = None
        raw_path = entry.metadata.get("path")
        if isinstance(raw_path, str):
            source_path = raw_path

        body_hash: str | None = None
        raw_hash = entry.metadata.get("body_hash")
        if isinstance(raw_hash, str):
            body_hash = raw_hash

        context = {
            "entry": entry,
            "rendered_body": rendered_body,
            "code_css": code_css,
            "source_path": source_path,
            "body_hash": body_hash,
            "operator_sub": session.operator_sub,
            "csrf_token": csrf_token,
            "active_surface": "knowledge",
            "page_title": f"{slug} · Knowledge",
            "ready": False,
        }
        response = get_templates().TemplateResponse(request, "kb/detail.html", context)
        response.set_cookie(
            key=CSRF_COOKIE_NAME,
            value=csrf_token,
            httponly=False,
            secure=True,
            samesite="strict",
            path="/ui",
        )
        return response

    @router.get("/ui/kb/{slug}/preview", response_class=HTMLResponse)
    async def kb_entry_preview(
        slug: str,
        request: Request,
        session: UISessionContext = _require_session,
        q: str = Query(default=""),
    ) -> HTMLResponse:
        """HTMX hover-preview partial.

        Returns the ``kb/_preview.html`` fragment with the matched snippet
        annotated with query-term highlight markup (server-side ``<mark>``
        spans). Called by ``hx-trigger="mouseenter delay:200ms"`` on result
        cards; the ``q`` param carries the active search query so terms can
        be highlighted in the preview.

        Cross-tenant or missing slugs → 404.
        """
        entry = await kb.get_entry(session.tenant_id, slug)
        if entry is None:
            raise HTTPException(status_code=404, detail="kb_entry_not_found")

        query = q.strip()
        snippet = _make_snippet(entry.body, max_chars=400)
        highlighted_snippet = _highlight_query_terms(snippet, query)

        context = {
            "entry": entry,
            "highlighted_snippet": highlighted_snippet,
            "query": query,
        }
        return get_templates().TemplateResponse(request, "kb/_preview.html", context)

    return router
