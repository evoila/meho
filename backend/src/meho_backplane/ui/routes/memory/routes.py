# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Memory UI route registration -- maps HTTP verbs to the render helpers.

Initiative #341 (G10.4 Memory UI). Task #877 (T1) shipped the read +
edit + delete + tag-filter surface; Task #878 (T2) layers create +
scope-promotion on top. The route handlers in this module are thin
wrappers: parse FastAPI params, resolve the session / operator
dependency, and hand off to the render functions in
:mod:`~meho_backplane.ui.routes.memory.views` (T1 routes) or
:mod:`~meho_backplane.ui.routes.memory.create` +
:mod:`~meho_backplane.ui.routes.memory.promote` (T2 routes).
Splitting registration here from render logic keeps each module
under the chassis-wide ~600-line + ~100-line caps and gives the
render helpers a unit-testable seam (no FastAPI :class:`Request`
fixture required for projection logic).

Route inventory
---------------

T1 (#877) -- read + edit + delete + tag autocomplete:

* ``GET /ui/memory`` -- list page or HTMX card-list fragment.
* ``GET /ui/memory/tags`` -- HTMX datalist for the tag autocomplete.
* ``GET /ui/memory/<scope>/<slug>`` -- detail page or HTMX body fragment.
* ``GET /ui/memory/<scope>/<slug>/edit`` -- HTMX edit-form fragment.
* ``PATCH /ui/memory/<scope>/<slug>`` -- HTMX save the edited body.
* ``DELETE /ui/memory/<scope>/<slug>`` -- HTMX delete + re-render the list.

T2 (#878) -- create + scope-promotion:

* ``GET /ui/memory/create`` -- HTMX-loaded create modal fragment.
* ``POST /ui/memory/create`` -- submit handler; calls ``MemoryService.remember``.
* ``POST /ui/memory/preview`` -- HTMX-debounced server-side Markdown preview.
* ``GET /ui/memory/<scope>/<slug>/promote`` -- HTMX-loaded promote modal fragment.
* ``POST /ui/memory/<scope>/<slug>/promote`` -- submit handler; calls G5.2 promote.

T3 (#879) -- expiry visualization + bulk:

* ``POST /ui/memory/bulk`` -- HTMX bulk delete / bulk extend-expiry.

Tenant + RBAC + info-leak posture are documented on the render
functions themselves; this module owns only the path / method /
dependency wiring.

Route registration order is **load-bearing for the static-prefix
verbs** -- ``/ui/memory/create`` MUST register before
``/ui/memory/{scope}/{slug}`` because FastAPI matches the first
route whose path template fits, and ``{scope}`` would otherwise
consume the literal ``"create"`` token. ``/ui/memory/tags`` already
relies on the same ordering for T1; the create + preview routes
inherit the convention.
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import HTMLResponse

from meho_backplane.auth.operator import Operator
from meho_backplane.memory import MemoryScope
from meho_backplane.memory.ttl import resolve_default_expires_at
from meho_backplane.ui.auth.middleware import UISessionContext, require_ui_session
from meho_backplane.ui.routes.memory._modal_shared import (
    TAGS_MAX_LENGTH,
    TARGET_NAME_MAX_LENGTH,
)
from meho_backplane.ui.routes.memory.create import (
    create_entry,
    render_body_preview,
    render_create_modal,
)
from meho_backplane.ui.routes.memory.operator import resolve_ui_operator
from meho_backplane.ui.routes.memory.promote import promote_entry, render_promote_modal
from meho_backplane.ui.routes.memory.views import (
    BODY_MAX_LENGTH,
    SCOPE_ALL,
    SLUG_MAX_LENGTH,
    delete_entry,
    patch_entry,
    render_bulk_action,
    render_detail,
    render_edit_form,
    render_index,
    render_tags,
    validate_slug,
)

__all__ = ["build_memory_router"]

#: Module-level :class:`Depends` closures -- ruff B008 idiom mirroring
#: the chassis dashboard + topology routes (no calls in default
#: argument positions).
_require_session_dep = Depends(require_ui_session)
_resolve_operator_dep = Depends(resolve_ui_operator)


async def _list_handler(
    request: Request,
    scope: str = Query(default=SCOPE_ALL, max_length=64),
    tag: str | None = Query(default=None, max_length=SLUG_MAX_LENGTH),
    session_ctx: UISessionContext = _require_session_dep,
) -> HTMLResponse:
    """``GET /ui/memory`` -- list page or HTMX card-list fragment."""
    return await render_index(request, session_ctx, scope=scope, tag=tag)


async def _tags_handler(
    request: Request,
    session_ctx: UISessionContext = _require_session_dep,
) -> HTMLResponse:
    """``GET /ui/memory/tags`` -- HTMX datalist autocomplete fragment."""
    return await render_tags(request, session_ctx)


async def _detail_handler(
    request: Request,
    scope: MemoryScope,
    slug: str,
    session_ctx: UISessionContext = _require_session_dep,
) -> HTMLResponse:
    """``GET /ui/memory/<scope>/<slug>`` -- detail page or HTMX body fragment."""
    validate_slug(slug)
    return await render_detail(request, session_ctx, scope=scope, slug=slug)


async def _edit_form_handler(
    request: Request,
    scope: MemoryScope,
    slug: str,
    session_ctx: UISessionContext = _require_session_dep,
    operator: Operator = _resolve_operator_dep,
) -> HTMLResponse:
    """``GET /ui/memory/<scope>/<slug>/edit`` -- HTMX edit-form fragment."""
    validate_slug(slug)
    return await render_edit_form(request, session_ctx, operator, scope=scope, slug=slug)


async def _patch_handler(
    request: Request,
    scope: MemoryScope,
    slug: str,
    body: str = Form(..., max_length=BODY_MAX_LENGTH),
    session_ctx: UISessionContext = _require_session_dep,
    operator: Operator = _resolve_operator_dep,
) -> HTMLResponse:
    """``PATCH /ui/memory/<scope>/<slug>`` -- save the edited body."""
    validate_slug(slug)
    return await patch_entry(request, session_ctx, operator, scope=scope, slug=slug, new_body=body)


async def _delete_handler(
    request: Request,
    scope: MemoryScope,
    slug: str,
    session_ctx: UISessionContext = _require_session_dep,
    operator: Operator = _resolve_operator_dep,
) -> HTMLResponse:
    """``DELETE /ui/memory/<scope>/<slug>`` -- delete + re-render the list."""
    validate_slug(slug)
    return await delete_entry(request, session_ctx, operator, scope=scope, slug=slug)


# ---------------------------------------------------------------------------
# T2 (#878) -- create modal + scope-promotion handlers
# ---------------------------------------------------------------------------


async def _create_modal_handler(
    request: Request,
    session_ctx: UISessionContext = _require_session_dep,
    operator: Operator = _resolve_operator_dep,
) -> HTMLResponse:
    """``GET /ui/memory/create`` -- HTMX-loaded create modal fragment."""
    return await render_create_modal(request, session_ctx, operator)


async def _create_submit_handler(
    request: Request,
    scope: MemoryScope = Form(...),
    body: str = Form(..., max_length=BODY_MAX_LENGTH),
    slug: str | None = Form(default=None, max_length=SLUG_MAX_LENGTH),
    tags: str | None = Form(default=None, max_length=TAGS_MAX_LENGTH),
    expires_at: datetime | None = Form(default=None),
    target_name: str | None = Form(default=None, max_length=TARGET_NAME_MAX_LENGTH),
    session_ctx: UISessionContext = _require_session_dep,
    operator: Operator = _resolve_operator_dep,
) -> HTMLResponse:
    """``POST /ui/memory/create`` -- create one memory via the service.

    Returns 204 + ``HX-Redirect: /ui/memory`` so HTMX navigates back
    to the list. The form-encoded body shape mirrors the
    ``RememberBody`` Pydantic model on ``/api/v1/memory``: scope is
    a ``MemoryScope`` enum value, body is required, slug + tags +
    expires_at + target_name are optional. CSRF is enforced by the
    chassis :class:`CSRFMiddleware` before this handler runs.

    ``tags`` is a comma-separated string parsed server-side into a
    list; the form input is one ``<input>`` rather than five (the
    typical UX shape for the v0.2 surface). ``expires_at`` is
    parsed by FastAPI's :class:`datetime` form coercion from the
    ISO-8601 string a ``<input type="datetime-local">`` produces.

    ``expires_at`` then runs through the shared G5.2-T2 (#624)
    default-TTL resolver before the persist -- same seam the REST
    ``remember`` route and the MCP ``add_to_memory`` tool consume
    (#1697): a blank picker on a ``user``-scope create injects
    ``now(UTC) + Settings.memory_user_default_ttl_days``, non-``user``
    scopes stay ``None``, and an explicit timestamp is honoured
    verbatim. The surface-native "field was set" discriminant is
    ``expires_at is not None``: FastAPI coerces both an absent field
    and the empty string a blank ``datetime-local`` input submits to
    ``None`` (verified against the installed FastAPI 0.136.3), and a
    browser form cannot express the REST surface's explicit-``null``
    opt-out, so set-vs-unset collapses to datetime-vs-``None`` here.
    """
    return await create_entry(
        request,
        session_ctx,
        operator,
        scope=scope,
        body=body,
        slug=slug,
        tags_raw=tags,
        expires_at=resolve_default_expires_at(
            scope,
            expires_at_was_set=expires_at is not None,
            explicit_expires_at=expires_at,
        ),
        target_name=target_name,
    )


async def _preview_handler(
    request: Request,
    body: str = Form(default="", max_length=BODY_MAX_LENGTH),
    session_ctx: UISessionContext = _require_session_dep,
) -> HTMLResponse:
    """``POST /ui/memory/preview`` -- debounced server-side Markdown preview.

    Triggered by ``hx-trigger="keyup changed delay:300ms"`` on the
    create-modal body textarea. Returns the rendered Markdown
    wrapped in the same ``<article>`` shape :file:`_body_view.html`
    uses so the preview pane and the detail view render identically.

    Session is required (the chassis CSRF middleware would already
    have rejected an anonymous POST, but the session dep adds the
    audit-trail anchor; ``_require_session_dep`` runs the standard
    redirect-to-login on a missing cookie).
    """
    del session_ctx  # used only to gate access; no per-render context.
    return await render_body_preview(request, body=body)


async def _promote_modal_handler(
    request: Request,
    scope: MemoryScope,
    slug: str,
    session_ctx: UISessionContext = _require_session_dep,
    operator: Operator = _resolve_operator_dep,
) -> HTMLResponse:
    """``GET /ui/memory/<scope>/<slug>/promote`` -- HTMX-loaded promote modal."""
    validate_slug(slug)
    return await render_promote_modal(request, session_ctx, operator, scope=scope, slug=slug)


async def _promote_submit_handler(
    request: Request,
    scope: MemoryScope,
    slug: str,
    to: MemoryScope = Form(...),
    target_name: str | None = Form(default=None, max_length=TARGET_NAME_MAX_LENGTH),
    session_ctx: UISessionContext = _require_session_dep,
    operator: Operator = _resolve_operator_dep,
) -> HTMLResponse:
    """``POST /ui/memory/<scope>/<slug>/promote`` -- promote and HX-Redirect.

    Delegates ladder + authority checks to G5.2's
    :func:`assert_can_promote` via :meth:`MemoryService.promote`.
    Idempotent re-run returns the same target row -- the redirect
    URL is the same on first promote and on a re-promote, matching
    the G5.2 contract.
    """
    validate_slug(slug)
    return await promote_entry(
        request,
        session_ctx,
        operator,
        source_scope=scope,
        source_slug=slug,
        target_scope=to,
        target_name=target_name,
    )


# ---------------------------------------------------------------------------
# T3 (#879) -- bulk delete / bulk extend-expiry handler
# ---------------------------------------------------------------------------


async def _bulk_handler(
    request: Request,
    action: str = Form(..., max_length=16),
    # The HTMX form posts ``ids`` as a multi-value field (one per
    # checked checkbox). FastAPI's ``Form(...)`` with a ``list[str]``
    # annotation collects every same-named entry into the list.
    ids: list[str] = Form(default_factory=list),
    extend_duration: str | None = Form(default=None, max_length=8),
    # Active scope + tag are echoed back as hidden fields so the
    # post-bulk render preserves the operator's filter state.
    scope: str = Form(default=SCOPE_ALL, max_length=64),
    tag: str | None = Form(default=None, max_length=SLUG_MAX_LENGTH),
    session_ctx: UISessionContext = _require_session_dep,
    operator: Operator = _resolve_operator_dep,
) -> HTMLResponse:
    """``POST /ui/memory/bulk`` -- T3 (#879) bulk delete / bulk extend-expiry."""
    return await render_bulk_action(
        request,
        session_ctx,
        operator,
        action=action,
        raw_ids=ids,
        extend_duration=extend_duration,
        scope=scope,
        tag=tag,
    )


def _register_static_prefix_routes(router: APIRouter) -> None:
    """Wire the static-prefix routes -- ``/ui/memory/tags``, ``/ui/memory/create``,
    ``/ui/memory/preview`` -- onto *router*.

    Registration order is **load-bearing**: these static-prefix routes
    MUST register before :func:`_register_parametrized_routes`. FastAPI
    matches the first route whose path template fits, and a request to
    ``/ui/memory/create`` would otherwise be routed to
    ``/ui/memory/{scope}/{slug}`` with ``scope="create"`` -- a 422
    response from the ``MemoryScope`` enum validator, instead of the
    intended modal fragment.

    The ``ui_memory_create_*`` + ``ui_memory_preview`` names are picked
    so a future ``url_for(...)`` from a template resolves cleanly; the
    same convention :func:`_register_parametrized_routes` uses.
    """
    router.add_api_route(
        "/ui/memory/tags",
        _tags_handler,
        methods=["GET"],
        name="ui_memory_tags",
        response_class=HTMLResponse,
    )
    router.add_api_route(
        "/ui/memory/create",
        _create_modal_handler,
        methods=["GET"],
        name="ui_memory_create_modal",
        response_class=HTMLResponse,
    )
    router.add_api_route(
        "/ui/memory/create",
        _create_submit_handler,
        methods=["POST"],
        name="ui_memory_create_submit",
        response_class=HTMLResponse,
    )
    router.add_api_route(
        "/ui/memory/preview",
        _preview_handler,
        methods=["POST"],
        name="ui_memory_preview",
        response_class=HTMLResponse,
    )


def _register_read_routes(router: APIRouter) -> None:
    """Wire the parametrised GET routes (list / detail / edit-form) onto *router*.

    Split out of :func:`build_memory_router` so the factory body stays
    under the chassis-wide ~100-line cap. ``ui_memory_*`` names match
    the per-handler closures so a future ``url_for(...)`` from a
    template resolves cleanly.

    Must register **after** :func:`_register_static_prefix_routes` --
    the static-prefix routes (``/ui/memory/tags``, ``/ui/memory/create``,
    ``/ui/memory/preview``) carry literal tokens that would otherwise
    bind to the ``{scope}`` parameter and 422 against
    :class:`MemoryScope` validation.
    """
    router.add_api_route(
        "/ui/memory",
        _list_handler,
        methods=["GET"],
        name="ui_memory_list",
        response_class=HTMLResponse,
    )
    router.add_api_route(
        "/ui/memory/{scope}/{slug}",
        _detail_handler,
        methods=["GET"],
        name="ui_memory_detail",
        response_class=HTMLResponse,
    )
    router.add_api_route(
        "/ui/memory/{scope}/{slug}/edit",
        _edit_form_handler,
        methods=["GET"],
        name="ui_memory_edit_form",
        response_class=HTMLResponse,
    )
    router.add_api_route(
        "/ui/memory/{scope}/{slug}/promote",
        _promote_modal_handler,
        methods=["GET"],
        name="ui_memory_promote_modal",
        response_class=HTMLResponse,
    )


def _register_write_routes(router: APIRouter) -> None:
    """Wire the PATCH + DELETE + promote-submit + bulk POST routes onto *router*.

    Split out of :func:`build_memory_router` for the same reason as
    :func:`_register_read_routes`. The PATCH route shares the
    ``/ui/memory/{scope}/{slug}`` path with the detail GET; FastAPI
    distinguishes by method so registration order is not load-bearing.
    The promote-submit route lives here (not in the create-promote
    group) because its parametrised path means it can register after
    the static-prefix routes without any ordering hazard.

    ``POST /ui/memory/bulk`` (T3 #879) is registered ahead of the
    parameterised PATCH/DELETE so the literal ``/bulk`` path segment
    is never mistaken for a scope value (the scope path param is
    typed as :class:`MemoryScope` so a stray ``bulk`` would 422 from
    FastAPI's converter, but registering the literal first keeps the
    OpenAPI surface clean).
    """
    router.add_api_route(
        "/ui/memory/bulk",
        _bulk_handler,
        methods=["POST"],
        name="ui_memory_bulk",
        response_class=HTMLResponse,
    )
    router.add_api_route(
        "/ui/memory/{scope}/{slug}",
        _patch_handler,
        methods=["PATCH"],
        name="ui_memory_patch",
        response_class=HTMLResponse,
    )
    router.add_api_route(
        "/ui/memory/{scope}/{slug}",
        _delete_handler,
        methods=["DELETE"],
        name="ui_memory_delete",
        response_class=HTMLResponse,
    )
    router.add_api_route(
        "/ui/memory/{scope}/{slug}/promote",
        _promote_submit_handler,
        methods=["POST"],
        name="ui_memory_promote_submit",
        response_class=HTMLResponse,
    )


def build_memory_router() -> APIRouter:
    """Construct the memory UI :class:`APIRouter`.

    Factory function (not a module-level constant) so a test app can
    construct parallel routers without sharing route state -- mirrors
    the broadcast / topology / dashboard convention.

    Registration order is load-bearing for the static-prefix routes
    (``/ui/memory/tags``, ``/ui/memory/create``, ``/ui/memory/preview``):
    they must register before the parametrised routes so a literal
    ``"create"`` token in the URL doesn't bind to ``{scope}``.
    """
    router = APIRouter(tags=["ui-memory"])
    _register_static_prefix_routes(router)
    _register_read_routes(router)
    _register_write_routes(router)
    return router
