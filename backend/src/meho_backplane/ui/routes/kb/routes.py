# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""KB UI routes: list/search + entry detail + hover preview partial + editor.

Initiative #339 (G10.2 Knowledge base UI). Tasks #870 (T1) + #872 (T3).

T1 routes (read surface):

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

T3 routes (editor + mobile reflow):

* ``POST /ui/kb/editor-preview`` (HTMX editor preview partial) — accepts
  ``body`` as a form field, renders the Markdown via
  :func:`~meho_backplane.ui.routes.kb.render.render_markdown` (reusing
  the same renderer as the entry-detail view), and returns the
  ``kb/_editor_preview.html`` fragment. Called by the CodeMirror
  editor's debounced input listener via HTMX. Any authenticated
  operator can call this endpoint (preview is a read-only transform).

* ``POST /ui/kb/new`` (editor save) — accepts ``slug``, ``body``, and
  ``tags`` as form fields, validates ``tenant_admin`` role by loading
  the full session and re-verifying the access token through
  :func:`~meho_backplane.auth.jwt.verify_jwt_for_audience`, then calls
  :meth:`~meho_backplane.kb.KbService.create_entry`. Returns an HTMX
  redirect (``HX-Redirect``) to the new entry's detail page on success,
  or re-renders the editor modal with a visible error message on failure.

Tenant scoping
--------------

Every handler derives tenant identity from
:class:`~meho_backplane.ui.auth.middleware.UISessionContext`. There is
no query parameter or form field that overrides tenant — a cross-tenant
slug probe surfaces as 404, not 403, matching the ``/api/v1/kb``
surface's posture.

RBAC
----

``operator`` role minimum for read + preview routes (enforced by
:func:`~meho_backplane.ui.auth.middleware.require_ui_session`). The
``POST /ui/kb/new`` save route additionally requires ``tenant_admin``:
it loads the full decrypted session via
:func:`~meho_backplane.ui.auth.session_store.load_session`, re-verifies
the access token through
:func:`~meho_backplane.auth.jwt.verify_jwt_for_audience`, and returns
403 if the operator's ``tenant_role`` is below ``TENANT_ADMIN``. This
mirrors the ``/api/v1/kb POST`` RBAC posture without adding a separate
auth middleware layer.

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
from fastapi.responses import HTMLResponse, Response

from meho_backplane.auth.jwt import verify_jwt_for_audience
from meho_backplane.auth.operator import TenantRole
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.kb import KbEntry, KbEntrySearchHit, KbService
from meho_backplane.kb.schemas import InvalidKbSlugError
from meho_backplane.settings import get_settings
from meho_backplane.ui.auth.middleware import UISessionContext, require_ui_session
from meho_backplane.ui.auth.session_store import load_session
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

#: Maximum length of a slug submitted via the editor save form.
#: Mirrors :data:`meho_backplane.api.v1.kb._SLUG_MAX_LENGTH`.
_MAX_SLUG_LENGTH: Final[int] = 256

#: Maximum body size accepted by the editor preview partial. Generous cap
#: so in-progress large documents still render; the KB API enforces its
#: own body limits downstream.
_MAX_EDITOR_BODY_LENGTH: Final[int] = 65_536

#: Maximum length of the comma-separated tags field on the editor save form.
_MAX_TAGS_LENGTH: Final[int] = 500

#: Module-level Depends closure for the require_ui_session gate.
#: Matches the ruff B008 idiom the topology and dashboard routes use.
_require_session = Depends(require_ui_session)


async def _require_tenant_admin(session_ctx: UISessionContext) -> None:
    """Verify the session's access token carries at least ``tenant_admin`` role.

    Loads the full :class:`DecryptedSession` via
    :func:`~meho_backplane.ui.auth.session_store.load_session` so the
    plaintext access token is available for JWT re-verification.
    :func:`~meho_backplane.auth.jwt.verify_jwt_for_audience` re-runs the
    full JWKS chain (signature, claims, audience) and surfaces the
    ``tenant_role`` claim.

    Raises :class:`fastapi.HTTPException` 403 when the session's role is
    below ``TENANT_ADMIN`` or when the session has been revoked/expired
    between the middleware check and this call (extremely rare but
    possible under concurrent logout).
    """
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as db_session, db_session.begin():
        decrypted = await load_session(db_session, session_ctx.session_id)
    if decrypted is None:
        raise HTTPException(status_code=403, detail="session_not_found")
    settings = get_settings()
    operator = await verify_jwt_for_audience(
        f"Bearer {decrypted.access_token}",
        expected_audience=settings.keycloak_audience,
    )
    if operator.tenant_role != TenantRole.TENANT_ADMIN:
        raise HTTPException(status_code=403, detail="tenant_admin_required")


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
    pattern = re.compile(
        "|".join(re.escape(t) for t in terms),
        re.IGNORECASE,
    )
    result = pattern.sub(
        lambda m: f'<mark class="kb-term">{escape(m.group(0))}</mark>',
        escaped_snippet,
    )
    return Markup(result)


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
        q: str = Form(default="", max_length=_MAX_QUERY_LENGTH),
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

        has_more = False

        if query:
            hits = await kb.search_entries(
                session.tenant_id,
                query,
                limit=_DEFAULT_PAGE_LIMIT,
            )
        else:
            entries = await kb.list_entries(
                session.tenant_id,
                limit=_DEFAULT_PAGE_LIMIT + 1,
                offset=0,
            )
            has_more = len(entries) > _DEFAULT_PAGE_LIMIT
            entries = entries[:_DEFAULT_PAGE_LIMIT]

        context = {
            "query": query,
            "hits": hits,
            "entries": entries,
            "has_more": has_more,
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

    @router.post("/ui/kb/editor-preview", response_class=HTMLResponse)
    async def kb_editor_preview(
        request: Request,
        session: UISessionContext = _require_session,
        body: str = Form(default="", max_length=_MAX_EDITOR_BODY_LENGTH),
    ) -> HTMLResponse:
        """HTMX editor live-preview partial.

        Accepts the current editor ``body`` as a form field, renders it
        via :func:`render_markdown` (the same renderer as the entry-detail
        view), and returns the ``kb/_editor_preview.html`` fragment. Called
        by the CodeMirror editor's debounced HTMX POST (``hx-trigger="input
        changed delay:500ms"``). Any authenticated operator (not just
        ``tenant_admin``) can call this endpoint — it is a pure read-only
        Markdown transform, not a write.

        Tenant identity is present (from ``session``) but not needed here;
        the preview is stateless (no DB read) and cross-tenant safe because
        the body comes from the operator's own input, not a stored document.
        """
        rendered_body = render_markdown(body)
        code_css = pygments_css()
        context = {
            "rendered_body": rendered_body,
            "code_css": code_css,
        }
        return get_templates().TemplateResponse(request, "kb/_editor_preview.html", context)

    @router.post("/ui/kb/new", response_class=HTMLResponse)
    async def kb_editor_save(
        request: Request,
        session: UISessionContext = _require_session,
        slug: str = Form(..., max_length=_MAX_SLUG_LENGTH),
        body: str = Form(..., max_length=_MAX_EDITOR_BODY_LENGTH),
        tags: str = Form(default="", max_length=_MAX_TAGS_LENGTH),
    ) -> Response:
        """Save a new KB entry from the editor modal.

        Requires ``tenant_admin`` role — enforced by re-verifying the
        session's access token through
        :func:`~meho_backplane.auth.jwt.verify_jwt_for_audience`. Plain
        ``operator`` role receives 403. The CSRF middleware gate runs
        before this handler (``POST /ui/*`` passes through
        :class:`~meho_backplane.ui.csrf.CSRFMiddleware`).

        On success, returns ``HX-Redirect`` to the new entry's detail
        page so HTMX swaps the page without a full navigation. On
        validation failure (invalid slug, empty body) re-renders the
        editor modal with an inline error message.

        Tag parsing: the ``tags`` field is a comma-separated string
        (``"tag-a, tag-b"``); individual values are stripped of
        whitespace and empty strings are dropped. Tags are stored as
        ``metadata["tags"]`` on the entry.
        """
        await _require_tenant_admin(session)

        # Normalise tags: split on comma, strip whitespace, drop blanks.
        tag_list = [t.strip() for t in tags.split(",") if t.strip()]

        metadata: dict[str, object] = {}
        if tag_list:
            metadata["tags"] = tag_list

        error_message: str | None = None
        entry: KbEntry | None = None
        try:
            entry = await kb.create_entry(
                tenant_id=session.tenant_id,
                slug=slug.strip(),
                body=body,
                metadata=metadata if metadata else None,
            )
        except InvalidKbSlugError as exc:
            error_message = str(exc)
        except Exception:
            log.exception("kb_editor_save_unexpected_error", slug=slug)
            error_message = "Unexpected error saving entry. Please try again."

        if entry is not None:
            # HTMX redirect to the new entry's detail page.
            return Response(
                status_code=204,
                headers={"HX-Redirect": f"/ui/kb/{entry.slug}"},
            )

        # Re-render the editor modal with the error message.
        csrf_token = mint_csrf_token(str(session.session_id))
        context = {
            "error_message": error_message,
            "slug": slug,
            "body": body,
            "tags": tags,
            "csrf_token": csrf_token,
        }
        return get_templates().TemplateResponse(
            request, "kb/_editor_modal.html", context, status_code=422
        )

    return router
