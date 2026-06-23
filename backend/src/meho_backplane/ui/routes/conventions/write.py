# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Conventions UI write surface -- author / edit / delete modals + history.

Initiative #1838 (G10.12 Conventions console), Task #1896 (T2). Layers
the write surface on top of T1's (#1895) read pages:

* ``GET /ui/conventions/create`` -- HTMX ``<dialog>`` author modal with a
  debounced token-cost preview wiring.
* ``POST /ui/conventions/create`` -- submit; on success 204 +
  ``HX-Redirect: /ui/conventions``; on 409 / 422 render the error inline.
* ``GET /ui/conventions/{slug}/edit`` -- HTMX edit modal (title / priority
  / body editable; kind + slug read-only -- PATCH cannot change them).
* ``PATCH /ui/conventions/{slug}`` -- submit; calls
  :meth:`ConventionsService.update_convention`.
* ``POST /ui/conventions/preview`` -- debounced server-side token-cost
  preview: takes ``body`` + ``kind``, renders the estimated tokens vs the
  600 budget (red when an ``operational`` body is over) plus the
  sanitised Markdown render.
* ``GET /ui/conventions/{slug}/delete`` -- the confirm ``<dialog>`` gate.
* ``DELETE /ui/conventions/{slug}`` -- delete behind the confirm gate; a
  second DELETE on the already-gone slug renders a benign "already
  deleted" fragment rather than surfacing the service's non-idempotent
  404 (the service 404s on a missing row).
* ``GET /ui/conventions/{slug}/history`` -- the history diff panel,
  newest-first, with a per-row ``body_before`` -> ``body_after`` diff.

Service reuse
-------------

Every write goes through the shared
:class:`~meho_backplane.conventions.service.ConventionsService` so the
budget gate, the ``tenant_convention_history`` row, and the
pre-allocated-audit-id soft-FK pairing
(:func:`~meho_backplane.audit.bind_preallocated_audit_id`, called inside
the service) are exercised identically to the REST surface. This module
never re-derives the budget arithmetic -- the token-cost preview calls
:func:`~meho_backplane.conventions.schemas.estimate_tokens` directly (the
single source of truth) rather than re-deriving the chars-per-token
ratio.

Audit binding
-------------

The chassis :class:`~meho_backplane.audit.AuditMiddleware` writes one row
per request iff ``operator_sub`` is bound to the structlog contextvars
when the hook fires. The BFF session middleware does not bind them, so
each write handler binds the same ``audit_op_id`` / ``audit_op_class`` /
``audit_slug`` triple the REST handler uses before the service call --
and because the service mints + binds the pre-allocated audit id in the
same transaction as the history row, the history row's ``audit_id``
joins the audit row rather than landing NULL.

RBAC + CSRF
-----------

Write = TENANT_ADMIN. Every write handler resolves the operator through
:func:`~meho_backplane.ui.routes.connectors.operator.resolve_operator_or_403`
(403 on a non-admin) -- the connectors surface is the authoritative
precedent for these BFF write helpers. The CSRF double-submit pair is
enforced by the chassis :class:`~meho_backplane.ui.csrf.CSRFMiddleware`
on every ``/ui/*`` mutating verb; each modal render mints a fresh token
and sets the matching ``meho_csrf`` cookie on the same response so the
form's ``hx-headers`` echo lines up (#1693).
"""

from __future__ import annotations

import structlog
from fastapi import HTTPException, Request, status
from fastapi.responses import HTMLResponse
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.auth.operator import Operator
from meho_backplane.conventions.schemas import (
    DEFAULT_MAX_PREAMBLE_TOKENS,
    ConventionCreate,
    ConventionKind,
    ConventionUpdate,
    PreambleInclusion,
    estimate_tokens,
)
from meho_backplane.conventions.service import (
    ConventionConflictError,
    ConventionNotFoundError,
    ConventionsService,
    OverBudgetError,
)
from meho_backplane.ui.csrf import CSRF_COOKIE_NAME, mint_csrf_token
from meho_backplane.ui.routes.conventions.operator import ConventionsWriteContext
from meho_backplane.ui.routes.memory.render import render_markdown
from meho_backplane.ui.templating import get_templates

__all__ = [
    "BODY_MAX_LENGTH",
    "PRIORITY_MAX",
    "PRIORITY_MIN",
    "SLUG_MAX_LENGTH",
    "TITLE_MAX_LENGTH",
    "delete_convention",
    "render_create_modal",
    "render_delete_confirm",
    "render_edit_modal",
    "render_token_preview",
    "submit_create",
    "submit_update",
]

_log = structlog.get_logger(__name__)

#: One shared service instance -- stateless (every method takes the
#: session), mirroring the read views' module-level singleton.
_service = ConventionsService()

#: Field bounds mirrored from :class:`ConventionCreate` / the substrate
#: so the modal's ``maxlength`` / ``min`` / ``max`` HTML attributes match
#: the validation the service enforces (a paste-past-the-cap surfaces as
#: the service's 422 rather than a silent truncation).
SLUG_MAX_LENGTH = 128
TITLE_MAX_LENGTH = 200
BODY_MAX_LENGTH = 64_000
PRIORITY_MIN = -32768
PRIORITY_MAX = 32767

#: Audit op-id triple mirrored from the REST surface (``conventions.*``)
#: so the BFF writes land in the same G8 audit / dashboard classes.
_OP_ID_CREATE = "conventions.create"
_OP_ID_UPDATE = "conventions.update"
_OP_ID_DELETE = "conventions.delete"


def _set_csrf_cookie(response: HTMLResponse, csrf_token: str) -> None:
    """Set the ``meho_csrf`` double-submit cookie on a modal response.

    Mirrors the memory modal's cookie posture: ``Secure`` +
    ``SameSite=Strict`` + ``path=/ui`` so the cookie the modal mints
    rides the same response as the form's header echo and the chassis
    middleware's double-submit check lines up on the next write POST.
    """
    response.set_cookie(
        key=CSRF_COOKIE_NAME,
        value=csrf_token,
        httponly=False,
        secure=True,
        samesite="strict",
        path="/ui",
    )


def _kind_options() -> list[dict[str, str]]:
    """Render the kind selector's (value, label) pairs for the author modal."""
    return [
        {"value": ConventionKind.OPERATIONAL.value, "label": "Operational"},
        {"value": ConventionKind.WORKFLOW.value, "label": "Workflow"},
        {"value": ConventionKind.REFERENCE.value, "label": "Reference"},
    ]


def _bind_write_audit(*, operator: Operator, op_id: str, slug: str) -> None:
    """Bind the contextvars the chassis audit hook reads for a write.

    Same shape the REST conventions handlers use -- ``operator_sub`` +
    ``tenant_id`` so the chassis :class:`AuditMiddleware` commits a row
    at all, plus the canonical ``audit_op_id`` / ``audit_op_class`` /
    ``audit_slug`` so the row carries the right op id. The service then
    mints + binds the pre-allocated audit id in the same transaction as
    the history row, so the history ``audit_id`` joins this audit row.
    """
    structlog.contextvars.bind_contextvars(
        operator_sub=operator.sub,
        tenant_id=str(operator.tenant_id),
        audit_op_id=op_id,
        audit_op_class="write",
        audit_slug=slug,
    )


async def render_create_modal(
    request: Request,
    write_ctx: ConventionsWriteContext,
) -> HTMLResponse:
    """Render the HTMX author ``<dialog>`` fragment.

    Mints a fresh CSRF token, embeds it in the form's ``hx-headers``
    echo, and sets the matching ``meho_csrf`` cookie on this response.
    The body textarea wires a debounced ``hx-post`` to
    ``/ui/conventions/preview`` (``keyup changed delay:300ms``) so the
    token-cost preview updates live as the operator types.
    """
    csrf_token = mint_csrf_token(str(write_ctx.session_id))
    context: dict[str, object] = {
        "csrf_token": csrf_token,
        "kind_options": _kind_options(),
        "slug_max_length": SLUG_MAX_LENGTH,
        "title_max_length": TITLE_MAX_LENGTH,
        "body_max_length": BODY_MAX_LENGTH,
        "priority_min": PRIORITY_MIN,
        "priority_max": PRIORITY_MAX,
        "max_tokens": DEFAULT_MAX_PREAMBLE_TOKENS,
    }
    response = get_templates().TemplateResponse(request, "conventions/_create_modal.html", context)
    _set_csrf_cookie(response, csrf_token)
    return response


async def render_edit_modal(
    request: Request,
    write_ctx: ConventionsWriteContext,
    *,
    session: AsyncSession,
    slug: str,
) -> HTMLResponse:
    """Render the HTMX edit ``<dialog>`` fragment, pre-filled from the row.

    ``kind`` and ``slug`` are shown read-only -- the PATCH surface cannot
    change them (operators delete + recreate to switch kind). Only
    ``title`` / ``priority`` / ``body`` are editable + submitted.
    """
    try:
        convention = await _service.get_convention(
            session=session,
            tenant_id=write_ctx.operator.tenant_id,
            slug=slug,
        )
    except ConventionNotFoundError as exc:
        raise HTTPException(status_code=404, detail="convention_not_found") from exc
    csrf_token = mint_csrf_token(str(write_ctx.session_id))
    context: dict[str, object] = {
        "csrf_token": csrf_token,
        "convention": {
            "slug": convention.slug,
            "title": convention.title,
            "kind": convention.kind,
            "priority": convention.priority,
            "body": convention.body,
        },
        "title_max_length": TITLE_MAX_LENGTH,
        "body_max_length": BODY_MAX_LENGTH,
        "priority_min": PRIORITY_MIN,
        "priority_max": PRIORITY_MAX,
        "max_tokens": DEFAULT_MAX_PREAMBLE_TOKENS,
    }
    response = get_templates().TemplateResponse(request, "conventions/_edit_modal.html", context)
    _set_csrf_cookie(response, csrf_token)
    return response


async def render_token_preview(
    request: Request,
    *,
    body: str,
    kind: str,
) -> HTMLResponse:
    """Render the debounced token-cost preview fragment.

    Computes the estimated token cost via
    :func:`~meho_backplane.conventions.schemas.estimate_tokens` (the
    single source of truth -- no re-derived chars/token ratio). The
    fragment marks an ``operational`` body that exceeds
    :data:`DEFAULT_MAX_PREAMBLE_TOKENS` in red; the same body as
    ``workflow`` / ``reference`` is not flagged (those kinds never enter
    the preamble, so they are exempt from the budget gate). A sanitised
    Markdown render of the body is shown alongside.
    """
    estimated = estimate_tokens(body)
    # Resolve the kind defensively: an unknown token (a tampered form,
    # or the selector mid-change) falls back to REFERENCE -- the safe
    # direction (exempt from the budget gate), so a transient bad value
    # never flashes a spurious red over-budget warning.
    try:
        kind_enum = ConventionKind(kind)
    except ValueError:
        kind_enum = ConventionKind.REFERENCE
    counts_against_budget = kind_enum is ConventionKind.OPERATIONAL
    over_budget = counts_against_budget and estimated > DEFAULT_MAX_PREAMBLE_TOKENS
    context: dict[str, object] = {
        "estimated_tokens": estimated,
        "max_tokens": DEFAULT_MAX_PREAMBLE_TOKENS,
        "counts_against_budget": counts_against_budget,
        "over_budget": over_budget,
        "kind": kind_enum.value,
        "body_html": render_markdown(body) if body.strip() else None,
    }
    return get_templates().TemplateResponse(request, "conventions/_token_preview.html", context)


def _preamble_status_context(status_: PreambleInclusion | None) -> dict[str, object] | None:
    """Project a :class:`PreambleInclusion` into the result-fragment shape."""
    if status_ is None:
        return None
    return {
        "included": status_.included,
        "position": status_.position,
        "token_count": status_.token_count,
        "would_drop_slugs": list(status_.would_drop_slugs),
    }


def _render_create_error(
    request: Request,
    *,
    message: str,
) -> HTMLResponse:
    """Render the inline create-error fragment (409 / 422) -- no redirect.

    The author modal's form posts with ``hx-target`` pointed at an inline
    result slot, so a 409 (duplicate slug) or 422 (over budget) renders
    the actionable message in place rather than navigating away and
    losing the operator's input.
    """
    context = {"message": message}
    return get_templates().TemplateResponse(
        request,
        "conventions/_write_error.html",
        context,
        status_code=status.HTTP_200_OK,
    )


async def submit_create(
    request: Request,
    write_ctx: ConventionsWriteContext,
    *,
    session: AsyncSession,
    slug: str,
    title: str,
    body: str,
    kind: str,
    priority: int,
) -> HTMLResponse:
    """Persist a new convention via the service; HX-Redirect on success.

    On success returns 204 + ``HX-Redirect: /ui/conventions`` so HTMX
    navigates back to the list with the new row visible -- unless the new
    ``operational`` rule was dropped from the preamble on budget
    overflow, in which case the result fragment surfaces the red
    "DROPPED" ``preamble_status`` indicator instead so the operator sees
    the rule will not reach agents before leaving the modal.

    On a duplicate slug (:class:`ConventionConflictError` -> 409) or an
    over-budget single body (:class:`OverBudgetError` -> 422), renders
    the error inline in the modal (no redirect).
    """
    try:
        kind_enum = ConventionKind(kind)
    except ValueError:
        return _render_create_error(
            request,
            message=(f"kind must be one of {[k.value for k in ConventionKind]}; got {kind!r}"),
        )

    try:
        create_body = ConventionCreate(
            slug=slug,
            title=title,
            body=body,
            kind=kind_enum,
            priority=priority,
        )
    except ValueError as exc:
        return _render_create_error(request, message=str(exc))

    operator = write_ctx.operator
    _bind_write_audit(operator=operator, op_id=_OP_ID_CREATE, slug=create_body.slug)
    try:
        convention = await _service.create_convention(
            session=session,
            operator=operator,
            body=create_body,
        )
        await session.commit()
    except ConventionConflictError:
        await session.rollback()
        return _render_create_error(
            request,
            message=f"A convention with slug {slug!r} already exists.",
        )
    except OverBudgetError as exc:
        await session.rollback()
        return _render_create_error(
            request,
            message=(
                f"Body exceeds the preamble budget "
                f"(estimated {exc.estimated} tokens, budget {exc.budget}). "
                f"Trim the body or set kind to workflow / reference."
            ),
        )

    _log.info(
        "ui_conventions_create",
        tenant_id=str(operator.tenant_id),
        operator_sub=operator.sub,
        slug=create_body.slug,
        kind=kind_enum.value,
    )
    return _post_write_response(
        request,
        preamble_status=convention.preamble_status,
        slug=create_body.slug,
    )


async def submit_update(
    request: Request,
    write_ctx: ConventionsWriteContext,
    *,
    session: AsyncSession,
    slug: str,
    title: str,
    body: str,
    priority: int,
) -> HTMLResponse:
    """Apply a PATCH via the service; HX-Redirect on success.

    The edit modal submits ``title`` / ``priority`` / ``body`` only --
    ``kind`` + ``slug`` are read-only because the PATCH surface cannot
    change them. On an over-budget body change renders the error inline;
    otherwise HX-Redirects (or surfaces the ``preamble_status`` drop, as
    create does).
    """
    try:
        update_body = ConventionUpdate(title=title, body=body, priority=priority)
    except ValueError as exc:
        return _render_create_error(request, message=str(exc))

    operator = write_ctx.operator
    _bind_write_audit(operator=operator, op_id=_OP_ID_UPDATE, slug=slug)
    try:
        convention = await _service.update_convention(
            session=session,
            operator=operator,
            slug=slug,
            body=update_body,
        )
        await session.commit()
    except ConventionNotFoundError as exc:
        await session.rollback()
        raise HTTPException(status_code=404, detail="convention_not_found") from exc
    except OverBudgetError as exc:
        await session.rollback()
        return _render_create_error(
            request,
            message=(
                f"Body exceeds the preamble budget "
                f"(estimated {exc.estimated} tokens, budget {exc.budget})."
            ),
        )

    _log.info(
        "ui_conventions_update",
        tenant_id=str(operator.tenant_id),
        operator_sub=operator.sub,
        slug=slug,
    )
    return _post_write_response(
        request,
        preamble_status=convention.preamble_status,
        slug=slug,
    )


def _post_write_response(
    request: Request,
    *,
    preamble_status: PreambleInclusion | None,
    slug: str,
) -> HTMLResponse:
    """Build the create/update response: HX-Redirect or a DROPPED warning.

    When the just-written ``operational`` rule landed in the preamble (or
    the kind never enters the preamble), HX-Redirect back to the list.
    When the rule was dropped on budget overflow
    (``preamble_status.included is False``), the operator must see that
    before navigating away -- so render the red "DROPPED" fragment naming
    ``would_drop_slugs`` instead of redirecting silently.
    """
    if preamble_status is not None and not preamble_status.included:
        context = {
            "preamble_status": _preamble_status_context(preamble_status),
            "slug": slug,
        }
        return get_templates().TemplateResponse(
            request, "conventions/_preamble_dropped.html", context
        )
    return HTMLResponse(
        status_code=status.HTTP_204_NO_CONTENT,
        headers={"HX-Redirect": "/ui/conventions"},
    )


async def render_delete_confirm(
    request: Request,
    write_ctx: ConventionsWriteContext,
    *,
    session: AsyncSession,
    slug: str,
) -> HTMLResponse:
    """Render the delete-confirm ``<dialog>`` gate.

    The actual DELETE fires only from the button inside this confirm
    dialog -- a plain row/detail click never deletes directly. Loads the
    convention so the confirm copy can name the title; a missing slug
    404s (the operator's view is stale).
    """
    try:
        convention = await _service.get_convention(
            session=session,
            tenant_id=write_ctx.operator.tenant_id,
            slug=slug,
        )
    except ConventionNotFoundError as exc:
        raise HTTPException(status_code=404, detail="convention_not_found") from exc
    csrf_token = mint_csrf_token(str(write_ctx.session_id))
    context: dict[str, object] = {
        "csrf_token": csrf_token,
        "slug": convention.slug,
        "title": convention.title,
        "kind": convention.kind,
    }
    response = get_templates().TemplateResponse(
        request, "conventions/_delete_confirm.html", context
    )
    _set_csrf_cookie(response, csrf_token)
    return response


async def delete_convention(
    request: Request,
    operator: Operator,
    *,
    session: AsyncSession,
    slug: str,
) -> HTMLResponse:
    """Delete a convention via the service; HX-Redirect on success.

    The service DELETE is **non-idempotent**: it 404s on a missing row
    (and writes a history row before deleting). A naive "delete then
    re-render the list" double-fires that 404 when the confirm button is
    clicked twice (or re-submitted from a stale modal). This handler
    catches :class:`ConventionNotFoundError` and renders a benign
    "already deleted" fragment rather than surfacing a raw 404, so the
    re-fire is safe.
    """
    _bind_write_audit(operator=operator, op_id=_OP_ID_DELETE, slug=slug)
    try:
        await _service.delete_convention(
            session=session,
            operator=operator,
            slug=slug,
        )
        await session.commit()
    except ConventionNotFoundError:
        await session.rollback()
        # Non-idempotent re-fire: the row is already gone. Render the
        # benign already-deleted message instead of a 404 so a
        # double-click on the confirm button does not surface an error.
        return get_templates().TemplateResponse(
            request,
            "conventions/_delete_done.html",
            {"slug": slug, "already_deleted": True},
        )

    _log.info(
        "ui_conventions_delete",
        tenant_id=str(operator.tenant_id),
        operator_sub=operator.sub,
        slug=slug,
    )
    return HTMLResponse(
        status_code=status.HTTP_204_NO_CONTENT,
        headers={"HX-Redirect": "/ui/conventions"},
    )
