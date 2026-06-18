# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Agent create / edit / enable-disable / delete (DaisyUI modal, HTMX).

Initiative #1824 (G10.8 Agents console), Task #1825 (T1). The read
surface (list + detail) lives in
:mod:`~meho_backplane.ui.routes.agents.views`; this module layers the
write surface on top:

* **``GET /ui/agents/create``** -- HTMX-loaded create modal. The
  ``model_tier`` ``<select>`` is server-rendered from the
  :class:`~meho_backplane.agents.schemas.AgentModelTier` enum.
* **``POST /ui/agents/create``** -- submit handler. Builds an
  :class:`~meho_backplane.agents.schemas.AgentDefinitionCreate` from the
  form fields and persists via
  :meth:`~meho_backplane.agents.service.AgentDefinitionService.create`.
  Success -> 204 + ``HX-Redirect: /ui/agents``. A Pydantic
  :class:`~pydantic.ValidationError`, a duplicate-name
  :class:`~meho_backplane.agents.service.AgentDefinitionExistsError`
  (409), or an
  :class:`~meho_backplane.agents.service.AgentIdentityRefInvalidError`
  (422) all re-render the modal with per-field messages and the
  matching HTTP status.
* **``GET /ui/agents/{name}/edit``** -- HTMX-loaded edit modal, fields
  pre-populated server-side. ``name`` is read-only (not updatable per
  :class:`~meho_backplane.agents.schemas.AgentDefinitionUpdate`).
* **``PATCH /ui/agents/{name}``** -- submit handler. Builds an
  :class:`~meho_backplane.agents.schemas.AgentDefinitionUpdate` and
  delegates to
  :meth:`~meho_backplane.agents.service.AgentDefinitionService.update`.
* **``POST /ui/agents/{name}/toggle``** -- enable / disable toggle.
  PATCHes the ``enabled`` flag to the posted value. Success -> 204 +
  ``HX-Redirect`` so the list / detail re-renders.
* **``GET /ui/agents/{name}/delete``** -- HTMX-loaded delete-confirm
  modal.
* **``POST /ui/agents/{name}/delete``** -- delete submit. Success ->
  204 + ``HX-Redirect: /ui/agents``.

The ``identity_ref`` field is free-text here (the picker over
registered non-revoked principals is T4 #1832; until it lands a
free-text field with inline 422 surfacing is the accepted shape per
the #1825 issue body). A typo'd / revoked / cross-tenant ``identity_ref``
surfaces as the inline ``identity_ref`` field error, not a generic 500.

RBAC posture
------------

Create / edit / toggle / delete are **tenant_admin only** and the gate
is server-side: every write route depends on
:func:`~meho_backplane.ui.routes.agents.operator.resolve_operator_or_403`,
which lifts the full :class:`~meho_backplane.auth.operator.Operator`
from the BFF session, re-validates the access token through the chassis
JWT chain, and raises 403 for a non-admin caller. The list / detail
templates additionally hide the affordances from non-admins (UX) -- a
crafted POST / PATCH still hits the 403.

The :class:`~meho_backplane.agents.service.AgentDefinitionService` does
not itself enforce roles (it assumes the caller validated the tenant
role), so the 403 gate lives on this module's route deps -- the same
service-RBAC split the REST routes apply.

CSRF posture
------------

``POST`` / ``PATCH`` under ``/ui/`` are gated by the chassis
:class:`~meho_backplane.ui.csrf.CSRFMiddleware` (signed double-submit)
before the handler runs. Each modal render re-mints + re-sets the
``meho_csrf`` cookie and the form declares its own ``hx-headers`` echo
so the double-submit pair lines up (#1693).
"""

from __future__ import annotations

import structlog
from fastapi import Request, status
from fastapi.responses import HTMLResponse
from pydantic import ValidationError

from meho_backplane.agents.schemas import (
    AgentDefinitionCreate,
    AgentDefinitionUpdate,
    AgentModelTier,
)
from meho_backplane.agents.service import (
    AgentDefinitionExistsError,
    AgentDefinitionService,
    AgentIdentityRefInvalidError,
)
from meho_backplane.auth.operator import Operator
from meho_backplane.ui.auth.middleware import UISessionContext
from meho_backplane.ui.csrf import CSRF_COOKIE_NAME, mint_csrf_token
from meho_backplane.ui.routes.agents.views import _fetch_agent_or_404
from meho_backplane.ui.templating import get_templates

__all__ = [
    "render_create_modal",
    "render_delete_modal",
    "render_edit_modal",
    "submit_create",
    "submit_delete",
    "submit_edit",
    "submit_toggle",
]

_log = structlog.get_logger(__name__)

#: Form-field length caps mirroring the
#: :class:`~meho_backplane.agents.schemas.AgentDefinitionCreate` bounds.
#: The server-side Pydantic validation is authoritative; these caps
#: bound the form-body parse before the bytes reach the schema.
NAME_MAX: int = 128
IDENTITY_REF_MAX: int = 256
SYSTEM_PROMPT_MAX: int = 64 * 1024
TURN_BUDGET_MAX_LEN: int = 8


def _set_csrf_cookie(response: HTMLResponse, csrf_token: str) -> None:
    """Mirror the chassis CSRF cookie posture for the modal renders."""
    response.set_cookie(
        key=CSRF_COOKIE_NAME,
        value=csrf_token,
        httponly=False,
        secure=True,
        samesite="strict",
        path="/ui",
    )


def _validation_errors_by_field(exc: ValidationError) -> dict[str, str]:
    """Project a Pydantic :class:`ValidationError` into a field->message map.

    Keys are the first ``loc`` element (the field name); the value is
    the human-readable ``msg``. A field with multiple errors keeps the
    first -- one message per field, the DaisyUI ``input-error`` + label
    convention. Same shape as the connectors forms helper.
    """
    errors: dict[str, str] = {}
    for err in exc.errors():
        loc = err.get("loc") or ()
        field = str(loc[0]) if loc else "__root__"
        errors.setdefault(field, str(err.get("msg", "invalid value")))
    return errors


def _build_form_context(csrf_token: str, *, mode: str) -> dict[str, object]:
    """Build the template context shared by the create + edit modals."""
    return {
        "page_title": "Agents",
        "active_surface": "agents",
        "csrf_token": csrf_token,
        "mode": mode,
        "model_tiers": [tier.value for tier in AgentModelTier],
        # Bare-form render carries no errors / submitted values.
        "errors": {},
        "values": {},
    }


def _redirect_to_list() -> HTMLResponse:
    """Return a 204 + ``HX-Redirect`` so HTMX reloads the agents list."""
    return HTMLResponse(
        status_code=status.HTTP_204_NO_CONTENT,
        headers={"HX-Redirect": "/ui/agents"},
    )


async def render_create_modal(
    request: Request,
    session_ctx: UISessionContext,
) -> HTMLResponse:
    """Render the HTMX-loaded create modal fragment (tenant_admin-gated route)."""
    csrf_token = mint_csrf_token(str(session_ctx.session_id))
    response = get_templates().TemplateResponse(
        request,
        "agents/_create_modal.html",
        _build_form_context(csrf_token, mode="create"),
    )
    _set_csrf_cookie(response, csrf_token)
    return response


async def render_edit_modal(
    request: Request,
    session_ctx: UISessionContext,
    *,
    name: str,
) -> HTMLResponse:
    """Render the edit modal pre-populated from the resolved agent.

    404 on an absent / cross-tenant name (the service returns ``None``
    for both). ``name`` renders read-only -- it is the per-tenant
    natural key and is not updatable; renaming is delete + recreate.
    """
    agent = await _fetch_agent_or_404(session_ctx, name)
    csrf_token = mint_csrf_token(str(session_ctx.session_id))
    context = _build_form_context(csrf_token, mode="edit")
    context["values"] = {
        "name": agent.name,
        "identity_ref": agent.identity_ref,
        "model_tier": agent.model_tier,
        "system_prompt": agent.system_prompt,
        "turn_budget": str(agent.turn_budget),
        "enabled": agent.enabled,
    }
    response = get_templates().TemplateResponse(request, "agents/_edit_modal.html", context)
    _set_csrf_cookie(response, csrf_token)
    return response


async def render_delete_modal(
    request: Request,
    session_ctx: UISessionContext,
    *,
    name: str,
) -> HTMLResponse:
    """Render the delete-confirm modal (404 on absent / cross-tenant name)."""
    agent = await _fetch_agent_or_404(session_ctx, name)
    csrf_token = mint_csrf_token(str(session_ctx.session_id))
    context: dict[str, object] = {
        "page_title": "Agents",
        "active_surface": "agents",
        "csrf_token": csrf_token,
        "agent": {"name": agent.name},
    }
    response = get_templates().TemplateResponse(request, "agents/_delete_modal.html", context)
    _set_csrf_cookie(response, csrf_token)
    return response


def _coerce_turn_budget(raw: str | None) -> int | str:
    """Coerce the free-text ``turn_budget`` field to ``int``.

    A non-numeric value returns the raw string so the
    :class:`AgentDefinitionCreate` / :class:`AgentDefinitionUpdate`
    field validator surfaces a typed 422 (rather than this helper
    raising a bare ``ValueError`` that would 500). The 1..1000 range
    check stays in the schema so the bound lives in one place.
    """
    if raw is None or not raw.strip():
        return ""
    try:
        return int(raw.strip())
    except ValueError:
        return raw.strip()


async def submit_create(
    request: Request,
    session_ctx: UISessionContext,
    operator: Operator,
    *,
    name: str,
    identity_ref: str,
    model_tier: str,
    system_prompt: str,
    turn_budget: str | None,
    enabled: bool,
) -> HTMLResponse:
    """Build an :class:`AgentDefinitionCreate` and persist it via the service.

    A Pydantic :class:`ValidationError` re-renders the modal with
    per-field messages + 422; a duplicate ``(tenant, name)`` re-renders
    with a 409 ``name`` field error; an unknown / revoked
    ``identity_ref`` re-renders with a 422 ``identity_ref`` field error.
    A clean create returns 204 + ``HX-Redirect: /ui/agents``.
    """
    raw_values = {
        "name": name,
        "identity_ref": identity_ref,
        "model_tier": model_tier,
        "system_prompt": system_prompt,
        "turn_budget": turn_budget or "",
        "enabled": enabled,
    }
    try:
        body = AgentDefinitionCreate(
            name=name,
            identity_ref=identity_ref,
            model_tier=AgentModelTier(model_tier),
            system_prompt=system_prompt,
            turn_budget=_coerce_turn_budget(turn_budget),  # type: ignore[arg-type]
            enabled=enabled,
        )
    except (ValidationError, ValueError) as exc:
        return _render_form_with_errors(
            request,
            csrf_session_id=str(session_ctx.session_id),
            mode="create",
            exc=exc,
            values=raw_values,
        )

    service = AgentDefinitionService()
    try:
        await service.create(
            tenant_id=session_ctx.tenant_id,
            created_by_sub=operator.sub,
            payload=body,
        )
    except AgentDefinitionExistsError:
        return _render_form_with_errors(
            request,
            csrf_session_id=str(session_ctx.session_id),
            mode="create",
            field_errors={"name": "an agent with this name already exists"},
            values=raw_values,
            status_code=status.HTTP_409_CONFLICT,
        )
    except AgentIdentityRefInvalidError:
        return _render_form_with_errors(
            request,
            csrf_session_id=str(session_ctx.session_id),
            mode="create",
            field_errors={
                "identity_ref": (
                    "identity_ref does not resolve to a registered, "
                    "non-revoked agent principal in this tenant"
                )
            },
            values=raw_values,
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        )

    _log.info(
        "ui_agent_create",
        tenant_id=str(session_ctx.tenant_id),
        operator_sub=session_ctx.operator_sub,
        name=body.name,
    )
    del request  # HX-Redirect needs no request context.
    return _redirect_to_list()


async def submit_edit(
    request: Request,
    session_ctx: UISessionContext,
    operator: Operator,
    *,
    name: str,
    identity_ref: str,
    model_tier: str,
    system_prompt: str,
    turn_budget: str | None,
    enabled: bool,
) -> HTMLResponse:
    """Build an :class:`AgentDefinitionUpdate` and persist it via the service.

    The edit form posts every editable field every time (it's
    pre-populated), so the PATCH body carries the full editable surface
    rather than a sparse diff. ``name`` is not patchable -- it only
    addresses the row. A 404 from an absent / cross-tenant name renders
    the 404 page; validation / identity_ref failures re-render the modal
    inline.
    """
    del operator  # gate only; the service write is tenant-scoped by session.
    raw_values = {
        "name": name,
        "identity_ref": identity_ref,
        "model_tier": model_tier,
        "system_prompt": system_prompt,
        "turn_budget": turn_budget or "",
        "enabled": enabled,
    }
    try:
        body = AgentDefinitionUpdate(
            identity_ref=identity_ref,
            model_tier=AgentModelTier(model_tier),
            system_prompt=system_prompt,
            turn_budget=_coerce_turn_budget(turn_budget),  # type: ignore[arg-type]
            enabled=enabled,
        )
    except (ValidationError, ValueError) as exc:
        return _render_form_with_errors(
            request,
            csrf_session_id=str(session_ctx.session_id),
            mode="edit",
            exc=exc,
            values=raw_values,
        )

    service = AgentDefinitionService()
    try:
        entry = await service.update(session_ctx.tenant_id, name, body)
    except AgentIdentityRefInvalidError:
        return _render_form_with_errors(
            request,
            csrf_session_id=str(session_ctx.session_id),
            mode="edit",
            field_errors={
                "identity_ref": (
                    "identity_ref does not resolve to a registered, "
                    "non-revoked agent principal in this tenant"
                )
            },
            values=raw_values,
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        )
    if entry is None:
        from fastapi import HTTPException

        raise HTTPException(status_code=404, detail="agent_not_found")

    _log.info(
        "ui_agent_edit",
        tenant_id=str(session_ctx.tenant_id),
        operator_sub=session_ctx.operator_sub,
        name=name,
    )
    del request
    return _redirect_to_list()


async def submit_toggle(
    session_ctx: UISessionContext,
    operator: Operator,
    *,
    name: str,
    enabled: bool,
) -> HTMLResponse:
    """Enable / disable an agent by PATCHing only its ``enabled`` flag.

    404 on an absent / cross-tenant name. Success returns 204 +
    ``HX-Redirect: /ui/agents`` so the list / detail re-renders with the
    flipped pill. The toggle is a single-field PATCH so it never touches
    ``identity_ref`` (and so skips the identity-ref re-validation the
    service applies only when that field is set).
    """
    del operator  # gate only.
    service = AgentDefinitionService()
    entry = await service.update(
        session_ctx.tenant_id,
        name,
        AgentDefinitionUpdate(enabled=enabled),
    )
    if entry is None:
        from fastapi import HTTPException

        raise HTTPException(status_code=404, detail="agent_not_found")
    _log.info(
        "ui_agent_toggle",
        tenant_id=str(session_ctx.tenant_id),
        operator_sub=session_ctx.operator_sub,
        name=name,
        enabled=enabled,
    )
    return _redirect_to_list()


async def submit_delete(
    session_ctx: UISessionContext,
    operator: Operator,
    *,
    name: str,
) -> HTMLResponse:
    """Delete one agent definition by name.

    404 when no row matched (absent or cross-tenant) so the UX matches
    the REST contract; a clean delete returns 204 +
    ``HX-Redirect: /ui/agents``.
    """
    del operator  # gate only.
    service = AgentDefinitionService()
    deleted = await service.delete(session_ctx.tenant_id, name)
    if not deleted:
        from fastapi import HTTPException

        raise HTTPException(status_code=404, detail="agent_not_found")
    _log.info(
        "ui_agent_delete",
        tenant_id=str(session_ctx.tenant_id),
        operator_sub=session_ctx.operator_sub,
        name=name,
    )
    return _redirect_to_list()


def _render_form_with_errors(
    request: Request,
    *,
    csrf_session_id: str,
    mode: str,
    values: dict[str, object],
    exc: ValidationError | ValueError | None = None,
    field_errors: dict[str, str] | None = None,
    status_code: int = status.HTTP_422_UNPROCESSABLE_CONTENT,
) -> HTMLResponse:
    """Re-render the modal fragment carrying per-field errors + a status.

    The HTMX form targets the modal container with
    ``hx-swap="innerHTML"`` so the error body replaces the dialog in
    place; the operator keeps their typed values (echoed via ``values``)
    and sees the field-level messages. ``exc`` covers the Pydantic /
    coercion path; ``field_errors`` covers the service-raised
    duplicate-name (409) and identity_ref (422) cases.
    """
    if field_errors is not None:
        errors = field_errors
    elif isinstance(exc, ValidationError):
        errors = _validation_errors_by_field(exc)
    else:
        # A bare ValueError from the model_tier enum coercion.
        errors = {"model_tier": "model_tier must be one of standard, fast, deep"}
    csrf_token = mint_csrf_token(csrf_session_id)
    context = _build_form_context(csrf_token, mode=mode)
    context["errors"] = errors
    context["values"] = values
    template = "agents/_create_modal.html" if mode == "create" else "agents/_edit_modal.html"
    response = get_templates().TemplateResponse(
        request,
        template,
        context,
        status_code=status_code,
    )
    _set_csrf_cookie(response, csrf_token)
    return response
