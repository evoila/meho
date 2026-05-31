# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Runbooks UI lifecycle controls: publish / deprecate handler bodies + wiring.

Initiative #1381 (G10.6 Runbooks UI), Task #1384 (T3). The ``tenant_admin``
lifecycle half of the ``/ui/runbooks*`` surface -- the publish / deprecate
actions that drive a template through its state machine
(``draft --publish--> published --deprecate--> deprecated``) over the same
service the REST surface (:mod:`meho_backplane.api.v1.runbook_templates`) and
the CLI (``meho runbook publish-template`` / ``deprecate-template``) use.

Route inventory (all ``require_ui_admin``-gated):

* ``POST /ui/runbooks/{slug}/publish``    -> :meth:`RunbookTemplateService.publish`
  (mirrors REST ``POST /api/v1/runbooks/templates/{slug}/publish`` -- 200
  idempotent / 400 not-draft / 404).
* ``POST /ui/runbooks/{slug}/deprecate``  -> :meth:`RunbookTemplateService.deprecate`
  (mirrors REST ``POST /api/v1/runbooks/templates/{slug}/deprecate`` -- 200
  idempotent / 400 not-published / 404).

Both target the **specific** ``version`` carried by the detail / list view the
control was rendered into (the form posts a ``version`` field), so a stale
catalog row that pointed at an older version acts on that version rather than
silently retargeting the latest. The slug is the URL's job; the body carries
the integer version only -- the same single-source-of-truth posture the REST
``_VersionBody`` enforces.

Split out of :mod:`meho_backplane.ui.routes.runbooks.routes` (the read
surface, T1 #1382) and :mod:`meho_backplane.ui.routes.runbooks.editor` (the
authoring surface, T2 #1383) so the lifecycle concern lives in its own module
and no file crosses the code-quality size gate -- the same package-split
convention the editor used. This module holds the action handlers + the
fragment render; the FastAPI route wiring is :func:`register_lifecycle_routes`,
called from :func:`build_runbooks_router` ahead of the ``/ui/runbooks/{slug}``
catch-all.

Response shape
--------------

Each action posts via HTMX against the detail view's ``#runbook-lifecycle``
region and swaps the re-rendered :mod:`runbooks/_detail_actions.html` fragment
back in. That fragment carries:

* the lifecycle action row (the buttons valid for the new state), and
* an ``hx-swap-oob`` copy of the status badge, so the header badge in
  ``detail.html`` flips in place without a full-page reload, and
* an inline DaisyUI alert region -- a typed 400 (publishing a non-draft, or
  deprecating a non-published version) renders as an ``alert-error``; an
  idempotent re-action (200 no-op) just refreshes the badge with no error.

A 404 (missing slug / version, or a cross-tenant probe) is the one case that
raises ``HTTPException`` rather than re-rendering the fragment: the version the
control names no longer exists, so there is no coherent state to render -- the
detail page the operator is on is stale and a reload is the right move.

CSRF
----

The action buttons echo ``X-CSRF-Token`` via ``hx-headers`` (the token minted
on the detail render + set as the JS-readable cookie). The
:class:`~meho_backplane.ui.csrf.CSRFMiddleware` blocks any POST missing the
double-submit token before the handler runs -- a forged operator POST with no
token is a 403 at the CSRF gate; a forged operator POST *with* a token is a 403
at :func:`~meho_backplane.ui.auth.middleware.require_ui_admin` (the role
re-check is the real authority -- the hidden client controls are convenience
only).

References
----------

* HTMX 2.0.9 ``hx-swap-oob`` (out-of-band badge refresh):
  https://htmx.org/attributes/hx-swap-oob/
* DaisyUI 5.5.20 alert / badge: https://daisyui.com/components/alert/
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

import structlog
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse

from meho_backplane.runbooks.schemas import (
    DeprecateTemplateRequest,
    PublishTemplateRequest,
    ShowTemplateResponse,
)
from meho_backplane.runbooks.service import (
    RunbookTemplateService,
    TemplateNotDraftError,
    TemplateNotFoundError,
    TemplateNotPublishedError,
)
from meho_backplane.ui.auth.middleware import UISessionContext, require_ui_admin
from meho_backplane.ui.csrf import mint_csrf_token
from meho_backplane.ui.routes.runbooks.editor import set_csrf_cookie
from meho_backplane.ui.templating import get_templates

__all__ = ["register_lifecycle_routes"]

log = structlog.get_logger(__name__)

#: Module-level ``Depends`` closure for the ``tenant_admin`` gate every
#: lifecycle route requires (B008 idiom -- no call in a default arg). Mirrors
#: the editor surface: ``require_ui_admin`` re-verifies the session's access
#: token and raises 403 for ``operator`` / ``read_only`` before the handler
#: body runs.
_require_admin = Depends(require_ui_admin)

#: Cap on the ``version`` form field's string length before integer parsing.
#: A version is a small positive integer; this guards against an unbounded
#: form value reaching :func:`int` rather than expressing a real ceiling.
_MAX_VERSION_FIELD_LENGTH: Final[int] = 16


@dataclass(frozen=True)
class _ActionOutcome:
    """Resolved outcome of a publish / deprecate action.

    Exactly one of :attr:`template` (success -> re-render the fragment with
    the new status) or :attr:`error_message` (a typed 400 -> inline alert,
    badge unchanged) is the load-bearing field. On the error path
    :attr:`template` still carries the row in its *current* (unchanged) state
    so the fragment renders the correct badge + valid actions alongside the
    alert.
    """

    template: ShowTemplateResponse
    error_message: str | None


async def _resolve_template(
    session: UISessionContext, slug: str, version: int
) -> ShowTemplateResponse:
    """Load ``(slug, version)`` for the current tenant, or 404.

    A missing / cross-tenant ``(slug, version)`` raises 404 -- the version the
    control named no longer resolves, so the detail page is stale and a reload
    is the right move (the fragment has no coherent state to render). The
    service's tenant filter makes another tenant's row invisible, so a
    cross-tenant probe collapses to the same 404 as a genuinely absent row.
    """
    try:
        return await RunbookTemplateService().show_template(
            session.tenant_id, slug, version=version
        )
    except TemplateNotFoundError as exc:
        raise HTTPException(status_code=404, detail="runbook_template_not_found") from exc


async def _publish(session: UISessionContext, slug: str, version: int) -> _ActionOutcome:
    """Promote ``(slug, version)`` draft -> published; map the typed 400 to an alert.

    Idempotent: re-publishing an already-published version is a service-layer
    no-op (the fragment re-renders with the published badge, no error).
    Publishing a deprecated version raises :class:`TemplateNotDraftError` ->
    inline ``alert-error`` with the badge left on its current state.
    """
    try:
        await RunbookTemplateService().publish(
            session.tenant_id, PublishTemplateRequest(slug=slug, version=version)
        )
    except TemplateNotFoundError as exc:
        raise HTTPException(status_code=404, detail="runbook_template_not_found") from exc
    except TemplateNotDraftError as exc:
        template = await _resolve_template(session, slug, version)
        return _ActionOutcome(template=template, error_message=str(exc))
    template = await _resolve_template(session, slug, version)
    return _ActionOutcome(template=template, error_message=None)


async def _deprecate(session: UISessionContext, slug: str, version: int) -> _ActionOutcome:
    """Retire ``(slug, version)`` published -> deprecated; map the typed 400 to an alert.

    Idempotent: re-deprecating an already-deprecated version is a service-layer
    no-op. Deprecating a draft raises :class:`TemplateNotPublishedError` ->
    inline ``alert-error`` with the badge unchanged.
    """
    try:
        await RunbookTemplateService().deprecate(
            session.tenant_id, DeprecateTemplateRequest(slug=slug, version=version)
        )
    except TemplateNotFoundError as exc:
        raise HTTPException(status_code=404, detail="runbook_template_not_found") from exc
    except TemplateNotPublishedError as exc:
        template = await _resolve_template(session, slug, version)
        return _ActionOutcome(template=template, error_message=str(exc))
    template = await _resolve_template(session, slug, version)
    return _ActionOutcome(template=template, error_message=None)


async def _render_actions_fragment(
    request: Request,
    session: UISessionContext,
    outcome: _ActionOutcome,
) -> HTMLResponse:
    """Render the ``_detail_actions.html`` fragment for an HTMX swap.

    Carries the post-action status badge (with an ``hx-swap-oob`` copy that
    refreshes the header badge), the lifecycle action row valid for the new
    state, and -- on the typed-400 path -- the inline alert. CSRF is re-minted
    (the prior token was consumed by this POST) so the next action carries a
    live token; the cookie is refreshed to match.
    """
    template = outcome.template
    in_flight = 0
    if template.status == "published":
        in_flight = await RunbookTemplateService().count_in_flight_runs(
            session.tenant_id, template.slug, template.version
        )
    csrf_token = mint_csrf_token(str(session.session_id))
    context = {
        "template": template,
        "is_admin": True,
        "in_flight_run_count": in_flight,
        "lifecycle_error": outcome.error_message,
        "csrf_token": csrf_token,
        "swap_badge": True,
    }
    response = get_templates().TemplateResponse(request, "runbooks/_detail_actions.html", context)
    set_csrf_cookie(response, csrf_token)
    return response


def _parse_version(raw: str) -> int:
    """Parse the ``version`` form field to a positive int, or 422.

    The control always posts the integer version it was rendered against; a
    non-integer / non-positive value is a malformed request (a tampered form),
    so a 422 is the right boundary failure rather than a 500 deeper in.
    """
    try:
        value = int(raw.strip())
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail="version must be an integer") from exc
    if value < 1:
        raise HTTPException(status_code=422, detail="version must be >= 1")
    return value


def register_lifecycle_routes(router: APIRouter) -> None:
    """Register the ``require_ui_admin``-gated publish / deprecate routes.

    The T3 (#1384) lifecycle surface: the publish + deprecate POST handlers.
    Called by
    :func:`meho_backplane.ui.routes.runbooks.routes.build_runbooks_router`
    after the read + editor routes so the literal segments are registered
    BEFORE ``/ui/runbooks/{slug}`` (FastAPI is first-match-wins -- the
    ``{slug}`` catch-all would otherwise swallow ``publish`` / ``deprecate``).
    Every route declares ``_require_admin``: the server is the single source of
    truth for the privilege; an ``operator`` gets 403 at the dependency, before
    the handler body runs.
    """

    @router.post("/ui/runbooks/{slug}/publish", response_class=HTMLResponse)
    async def runbooks_publish(
        slug: str,
        request: Request,
        session: UISessionContext = _require_admin,
        version: str = Form(default="", max_length=_MAX_VERSION_FIELD_LENGTH),
    ) -> HTMLResponse:
        """Publish ``(slug, version)`` (draft -> published); admin only."""
        parsed = _parse_version(version)
        outcome = await _publish(session, slug.strip(), parsed)
        return await _render_actions_fragment(request, session, outcome)

    @router.post("/ui/runbooks/{slug}/deprecate", response_class=HTMLResponse)
    async def runbooks_deprecate(
        slug: str,
        request: Request,
        session: UISessionContext = _require_admin,
        version: str = Form(default="", max_length=_MAX_VERSION_FIELD_LENGTH),
    ) -> HTMLResponse:
        """Deprecate ``(slug, version)`` (published -> deprecated); admin only."""
        parsed = _parse_version(version)
        outcome = await _deprecate(session, slug.strip(), parsed)
        return await _render_actions_fragment(request, session, outcome)
