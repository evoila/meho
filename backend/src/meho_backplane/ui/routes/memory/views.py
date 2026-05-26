# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Render helpers + projections for the memory UI surface.

Initiative #341 (G10.4 Memory UI), Task #877 (T1). Pulled out of
:mod:`~meho_backplane.ui.routes.memory.routes` so the route handlers
stay thin signature wrappers and the render logic + projection
helpers can be unit-tested without an HTTP layer.

The split is by responsibility:

* **Constants + parsing helpers** -- :data:`_SCOPE_ALL`,
  :data:`_SCOPE_TABS`, :func:`resolve_scope_filter`, :func:`is_htmx_request`,
  :func:`validate_slug`. Pure functions; no FastAPI / service deps.
* **Projection helpers** -- :func:`preview`, :func:`entry_tags`,
  :func:`collect_visible_tags`, :func:`entries_with_preview`,
  :func:`detail_context`. Transform :class:`MemoryEntry` into the
  dict shape Jinja consumes; no DB / network IO.
* **Render functions** -- :func:`render_index`, :func:`render_detail`,
  :func:`render_edit_form`, :func:`render_tags`, :func:`patch_entry`,
  :func:`delete_entry`. The route handlers in
  :mod:`~meho_backplane.ui.routes.memory.routes` are thin wrappers
  around these.

The render functions take the FastAPI :class:`Request` so they can
return :class:`HTMLResponse` via :class:`Jinja2Templates`; they do
not declare FastAPI :class:`Depends` parameters themselves (the
route layer is responsible for resolving deps and passing them
through).
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Final

import structlog
from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse

from meho_backplane.auth.operator import Operator
from meho_backplane.memory import (
    MemoryEntry,
    MemoryRbacResolver,
    MemoryScope,
    MemoryService,
    PermissionDeniedError,
)
from meho_backplane.ui.auth.middleware import UISessionContext
from meho_backplane.ui.csrf import CSRF_COOKIE_NAME, mint_csrf_token
from meho_backplane.ui.routes.memory.operator import build_read_operator
from meho_backplane.ui.routes.memory.render import pygments_css, render_markdown
from meho_backplane.ui.templating import get_templates

__all__ = [
    "BODY_MAX_LENGTH",
    "LIST_LIMIT",
    "SCOPE_ALL",
    "SCOPE_TABS",
    "SLUG_MAX_LENGTH",
    "TAG_AUTOCOMPLETE_LIMIT",
    "delete_entry",
    "is_htmx_request",
    "patch_entry",
    "render_detail",
    "render_edit_form",
    "render_index",
    "render_tags",
    "resolve_scope_filter",
    "validate_slug",
]

_log = structlog.get_logger(__name__)

#: Maximum length of any path slug accepted by the detail / edit /
#: patch / delete routes. Mirrors
#: :data:`meho_backplane.api.v1.memory._SLUG_MAX_LENGTH` so a slug
#: that passes the API surface also passes here.
SLUG_MAX_LENGTH: Final[int] = 256

#: Maximum length of the body field accepted by the edit-in-place
#: save. The substrate has no fixed cap (``Document.body`` is
#: ``TEXT``); 64 KiB is well above the consumer-needs-named ~15
#: file memory corpus while bounding the worst-case allocation
#: against a paste-from-clipboard accident.
BODY_MAX_LENGTH: Final[int] = 64 * 1024

#: Maximum number of memories pulled per list fetch. Aligned with
#: the ``/api/v1/memory`` ``list`` route cap so the UI never sees a
#: row the API would have suppressed.
LIST_LIMIT: Final[int] = 500

#: Maximum count of distinct tags rendered into the autocomplete
#: datalist. The operator picks from a finite vocabulary; surfacing
#: more than a couple hundred kills the input lag and isn't useful.
TAG_AUTOCOMPLETE_LIMIT: Final[int] = 200

#: Sentinel for the "show every visible scope" tab. Distinct from
#: the :class:`MemoryScope` enum values so the route's scope param is
#: a closed union of (one-of-five-scopes, ``"all"``); a typo on the
#: query string surfaces as 422.
SCOPE_ALL: Final[str] = "all"

#: Ordered (label, value) for the scope tabs in the template.
SCOPE_TABS: Final[tuple[tuple[str, str], ...]] = (
    ("User", MemoryScope.USER.value),
    ("User x Tenant", MemoryScope.USER_TENANT.value),
    ("User x Target", MemoryScope.USER_TARGET.value),
    ("Tenant", MemoryScope.TENANT.value),
    ("Target", MemoryScope.TARGET.value),
    ("All visible", SCOPE_ALL),
)

#: Length of the body preview rendered on each card. The issue body
#: names "200-char preview"; pinned as a constant so the template
#: doesn't hard-code the slice and the test can pin the cap without
#: scraping HTML.
_PREVIEW_CHARS: Final[int] = 200

#: Slug pattern is constrained by :data:`SLUG_PATTERN` at the schema
#: layer; the route surface re-validates so a malformed slug arrives
#: as 404 (info-leak avoidance) rather than reaching the service's
#: regex check.
_SLUG_PATTERN_RE: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9_\-\.]+$")


# ---------------------------------------------------------------------------
# Parsing + small pure helpers
# ---------------------------------------------------------------------------


def is_htmx_request(request: Request) -> bool:
    """Return ``True`` when the request was issued by HTMX.

    HTMX 2 sets ``HX-Request: true`` on every fetch its directives
    drive (see https://htmx.org/reference/#request_headers).
    """
    return request.headers.get("hx-request", "").lower() == "true"


def resolve_scope_filter(scope: str) -> MemoryScope | None:
    """Parse the tab's ``scope`` query value.

    ``"all"`` -> ``None`` (every visible scope). A valid
    :class:`MemoryScope` -> that scope. Anything else -> 422 via
    :class:`HTTPException` so a typo on the URL doesn't silently
    collapse to "all".
    """
    if scope == SCOPE_ALL:
        return None
    try:
        return MemoryScope(scope)
    except ValueError as exc:
        raise HTTPException(
            status_code=422,
            detail=(
                f"scope must be one of {[s.value for s in MemoryScope]} or 'all'; got {scope!r}"
            ),
        ) from exc


def validate_slug(slug: str) -> None:
    """Translate a malformed slug into 404 at the path-parameter stage.

    Defence-in-depth before the service-layer regex check. Mirrors
    :mod:`meho_backplane.api.v1.memory`'s posture: malformed slugs
    surface as 404 (info-leak avoidance), not 422.
    """
    if len(slug) > SLUG_MAX_LENGTH or not _SLUG_PATTERN_RE.fullmatch(slug):
        raise HTTPException(status_code=404, detail="memory_not_found")


def _preview(body: str) -> str:
    """Truncate *body* to the card-preview length without splitting words."""
    stripped = body.strip()
    if len(stripped) <= _PREVIEW_CHARS:
        return stripped
    head = stripped[:_PREVIEW_CHARS]
    last_space = head.rfind(" ")
    if last_space > 0:
        head = head[:last_space]
    return head + "..."


def _entry_tags(entry: MemoryEntry) -> list[str]:
    """Extract the ``tags`` list from metadata defensively."""
    raw = entry.metadata.get("tags")
    if not isinstance(raw, list):
        return []
    return [t for t in raw if isinstance(t, str)]


def _collect_visible_tags(entries: Iterable[MemoryEntry], limit: int) -> list[str]:
    """Build a sorted unique list of tags seen across *entries*."""
    seen: set[str] = set()
    for entry in entries:
        for tag in _entry_tags(entry):
            if not tag:
                continue
            seen.add(tag)
            if len(seen) >= limit:
                break
        if len(seen) >= limit:
            break
    return sorted(seen)


def _entries_with_preview(entries: list[MemoryEntry]) -> list[dict[str, object]]:
    """Project ``MemoryEntry`` rows into the dict shape the template renders."""
    return [
        {
            "id": str(entry.id),
            "scope": entry.scope.value,
            "slug": entry.slug,
            "preview": _preview(entry.body),
            "user_sub": entry.user_sub,
            "target_name": entry.target_name,
            "expires_at": entry.expires_at,
            "tags": _entry_tags(entry),
            "updated_at": entry.updated_at,
        }
        for entry in entries
    ]


def _can_write(operator: Operator, entry: MemoryEntry) -> bool:
    """Return ``True`` when the operator can edit / delete *entry*."""
    return MemoryRbacResolver().can_write(operator, entry.scope, entry.target_name)


def _set_csrf_cookie(response: HTMLResponse, csrf_token: str) -> None:
    """Mirror the dashboard's CSRF cookie posture for state-changing pages."""
    response.set_cookie(
        key=CSRF_COOKIE_NAME,
        value=csrf_token,
        httponly=False,
        secure=True,
        samesite="strict",
        path="/ui",
    )


def _common_template_context(
    session_ctx: UISessionContext,
    csrf_token: str,
) -> dict[str, object]:
    """Build the dict shared across every memory template render."""
    return {
        "page_title": "Memory",
        "active_surface": "memory",
        "ready": False,
        "operator_sub": session_ctx.operator_sub,
        "csrf_token": csrf_token,
    }


def _detail_context(entry: MemoryEntry, *, can_write: bool) -> dict[str, object]:
    """Project a :class:`MemoryEntry` into the detail-template shape."""
    return {
        "id": str(entry.id),
        "scope": entry.scope.value,
        "slug": entry.slug,
        "body_raw": entry.body,
        "body_html": render_markdown(entry.body),
        "user_sub": entry.user_sub,
        "target_name": entry.target_name,
        "expires_at": entry.expires_at,
        "tags": _entry_tags(entry),
        "created_at": entry.created_at,
        "updated_at": entry.updated_at,
        "can_write": can_write,
    }


async def _list_for_render(
    operator: Operator,
    scope_filter: MemoryScope | None,
    tag_filter: str | None,
) -> list[MemoryEntry]:
    """Pull the list-view rows for *operator* under the active filters."""
    service = MemoryService()
    return await service.list_memories(
        operator=operator,
        scope=scope_filter,
        tag=tag_filter or None,
        include_expired=False,
        limit=LIST_LIMIT,
    )


async def _fetch_entry_or_404(
    operator: Operator,
    scope: MemoryScope,
    slug: str,
) -> MemoryEntry:
    """Pull one memory by natural key. 404 on missing OR RBAC-deny.

    The 404-vs-403 collapse is the info-leak avoidance the
    ``/api/v1/memory`` route holds: a caller cannot distinguish
    "no such memory" from "you can't read it" by the response status.
    """
    service = MemoryService()
    entry = await service.recall(operator=operator, scope=scope, slug=slug, target_name=None)
    if entry is None:
        raise HTTPException(status_code=404, detail="memory_not_found")
    return entry


# ---------------------------------------------------------------------------
# Render entry points (consumed by the route handlers)
# ---------------------------------------------------------------------------


async def render_index(
    request: Request,
    session_ctx: UISessionContext,
    *,
    scope: str = SCOPE_ALL,
    tag: str | None = None,
    flash: str | None = None,
) -> HTMLResponse:
    """Render the list page or the HTMX card-list fragment."""
    scope_filter = resolve_scope_filter(scope)
    operator = build_read_operator(session_ctx)
    entries = await _list_for_render(operator, scope_filter, tag)
    csrf_token = mint_csrf_token(str(session_ctx.session_id))
    context: dict[str, object] = {
        **_common_template_context(session_ctx, csrf_token),
        "scope_tabs": SCOPE_TABS,
        "active_scope": scope,
        "active_tag": tag or "",
        "entries": _entries_with_preview(entries),
        "entry_count": len(entries),
        "flash": flash or "",
    }
    template_name = "memory/_cards.html" if is_htmx_request(request) else "memory/index.html"
    response = get_templates().TemplateResponse(request, template_name, context)
    _set_csrf_cookie(response, csrf_token)
    return response


async def render_detail(
    request: Request,
    session_ctx: UISessionContext,
    *,
    scope: MemoryScope,
    slug: str,
) -> HTMLResponse:
    """Render the detail page or the HTMX body fragment."""
    operator = build_read_operator(session_ctx)
    entry = await _fetch_entry_or_404(operator, scope, slug)
    csrf_token = mint_csrf_token(str(session_ctx.session_id))
    context: dict[str, object] = {
        **_common_template_context(session_ctx, csrf_token),
        "entry": _detail_context(entry, can_write=_can_write(operator, entry)),
        "pygments_css": pygments_css(),
    }
    template_name = "memory/_body_view.html" if is_htmx_request(request) else "memory/detail.html"
    response = get_templates().TemplateResponse(request, template_name, context)
    _set_csrf_cookie(response, csrf_token)
    return response


async def render_edit_form(
    request: Request,
    session_ctx: UISessionContext,
    operator: Operator,
    *,
    scope: MemoryScope,
    slug: str,
) -> HTMLResponse:
    """Render the HTMX edit-form fragment.

    403 when the operator cannot write the scope -- the template gate
    is UX, this is the server-side gate. The fragment carries the
    existing body as the textarea's default value so a cancel-then-
    edit roundtrip doesn't lose work.
    """
    entry = await _fetch_entry_or_404(operator, scope, slug)
    if not _can_write(operator, entry):
        raise HTTPException(status_code=403, detail="permission_denied")
    csrf_token = mint_csrf_token(str(session_ctx.session_id))
    context: dict[str, object] = {
        **_common_template_context(session_ctx, csrf_token),
        "entry": _detail_context(entry, can_write=True),
        "max_body_length": BODY_MAX_LENGTH,
    }
    response = get_templates().TemplateResponse(request, "memory/_body_edit.html", context)
    _set_csrf_cookie(response, csrf_token)
    return response


async def render_tags(
    request: Request,
    session_ctx: UISessionContext,
) -> HTMLResponse:
    """Return the tag-autocomplete datalist fragment."""
    operator = build_read_operator(session_ctx)
    entries = await _list_for_render(operator, scope_filter=None, tag_filter=None)
    tags = _collect_visible_tags(entries, TAG_AUTOCOMPLETE_LIMIT)
    context = {"tags": tags}
    return get_templates().TemplateResponse(request, "memory/_tags_options.html", context)


def _validate_body_or_422(new_body: str) -> None:
    """Surface the two body-shape guards as 422 errors."""
    if len(new_body) > BODY_MAX_LENGTH:
        raise HTTPException(
            status_code=422,
            detail=f"body too large (max {BODY_MAX_LENGTH} chars)",
        )
    if not new_body.strip():
        raise HTTPException(status_code=422, detail="body must not be empty")


def _strip_service_bookkeeping(metadata: dict[str, object]) -> dict[str, object]:
    """Drop the service-owned bookkeeping keys before re-passing metadata."""
    return {
        k: v
        for k, v in metadata.items()
        if k not in {"scope", "user_sub", "target_name", "expires_at"}
    }


async def patch_entry(
    request: Request,
    session_ctx: UISessionContext,
    operator: Operator,
    *,
    scope: MemoryScope,
    slug: str,
    new_body: str,
) -> HTMLResponse:
    """Persist the edit and return the updated body fragment.

    Two-step: ``recall`` (preserves metadata + RBAC re-check) then
    ``remember`` with the same natural key (the service-layer
    ``index_document`` upserts on ``(tenant_id, source, source_id)``,
    so the row body + ``updated_at`` change while ``created_at`` /
    ``id`` stay put). Tags + caller metadata ride through the save
    unchanged; ``expires_at`` is preserved so an operator's edit
    doesn't accidentally clear the row's TTL.
    """
    _validate_body_or_422(new_body)

    service = MemoryService()
    existing = await service.recall(operator=operator, scope=scope, slug=slug, target_name=None)
    if existing is None:
        raise HTTPException(status_code=404, detail="memory_not_found")
    if not _can_write(operator, existing):
        raise HTTPException(status_code=403, detail="permission_denied")

    try:
        await service.remember(
            operator=operator,
            scope=scope,
            body=new_body,
            slug=slug,
            metadata=_strip_service_bookkeeping(existing.metadata),
            expires_at=existing.expires_at,
            target_name=existing.target_name,
        )
    except PermissionDeniedError as exc:
        raise HTTPException(status_code=403, detail=f"permission_denied: {exc.reason}") from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    updated = await _fetch_entry_or_404(operator, scope, slug)
    csrf_token = mint_csrf_token(str(session_ctx.session_id))
    context: dict[str, object] = {
        **_common_template_context(session_ctx, csrf_token),
        "entry": _detail_context(updated, can_write=True),
        "pygments_css": pygments_css(),
    }
    response = get_templates().TemplateResponse(request, "memory/_body_view.html", context)
    _set_csrf_cookie(response, csrf_token)
    return response


async def delete_entry(
    request: Request,
    session_ctx: UISessionContext,
    operator: Operator,
    *,
    scope: MemoryScope,
    slug: str,
) -> HTMLResponse:
    """Delete the memory and redirect the client back to the list page.

    Re-fetches under read RBAC so we can surface a true 404 when the
    row doesn't exist for this operator -- :meth:`MemoryService.forget`
    is idempotent and would otherwise return ``False`` silently, which
    misleads the UX (the modal said "delete" and the row didn't
    disappear because it was never there).

    Response shape: an empty ``204`` carrying the ``HX-Redirect`` header
    so HTMX issues a full client-side GET to ``/ui/memory``. The detail
    page's confirm-delete button targets ``hx-target="body"`` so a
    fragment-only re-render would destroy the chassis chrome
    (``<html>``/``<head>``/``<body>``); ``HX-Redirect`` is the canonical
    HTMX pattern for post-delete navigation -- see
    https://htmx.org/headers/hx-redirect/. The list GET that follows
    re-derives the empty-or-trimmed state, so no flash plumbing is
    needed: the user lands on the list with the deleted row absent.
    """
    service = MemoryService()
    existing = await service.recall(operator=operator, scope=scope, slug=slug, target_name=None)
    if existing is None:
        raise HTTPException(status_code=404, detail="memory_not_found")
    try:
        await service.forget(
            operator=operator,
            scope=scope,
            slug=slug,
            target_name=existing.target_name,
        )
    except PermissionDeniedError as exc:
        raise HTTPException(status_code=403, detail=f"permission_denied: {exc.reason}") from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    _log.info(
        "ui_memory_delete",
        tenant_id=str(session_ctx.tenant_id),
        operator_sub=session_ctx.operator_sub,
        scope=scope.value,
        slug=slug,
    )
    del request  # not consumed -- HX-Redirect needs no request context.
    return HTMLResponse(
        status_code=204,
        headers={"HX-Redirect": "/ui/memory"},
    )
