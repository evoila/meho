# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Grant create / elevate / revoke (DaisyUI modal, HTMX).

Initiative #1824 (G10.8 Agents console), Task #1832 (T5). The read
surface (table + detail) lives in
:mod:`~meho_backplane.ui.routes.agents.grants.views`; this module
layers the three writes on top:

* **``GET /ui/agents/grants/create``** -- HTMX-loaded create modal. The
  ``verdict`` ``<select>`` is server-rendered from the
  :class:`~meho_backplane.db.models.PermissionVerdict` enum.
* **``POST /ui/agents/grants/create``** -- submit handler. Builds an
  :class:`~meho_backplane.agents.grant_schemas.AgentGrantCreate` and
  persists via :meth:`~meho_backplane.agents.grants.AgentGrantService.grant`.
  Success -> 204 + ``HX-Redirect: /ui/agents/grants``. A Pydantic
  :class:`~pydantic.ValidationError` or a
  :class:`~meho_backplane.agents.grants.GrantValidationError` (past
  expiry, bad ``target_scope`` UUID, duplicate grant) re-renders the
  modal with the matching field error + 422.
* **``GET /ui/agents/grants/elevate``** -- HTMX-loaded elevate modal.
  Identical to create plus ``expires_at`` is **required** (it's a
  time-bounded elevation); the modal makes the auto-expiry plain.
* **``POST /ui/agents/grants/elevate``** -- submit handler. Builds an
  :class:`~meho_backplane.agents.grant_schemas.AgentElevationCreate`
  (``expires_at`` required) so an omitted / past expiry surfaces as the
  inline ``expires_at`` field error.
* **``POST /ui/agents/grants/{grant_id}/revoke``** -- revoke submit
  (a native-``<dialog>`` confirm gates it client-side; the destructive
  DELETE-equivalent runs server-side). Success -> 204 +
  ``HX-Redirect: /ui/agents/grants``; 404 on an absent / cross-tenant
  id.

The ``principal_sub`` field is free-text (the picker over registered
principals is T4 #1831; until it lands a free-text field is the
accepted shape -- the same precedent the agent-definitions create modal
set for ``identity_ref`` per #1825). The grant service does not
validate ``principal_sub`` against registered principals (a grant may
be issued ahead of the principal's first login), so a free-text sub is
accepted as the backend contract intends.

RBAC posture
------------

The **entire** surface -- reads included -- is tenant_admin (grant
listings reveal the tenant's least-privilege posture, so they are
governance data; see
:mod:`~meho_backplane.ui.routes.agents.grants.operator`). Every route
depends on
:func:`~meho_backplane.ui.routes.agents.grants.operator.resolve_grants_admin_or_403`,
which lifts the full :class:`~meho_backplane.auth.operator.Operator`
from the BFF session, re-validates the access token through the chassis
JWT chain, and raises 403 for a non-admin caller.

CSRF posture
------------

``POST`` under ``/ui/`` is gated by the chassis
:class:`~meho_backplane.ui.csrf.CSRFMiddleware` (signed double-submit)
before the handler runs. Each modal render re-mints + re-sets the
``meho_csrf`` cookie and the form declares its own ``hx-headers`` echo
so the double-submit pair lines up (#1693). Revoke is destructive, so a
native ``<dialog>`` confirm wraps the submit.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

import structlog
from fastapi import Request, status
from fastapi.responses import HTMLResponse
from pydantic import ValidationError

from meho_backplane.agents.grant_schemas import (
    AgentElevationCreate,
    AgentGrantCreate,
    GrantVerdict,
)
from meho_backplane.agents.grants import AgentGrantService, GrantValidationError
from meho_backplane.auth.operator import Operator
from meho_backplane.ui.auth.middleware import UISessionContext
from meho_backplane.ui.csrf import CSRF_COOKIE_NAME, mint_csrf_token
from meho_backplane.ui.routes.agents.grants.views import _fetch_grant_or_404
from meho_backplane.ui.templating import get_templates

__all__ = [
    "EXPIRES_AT_MAX",
    "OP_PATTERN_MAX",
    "PRINCIPAL_SUB_MAX",
    "TARGET_SCOPE_MAX",
    "render_create_modal",
    "render_elevate_modal",
    "render_revoke_modal",
    "submit_create",
    "submit_elevate",
    "submit_revoke",
]

_log = structlog.get_logger(__name__)

#: Form-field length caps mirroring the
#: :class:`~meho_backplane.agents.grant_schemas.AgentGrantCreate` bounds.
#: The server-side Pydantic validation is authoritative; these caps
#: bound the form-body parse before the bytes reach the schema.
PRINCIPAL_SUB_MAX: int = 512
OP_PATTERN_MAX: int = 512
TARGET_SCOPE_MAX: int = 256
#: ``expires_at`` arrives as an ISO-8601 / HTML ``datetime-local`` string
#: (e.g. ``2026-07-01T13:30`` or ``2026-07-01T13:30:00+00:00``). 64
#: chars comfortably bounds the longest offset-bearing ISO form.
EXPIRES_AT_MAX: int = 64

#: Verdict the create modal defaults its ``<select>`` to. ``deny`` is the
#: conservative default -- a slip while filling the form should land on
#: the most restrictive verdict, never silently auto-execute.
_DEFAULT_VERDICT: str = GrantVerdict.DENY.value


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
    convention. Same shape as the agent-definitions forms helper.
    """
    errors: dict[str, str] = {}
    for err in exc.errors():
        loc = err.get("loc") or ()
        field = str(loc[0]) if loc else "__root__"
        errors.setdefault(field, str(err.get("msg", "invalid value")))
    return errors


def _build_form_context(csrf_token: str, *, mode: str) -> dict[str, object]:
    """Build the template context shared by the create + elevate modals."""
    return {
        "page_title": "Agent grants",
        "active_surface": "agents",
        "csrf_token": csrf_token,
        "mode": mode,
        "verdicts": [verdict.value for verdict in GrantVerdict],
        "default_verdict": _DEFAULT_VERDICT,
        # Bare-form render carries no errors / submitted values.
        "errors": {},
        "values": {},
    }


def _redirect_to_list() -> HTMLResponse:
    """Return a 204 + ``HX-Redirect`` so HTMX reloads the grants table."""
    return HTMLResponse(
        status_code=status.HTTP_204_NO_CONTENT,
        headers={"HX-Redirect": "/ui/agents/grants"},
    )


async def render_create_modal(
    request: Request,
    session_ctx: UISessionContext,
) -> HTMLResponse:
    """Render the HTMX-loaded create-grant modal (tenant_admin-gated route)."""
    csrf_token = mint_csrf_token(str(session_ctx.session_id))
    response = get_templates().TemplateResponse(
        request,
        "agents/grants/_create_modal.html",
        _build_form_context(csrf_token, mode="create"),
    )
    _set_csrf_cookie(response, csrf_token)
    return response


async def render_elevate_modal(
    request: Request,
    session_ctx: UISessionContext,
) -> HTMLResponse:
    """Render the HTMX-loaded elevate modal (``expires_at`` required)."""
    csrf_token = mint_csrf_token(str(session_ctx.session_id))
    response = get_templates().TemplateResponse(
        request,
        "agents/grants/_elevate_modal.html",
        _build_form_context(csrf_token, mode="elevate"),
    )
    _set_csrf_cookie(response, csrf_token)
    return response


async def render_revoke_modal(
    request: Request,
    session_ctx: UISessionContext,
    *,
    grant_id: UUID,
) -> HTMLResponse:
    """Render the revoke-confirm modal (404 on absent / cross-tenant id).

    Revoke is destructive (it drops a principal's permission), so a
    native ``<dialog>`` confirm gates the submit. The modal surfaces the
    grant's identity so the operator confirms they are revoking the
    right one.
    """
    grant = await _fetch_grant_or_404(session_ctx, grant_id)
    csrf_token = mint_csrf_token(str(session_ctx.session_id))
    context: dict[str, object] = {
        "page_title": "Agent grants",
        "active_surface": "agents",
        "csrf_token": csrf_token,
        "grant": {
            "id": str(grant.id),
            "principal_sub": grant.principal_sub,
            "op_pattern": grant.op_pattern,
            "verdict": grant.verdict,
        },
    }
    response = get_templates().TemplateResponse(
        request, "agents/grants/_revoke_modal.html", context
    )
    _set_csrf_cookie(response, csrf_token)
    return response


def _coerce_expires_at(raw: str | None) -> datetime | str | None:
    """Coerce the free-text ``expires_at`` field to ``datetime`` (or pass through).

    Returns ``None`` for an empty value (a permanent grant on the create
    path), a parsed :class:`~datetime.datetime` for a valid ISO-8601
    string, or the raw string when parsing fails so the schema's
    ``datetime`` field surfaces a typed 422 (rather than this helper
    raising a bare ``ValueError`` that would 500). An HTML
    ``datetime-local`` input posts a value like ``2026-07-01T13:30`` (no
    offset); :meth:`datetime.fromisoformat` parses that as naive, which
    the service's ``_validate_expires_at`` then rejects with a clear
    "must be timezone-aware" message -- so the modal pairs the field
    with a hint to include an offset / ``Z``.
    """
    if raw is None or not raw.strip():
        return None
    try:
        return datetime.fromisoformat(raw.strip())
    except ValueError:
        return raw.strip()


def _coerce_target_scope(raw: str | None) -> str | None:
    """Normalise the free-text ``target_scope`` field.

    An empty value means "any target" -> ``None`` (the schema default);
    a non-empty value passes through verbatim so the service's UUID-or-``*``
    validator owns the semantic check.
    """
    if raw is None or not raw.strip():
        return None
    return raw.strip()


def _raw_values(
    *,
    principal_sub: str,
    op_pattern: str,
    target_scope: str | None,
    verdict: str,
    expires_at: str | None,
) -> dict[str, object]:
    """Echo dict so a re-rendered modal keeps the operator's typed input."""
    return {
        "principal_sub": principal_sub,
        "op_pattern": op_pattern,
        "target_scope": target_scope or "",
        "verdict": verdict,
        "expires_at": expires_at or "",
    }


async def _persist_grant(
    request: Request,
    session_ctx: UISessionContext,
    operator: Operator,
    *,
    payload: AgentGrantCreate,
    mode: str,
    raw_values: dict[str, object],
) -> HTMLResponse:
    """Persist a validated grant payload, re-rendering the modal on a service error.

    Shared by the create + elevate submit paths -- both build a
    :class:`AgentGrantCreate` (elevate via its ``expires_at``-required
    subclass) and dispatch through the one
    :meth:`~meho_backplane.agents.grants.AgentGrantService.grant` code
    path. A :class:`~meho_backplane.agents.grants.GrantValidationError`
    (past expiry, bad ``target_scope`` UUID, duplicate grant) re-renders
    inline with 422; a clean grant returns 204 + ``HX-Redirect``.
    """
    service = AgentGrantService()
    try:
        entry = await service.grant(session_ctx.tenant_id, operator.sub, payload)
    except GrantValidationError as exc:
        return _render_form_with_errors(
            request,
            csrf_session_id=str(session_ctx.session_id),
            mode=mode,
            field_errors=_service_error_to_field(exc.message),
            values=raw_values,
        )
    _log.info(
        "ui_agent_grant_create",
        tenant_id=str(session_ctx.tenant_id),
        operator_sub=session_ctx.operator_sub,
        mode=mode,
        principal_sub=payload.principal_sub,
        op_pattern=payload.op_pattern,
        verdict=payload.verdict.value,
        grant_id=str(entry.id),
        is_elevation=payload.expires_at is not None,
    )
    return _redirect_to_list()


def _service_error_to_field(message: str) -> dict[str, str]:
    """Map a :class:`GrantValidationError` message to the offending field.

    The service raises one message string covering several semantic
    cases; route it to the field the operator can fix so the inline
    error lands on the right input rather than a generic banner.
    """
    lowered = message.lower()
    if "expires_at" in lowered:
        return {"expires_at": message}
    if "target_scope" in lowered:
        return {"target_scope": message}
    if "op_pattern" in lowered:
        return {"op_pattern": message}
    if "already exists" in lowered:
        # The uniqueness key is (principal, op_pattern, target_scope);
        # the op_pattern field is the most actionable anchor.
        return {"op_pattern": message}
    return {"__root__": message}


async def submit_create(
    request: Request,
    session_ctx: UISessionContext,
    operator: Operator,
    *,
    principal_sub: str,
    op_pattern: str,
    target_scope: str | None,
    verdict: str,
    expires_at: str | None,
) -> HTMLResponse:
    """Build an :class:`AgentGrantCreate` and persist it via the service.

    ``expires_at`` is optional here -- omit it for a permanent grant.
    A Pydantic :class:`ValidationError` (empty ``op_pattern`` / unknown
    ``verdict`` / unparseable ``expires_at``) re-renders the modal with
    per-field messages + 422; a service-level
    :class:`~meho_backplane.agents.grants.GrantValidationError` (past
    expiry, bad UUID scope, duplicate) does the same. A clean create
    returns 204 + ``HX-Redirect: /ui/agents/grants``.
    """
    raw_values = _raw_values(
        principal_sub=principal_sub,
        op_pattern=op_pattern,
        target_scope=target_scope,
        verdict=verdict,
        expires_at=expires_at,
    )
    try:
        payload = AgentGrantCreate(
            principal_sub=principal_sub,
            op_pattern=op_pattern,
            target_scope=_coerce_target_scope(target_scope),
            verdict=GrantVerdict(verdict),
            expires_at=_coerce_expires_at(expires_at),  # type: ignore[arg-type]
        )
    except (ValidationError, ValueError) as exc:
        return _render_form_with_errors(
            request,
            csrf_session_id=str(session_ctx.session_id),
            mode="create",
            exc=exc,
            values=raw_values,
        )
    return await _persist_grant(
        request,
        session_ctx,
        operator,
        payload=payload,
        mode="create",
        raw_values=raw_values,
    )


async def submit_elevate(
    request: Request,
    session_ctx: UISessionContext,
    operator: Operator,
    *,
    principal_sub: str,
    op_pattern: str,
    target_scope: str | None,
    verdict: str,
    expires_at: str | None,
) -> HTMLResponse:
    """Build an :class:`AgentElevationCreate` and persist it via the service.

    Identical to :func:`submit_create` except ``expires_at`` is
    **required** -- an omitted / empty value surfaces as the inline
    ``expires_at`` field error (the
    :class:`~meho_backplane.agents.grant_schemas.AgentElevationCreate`
    subclass makes the field non-optional), and a past / naive value is
    rejected by the service with a clear message. A clean elevation
    returns 204 + ``HX-Redirect``.
    """
    raw_values = _raw_values(
        principal_sub=principal_sub,
        op_pattern=op_pattern,
        target_scope=target_scope,
        verdict=verdict,
        expires_at=expires_at,
    )
    try:
        payload: AgentGrantCreate = AgentElevationCreate(
            principal_sub=principal_sub,
            op_pattern=op_pattern,
            target_scope=_coerce_target_scope(target_scope),
            verdict=GrantVerdict(verdict),
            expires_at=_coerce_expires_at(expires_at),  # type: ignore[arg-type]
        )
    except (ValidationError, ValueError) as exc:
        return _render_form_with_errors(
            request,
            csrf_session_id=str(session_ctx.session_id),
            mode="elevate",
            exc=exc,
            values=raw_values,
        )
    return await _persist_grant(
        request,
        session_ctx,
        operator,
        payload=payload,
        mode="elevate",
        raw_values=raw_values,
    )


async def submit_revoke(
    session_ctx: UISessionContext,
    operator: Operator,
    *,
    grant_id: UUID,
) -> HTMLResponse:
    """Revoke one grant by id.

    404 when no row matched (absent or cross-tenant) so the UX matches
    the REST DELETE contract; a clean revoke returns 204 +
    ``HX-Redirect: /ui/agents/grants``. Revoking drops the principal's
    explicit permission, reverting it to the ``safety_level`` default
    (which never auto-executes a dangerous op).
    """
    del operator  # gate only; the service revoke is tenant-scoped by session.
    service = AgentGrantService()
    deleted = await service.revoke(session_ctx.tenant_id, grant_id)
    if not deleted:
        from fastapi import HTTPException

        raise HTTPException(status_code=404, detail="grant_not_found")
    _log.info(
        "ui_agent_grant_revoke",
        tenant_id=str(session_ctx.tenant_id),
        operator_sub=session_ctx.operator_sub,
        grant_id=str(grant_id),
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
) -> HTMLResponse:
    """Re-render the create / elevate modal carrying per-field errors + 422.

    The HTMX form targets the modal container with
    ``hx-swap="innerHTML"`` so the error body replaces the dialog in
    place; the operator keeps their typed values (echoed via ``values``)
    and sees the field-level messages. ``exc`` covers the Pydantic /
    coercion path; ``field_errors`` covers the service-raised
    :class:`~meho_backplane.agents.grants.GrantValidationError` cases.
    """
    if field_errors is not None:
        errors = field_errors
    elif isinstance(exc, ValidationError):
        errors = _validation_errors_by_field(exc)
    else:
        # A bare ValueError from the verdict enum coercion.
        errors = {"verdict": "verdict must be one of auto-execute, needs-approval, deny"}
    csrf_token = mint_csrf_token(csrf_session_id)
    context = _build_form_context(csrf_token, mode=mode)
    context["errors"] = errors
    context["values"] = values
    template = (
        "agents/grants/_create_modal.html"
        if mode == "create"
        else "agents/grants/_elevate_modal.html"
    )
    response = get_templates().TemplateResponse(
        request,
        template,
        context,
        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
    )
    _set_csrf_cookie(response, csrf_token)
    return response
