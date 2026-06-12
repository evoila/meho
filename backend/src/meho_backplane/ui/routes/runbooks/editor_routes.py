# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Runbooks UI authoring editor: FastAPI route wiring.

Initiative #1381 (G10.6 Runbooks UI), Task #1383 (T2). The route layer for
the ``tenant_admin`` authoring surface -- the thin FastAPI registration that
maps the editor URLs to the form-handling logic in
:mod:`meho_backplane.ui.routes.runbooks.editor`. Split from that module so
neither file crosses the code-quality size gate (this file is wiring; the
other is form (de)serialisation + service calls).

Route inventory (all ``require_ui_admin``-gated):

* ``GET  /ui/runbooks/new``          -- render the blank-draft editor.
* ``POST /ui/runbooks/new``          -- create a draft (mirrors REST POST).
* ``POST /ui/runbooks/preview``      -- HTMX Markdown live-preview partial.
* ``GET  /ui/runbooks/{slug}/edit``  -- render the editor pre-loaded.
* ``POST /ui/runbooks/{slug}/edit``  -- edit-in-place / fork (mirrors REST PATCH).

The literal ``new`` / ``preview`` segments are registered BEFORE the catalog
factory's ``/ui/runbooks/{slug}`` route (FastAPI is first-match-wins), so
:func:`register_editor_routes` is called from
:func:`meho_backplane.ui.routes.runbooks.routes.build_runbooks_router` ahead
of that catch-all.
"""

from __future__ import annotations

from typing import Final

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, Response

from meho_backplane.runbooks.service import (
    RunbookTemplateService,
    TemplateNotFoundError,
)
from meho_backplane.ui.auth.middleware import UISessionContext, require_ui_admin
from meho_backplane.ui.csrf import mint_csrf_token
from meho_backplane.ui.routes.kb.render import pygments_css, render_markdown
from meho_backplane.ui.routes.runbooks.editor import (
    build_editor_context,
    empty_step,
    handle_editor_submit,
    set_csrf_cookie,
    template_to_form_steps,
)
from meho_backplane.ui.templating import get_templates

__all__ = ["register_editor_routes"]

#: Module-level ``Depends`` closure for the ``tenant_admin`` gate every
#: authoring route requires (B008 idiom -- no call in a default arg).
#: ``require_ui_admin`` chains ``require_ui_session`` and re-verifies the
#: session's access token to read the role claim, raising 403 for
#: ``operator`` / ``read_only``. The server is the single source of truth for
#: the authoring privilege; the client-side toggles are convenience only.
_require_admin = Depends(require_ui_admin)

#: Cap on the editor ``steps`` form payload (the JSON-serialised step tree).
#: A runbook template is a handful of steps with Markdown bodies; 256 KiB is
#: generous headroom while keeping the form parse bounded. The server-side
#: Pydantic validation is the real contract; this cap only guards against an
#: unbounded form submission before parsing.
_MAX_EDITOR_PAYLOAD_LENGTH: Final[int] = 256 * 1024

#: Cap on the live-preview ``body`` field (one step's Markdown body). Matches
#: the KB editor-preview cap so the same renderer sees the same ceiling.
_MAX_PREVIEW_BODY_LENGTH: Final[int] = 65_536


def _render_new_editor(request: Request, session: UISessionContext) -> HTMLResponse:
    """Render the editor for a brand-new draft (one blank manual+confirm step).

    Mints a fresh CSRF token + sets the cookie so the Alpine-driven form can
    echo it in ``X-CSRF-Token`` on the preview + save requests.
    """
    csrf_token = mint_csrf_token(str(session.session_id))
    context = build_editor_context(
        session,
        mode="new",
        slug="",
        title="",
        description="",
        target_kind="",
        form_steps=[empty_step()],
        csrf_token=csrf_token,
    )
    response = get_templates().TemplateResponse(request, "runbooks/editor.html", context)
    set_csrf_cookie(response, csrf_token)
    return response


async def _render_edit_editor(
    request: Request, session: UISessionContext, slug: str
) -> HTMLResponse:
    """Render the editor pre-loaded with an existing template's latest version.

    Loads the latest version via the service and flattens its steps into the
    editor's form shape. A missing / cross-tenant slug 404s (the service's
    tenant filter makes another tenant's row invisible).
    """
    try:
        template = await RunbookTemplateService().show_template(session.tenant_id, slug)
    except TemplateNotFoundError as exc:
        raise HTTPException(status_code=404, detail="runbook_template_not_found") from exc
    csrf_token = mint_csrf_token(str(session.session_id))
    context = build_editor_context(
        session,
        mode="edit",
        slug=template.slug,
        title=template.title,
        description=template.description,
        target_kind=template.target_kind or "",
        form_steps=template_to_form_steps(template),
        csrf_token=csrf_token,
    )
    response = get_templates().TemplateResponse(request, "runbooks/editor.html", context)
    set_csrf_cookie(response, csrf_token)
    return response


def register_editor_routes(router: APIRouter) -> None:
    """Register the ``require_ui_admin``-gated authoring routes on *router*.

    The T2 (#1383) editor surface: the editor GET pages (``/ui/runbooks/new``
    + ``/ui/runbooks/{slug}/edit``), the draft/edit POST handlers, and the
    Markdown live-preview POST (``/ui/runbooks/preview``). Called by
    :func:`meho_backplane.ui.routes.runbooks.routes.build_runbooks_router`
    after the read routes so the literal ``new`` / ``preview`` segments are
    registered BEFORE ``/ui/runbooks/{slug}`` (FastAPI is first-match-wins).
    Every route declares ``_require_admin``: the server is the single source of
    truth for the authoring privilege; an ``operator`` gets 403 at the
    dependency, before the handler body runs. The route bodies are thin
    delegations to the helpers above + in ``runbooks.editor``.
    """

    @router.get("/ui/runbooks/new", response_class=HTMLResponse)
    async def runbooks_editor_new(
        request: Request,
        session: UISessionContext = _require_admin,
    ) -> HTMLResponse:
        """Render the authoring editor for a brand-new draft template."""
        return _render_new_editor(request, session)

    @router.post("/ui/runbooks/new", response_class=HTMLResponse)
    async def runbooks_editor_create(
        request: Request,
        session: UISessionContext = _require_admin,
        slug: str = Form(default=""),
        title: str = Form(default=""),
        description: str = Form(default=""),
        target_kind: str = Form(default=""),
        steps: str = Form(default="[]", max_length=_MAX_EDITOR_PAYLOAD_LENGTH),
    ) -> Response:
        """Create a new draft from the editor form (mirrors REST POST)."""
        return await handle_editor_submit(
            request,
            session,
            mode="new",
            slug=slug.strip(),
            title=title,
            description=description,
            target_kind=target_kind,
            steps_json=steps,
        )

    @router.post("/ui/runbooks/preview", response_class=HTMLResponse)
    async def runbooks_editor_preview(
        request: Request,
        session: UISessionContext = _require_admin,
        body: str = Form(default="", max_length=_MAX_PREVIEW_BODY_LENGTH),
    ) -> HTMLResponse:
        """HTMX live-preview partial for a step's Markdown ``body`` (admin only)."""
        rendered_body = render_markdown(body)
        context = {"rendered_body": rendered_body, "code_css": pygments_css()}
        return get_templates().TemplateResponse(request, "runbooks/_editor_preview.html", context)

    @router.get("/ui/runbooks/{slug}/edit", response_class=HTMLResponse)
    async def runbooks_editor_edit(
        slug: str,
        request: Request,
        session: UISessionContext = _require_admin,
    ) -> HTMLResponse:
        """Render the authoring editor pre-loaded with an existing template."""
        return await _render_edit_editor(request, session, slug)

    @router.post("/ui/runbooks/{slug}/edit", response_class=HTMLResponse)
    async def runbooks_editor_update(
        slug: str,
        request: Request,
        session: UISessionContext = _require_admin,
        title: str = Form(default=""),
        description: str = Form(default=""),
        target_kind: str = Form(default=""),
        steps: str = Form(default="[]", max_length=_MAX_EDITOR_PAYLOAD_LENGTH),
    ) -> Response:
        """Edit a draft in place / fork from published (mirrors REST PATCH)."""
        return await handle_editor_submit(
            request,
            session,
            mode="edit",
            slug=slug.strip(),
            title=title,
            description=description,
            target_kind=target_kind,
            steps_json=steps,
        )
