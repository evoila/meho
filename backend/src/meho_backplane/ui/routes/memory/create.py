# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Memory UI create-modal render + submit helpers.

Initiative #341 (G10.4 Memory UI), Task #878 (T2). Split out of a
hypothetical combined ``create_promote.py`` module so neither the
create nor the promote concern alone exceeds the chassis-wide
~600-line cap. Shared helpers live in
:mod:`~meho_backplane.ui.routes.memory._modal_shared`; the sibling
promote module lives in
:mod:`~meho_backplane.ui.routes.memory.promote`.

Routes served from this module:

* **``GET /ui/memory/create``** -- HTMX-loaded ``<dialog>`` with an
  RBAC-filtered scope selector, slug input (optional, auto-generated
  when blank), Markdown body textarea + debounced HTMX preview wiring,
  ``expires_at`` picker, comma-separated tags input.
* **``POST /ui/memory/create``** -- submit handler. Calls
  :meth:`MemoryService.remember` and returns 204 with
  ``HX-Redirect: /ui/memory`` so HTMX navigates back to the list with
  the new row visible.
* **``POST /ui/memory/preview``** -- debounced server-side Markdown
  preview. Returns the same ``<article>`` shape :file:`_body_view.html`
  uses so the preview pane and the detail view render identically.

Audit row contract
------------------

The chassis :class:`~meho_backplane.audit.AuditMiddleware` writes one
row per request iff ``operator_sub`` is bound to the structlog
contextvars at the time the audit hook fires. The UI session
middleware does not bind those contextvars (it only sets the
per-request :class:`UISessionContext` on ``request.state``); the
create handler binds them explicitly -- same shape the
``/api/v1/memory`` REST handler uses -- so the chassis audit row
commits with the canonical ``memory.remember`` op id and the
G6.1-T3 publish-on-write broadcast hook fires under the right class.
"""

from __future__ import annotations

from datetime import datetime

import structlog
from fastapi import HTTPException, Request, status
from fastapi.responses import HTMLResponse

from meho_backplane.auth.operator import Operator
from meho_backplane.memory import MemoryScope, MemoryService, PermissionDeniedError
from meho_backplane.memory.schemas import TARGET_SCOPED
from meho_backplane.ui.auth.middleware import UISessionContext
from meho_backplane.ui.csrf import mint_csrf_token
from meho_backplane.ui.routes.memory._modal_shared import (
    TAGS_MAX_LENGTH,
    TARGET_NAME_MAX_LENGTH,
    build_common_template_context,
    parse_tags,
    scope_label,
    set_csrf_cookie,
    writable_scopes_for,
)
from meho_backplane.ui.routes.memory.render import render_markdown
from meho_backplane.ui.routes.memory.views import (
    BODY_MAX_LENGTH,
    SLUG_MAX_LENGTH,
    validate_slug,
)
from meho_backplane.ui.templating import get_templates

__all__ = ["create_entry", "render_body_preview", "render_create_modal"]

_log = structlog.get_logger(__name__)


async def render_create_modal(
    request: Request,
    session_ctx: UISessionContext,
    operator: Operator,
) -> HTMLResponse:
    """Render the HTMX-loaded create modal fragment.

    Scope selector is filtered via :func:`writable_scopes_for` so the
    operator never sees a scope they can't write to (defence-in-depth:
    the service-layer matrix re-checks on submit). The modal carries
    its own CSRF token via the page-level ``hx-headers`` directive on
    the form -- the route also sets the cookie so the chassis
    double-submit pair lines up.
    """
    writable = writable_scopes_for(operator)
    csrf_token = mint_csrf_token(str(session_ctx.session_id))
    context: dict[str, object] = {
        **build_common_template_context(session_ctx, csrf_token),
        "writable_scopes": [
            {"value": scope.value, "label": scope_label(scope)} for scope in writable
        ],
        "target_scoped_values": [scope.value for scope in TARGET_SCOPED],
        "body_max_length": BODY_MAX_LENGTH,
        "slug_max_length": SLUG_MAX_LENGTH,
        "tags_max_length": TAGS_MAX_LENGTH,
        "target_name_max_length": TARGET_NAME_MAX_LENGTH,
    }
    response = get_templates().TemplateResponse(request, "memory/_create_modal.html", context)
    set_csrf_cookie(response, csrf_token)
    return response


async def render_body_preview(
    request: Request,
    *,
    body: str,
) -> HTMLResponse:
    """Render the debounced server-side Markdown preview fragment.

    Returns the same ``<article>`` shape :file:`_body_view.html` uses
    so the preview pane's styling matches the detail page's rendered
    body. Empty body returns an empty preview (no error -- the
    operator's still typing) so the UX stays smooth across the
    initial keystrokes.
    """
    if not body or not body.strip():
        placeholder = (
            '<article id="memory-create-preview" '
            'class="prose prose-sm max-w-none bg-base-100 '
            'border-base-300 border rounded-box p-4 opacity-50">'
            "Preview will render here as you type."
            "</article>"
        )
        return HTMLResponse(placeholder)
    if len(body) > BODY_MAX_LENGTH:
        # Defence-in-depth before passing the body to the markdown
        # renderer -- the textarea ``maxlength`` is the UX gate but
        # a paste-from-clipboard can blow past it.
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"body too large (max {BODY_MAX_LENGTH} chars)",
        )
    rendered = render_markdown(body)
    context = {"body_html": rendered}
    return get_templates().TemplateResponse(request, "memory/_body_preview.html", context)


def _bind_audit_for_remember(
    *,
    session_ctx: UISessionContext,
    scope: MemoryScope,
) -> None:
    """Bind contextvars so the chassis audit + broadcast hooks fire.

    Same shape ``/api/v1/memory`` (REST) uses -- ``operator_sub`` +
    ``tenant_id`` so the chassis :class:`AuditMiddleware` commits a
    row at all; ``audit_op_id`` / ``audit_op_class`` / ``audit_scope``
    so the row carries the canonical op id and the broadcast hook
    classifies it correctly.
    """
    structlog.contextvars.bind_contextvars(
        operator_sub=session_ctx.operator_sub,
        tenant_id=str(session_ctx.tenant_id),
        audit_op_id="memory.remember",
        audit_op_class="write",
        audit_scope=scope.value,
    )


def _validate_create_body(body: str) -> None:
    """Surface the two body-shape guards as 422 errors."""
    if not body or not body.strip():
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="body must not be empty",
        )
    if len(body) > BODY_MAX_LENGTH:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"body too large (max {BODY_MAX_LENGTH} chars)",
        )


def _normalize_slug_or_422(slug: str | None) -> str | None:
    """Validate the operator-supplied slug; empty string -> auto-generate.

    Empty / whitespace slug returns ``None`` so the service layer
    auto-generates one. A non-empty slug is validated via
    :func:`validate_slug` (which raises 404 to mirror recall's info-
    leak avoidance); on a write path 422 is the spec-correct surface
    (malformed input, not "not found"), so the 404 is rewritten to 422
    in this branch.
    """
    if slug is None or not slug.strip():
        return None
    try:
        validate_slug(slug)
    except HTTPException:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="slug contains characters outside the safe set",
        ) from None
    return slug


async def _persist_create(
    *,
    operator: Operator,
    scope: MemoryScope,
    body: str,
    slug: str | None,
    metadata: dict[str, object],
    expires_at: datetime | None,
    target_name: str | None,
) -> str:
    """Call ``MemoryService.remember`` and map service errors to HTTPException.

    Returns the persisted slug. Centralised so the
    :func:`create_entry` handler keeps its control flow flat.
    """
    service = MemoryService()
    try:
        entry = await service.remember(
            operator=operator,
            scope=scope,
            body=body,
            slug=slug,
            metadata=metadata,
            expires_at=expires_at,
            target_name=target_name,
        )
    except PermissionDeniedError as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"permission_denied: {exc.reason}",
        ) from exc
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        ) from exc
    return entry.slug


async def create_entry(
    request: Request,
    session_ctx: UISessionContext,
    operator: Operator,
    *,
    scope: MemoryScope,
    body: str,
    slug: str | None,
    tags_raw: str | None,
    expires_at: datetime | None,
    target_name: str | None,
) -> HTMLResponse:
    """Persist a new memory and return an HX-Redirect to the list.

    Submit handler for ``POST /ui/memory/create``. Builds the
    ``metadata.tags`` list from the comma-separated form input,
    delegates the persist to :meth:`MemoryService.remember`, and
    returns a 204 with ``HX-Redirect: /ui/memory`` so HTMX navigates
    back to the list (the new row will be the first card because
    ``list_memories`` orders by ``updated_at desc``).

    Same redirect shape T1's :func:`delete_entry` returns -- the
    detail-page modal flow uses ``hx-target="body"`` so a fragment-
    only response would destroy the chassis chrome. ``HX-Redirect``
    is the canonical HTMX pattern for post-mutation navigation
    (https://htmx.org/headers/hx-redirect/).
    """
    _validate_create_body(body)
    resolved_slug = _normalize_slug_or_422(slug)

    tags = parse_tags(tags_raw)
    metadata: dict[str, object] = {"tags": tags} if tags else {}

    _bind_audit_for_remember(session_ctx=session_ctx, scope=scope)

    persisted_slug = await _persist_create(
        operator=operator,
        scope=scope,
        body=body,
        slug=resolved_slug,
        metadata=metadata,
        expires_at=expires_at,
        target_name=target_name,
    )

    structlog.contextvars.bind_contextvars(audit_slug=persisted_slug)
    _log.info(
        "ui_memory_create",
        tenant_id=str(session_ctx.tenant_id),
        operator_sub=session_ctx.operator_sub,
        scope=scope.value,
        slug=persisted_slug,
    )
    del request  # not consumed -- HX-Redirect needs no request context.
    return HTMLResponse(
        status_code=status.HTTP_204_NO_CONTENT,
        headers={"HX-Redirect": "/ui/memory"},
    )
