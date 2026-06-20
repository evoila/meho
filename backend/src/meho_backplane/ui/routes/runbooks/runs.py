# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Runbooks UI run surface: role-scoped runs list + start-run modal + start handler.

Initiative #1837 (G10.11 Runbook runs UI), Task #1884 (T1). The entry
surface for the runbook *run* lifecycle on the operator console -- the
console already ships full template authoring (catalog + editor +
publish/deprecate, G10.6 #1381 in :mod:`~meho_backplane.ui.routes.runbooks.routes`);
this module adds the "Runs" tab: list the runs you own (or, for a
``tenant_admin``, every run in the tenant) and start a new one.

This is a pure UI/BFF build over the in-process
:class:`~meho_backplane.runbooks.run_service.RunbookRunService` -- the
``require_ui_session`` gate + a direct service call, never the Bearer
``/api/v1`` surface (the same posture the read surface uses calling
:class:`~meho_backplane.runbooks.service.RunbookTemplateService` in
process). The REST run routes (G12.3-T5 #1311,
:mod:`meho_backplane.api.v1.runbook_runs`) are the analogue, not the
dependency. The run **driver** (``/ui/runbooks/runs/{run_id}`` -- render
the current verify-gated step, Advance/Abort/Reassign) is T2 (#1893) and
depends on this list shipping first; each row links to that future route.

Route inventory (all registered ahead of ``/ui/runbooks/{slug}``)
----------------------------------------------------------------

* ``GET /ui/runbooks/runs`` -- runs list page. Resolves ``caller_is_admin``
  via the read surface's soft role lift
  (:func:`~meho_backplane.ui.routes.runbooks.routes._is_admin`, which fails
  soft to operator privileges rather than 5xx-ing the read page) and calls
  :meth:`RunbookRunService.list_runs` with it. The visibility split is
  **service-enforced**: an ``OPERATOR`` only ever sees their own runs (the
  service forces ``assignee=caller_sub`` regardless of the filter), a
  ``TENANT_ADMIN`` sees every tenant run and may filter by ``assignee``.
  ``HX-Request: true`` returns only the ``runbooks/_runs_list.html``
  fragment (filter swap); direct navigation returns the full page.

* ``GET /ui/runbooks/runs/start`` -- HTMX-loaded start-run modal fragment
  (mirrors ``memory/_create_modal.html``). Registered **before**
  ``/ui/runbooks/runs/{run_id}`` (T2) so the literal ``start`` segment is
  not swallowed by the ``{run_id}`` param route. Pre-populates a
  ``<datalist>`` of the tenant's published template slugs so the operator
  can pick one (free text still allowed -- the service resolves the slug).

* ``POST /ui/runbooks/runs`` -- start handler. Operator floor (the session
  gate is the floor; the service auto-assigns the caller). Builds
  :class:`~meho_backplane.runbooks.runs_schemas.StartRunRequest` and calls
  :meth:`RunbookRunService.start_run`. On success returns 204 +
  ``HX-Redirect: /ui/runbooks/runs/{run_id}`` (drops the operator into the
  T2 driver). The typed start errors
  (:class:`DeprecatedTemplateError` / :class:`TemplateNotFoundError` /
  :class:`MissingParamsError`) map to an inline modal ``alert-error``
  (HTTP 200 fragment, **not** a 500) so the operator sees *which*
  ``${run.params.X}`` to supply rather than a stack trace.

Role scoping
------------

Same two-layer posture as the read surface and the REST run routes:

* The **list** read is best-effort on role -- the soft lift degrades to
  operator privileges on any hiccup, which only ever *narrows* what the
  caller sees (their own runs), never widens it. The service is the
  authority: even a forged ``?assignee=`` is ignored for a non-admin.
* The **start** write has no role gate beyond the operator-session floor
  (matching REST ``start_run``'s ``operator`` floor) -- the service
  auto-assigns ``operator_sub`` as ``assigned_to`` and refuses to start a
  run for any other principal.

CSRF
----

Double-submit per :mod:`meho_backplane.ui.csrf`. The modal render mints a
token, echoes it on the form via ``hx-headers``, and -- on the **same
response** -- refreshes the ``meho_csrf`` cookie via
:func:`~meho_backplane.ui.routes.runbooks.editor.set_csrf_cookie`. Skipping
the cookie refresh is the desync footgun the memory create modal (#1693)
and the runbooks list fragment fixed: the inherited page-level token goes
stale the moment a fragment render rotates the cookie, and the chassis
:class:`~meho_backplane.ui.csrf.CSRFMiddleware` then 403s the submit with
``value_mismatch``. The start POST therefore carries a header token that
matches the cookie the modal just set.

References
----------

* HTMX 2.0.9 ``HX-Redirect`` (post-mutation client-side redirect) +
  ``hx-disabled-elt`` (``find button[type=submit]`` extended selector) +
  ``HX-Request`` fragment swap: https://htmx.org/reference/
* DaisyUI 5.5.20 table / badge / modal: https://daisyui.com/components/modal/
* Service role-split precedent: ``api/v1/runbook_runs.py:518-602``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated, Final, Literal

import structlog
from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse

from meho_backplane.runbooks.run_service import (
    MissingParamsError,
    RunbookRunService,
)
from meho_backplane.runbooks.runs_schemas import (
    ListRunsFilter,
    RunSummary,
    StartRunRequest,
)
from meho_backplane.runbooks.schemas import ListTemplatesFilter

# ``TemplateNotFoundError`` / ``DeprecatedTemplateError`` are canonically
# defined in ``runbooks.service`` (the read surface imports the former from
# there too); ``MissingParamsError`` is the run-service's own start-time
# guard. Importing the template-resolution errors from their definition
# module keeps the explicit-re-export check happy.
from meho_backplane.runbooks.service import (
    DeprecatedTemplateError,
    RunbookTemplateService,
    TemplateNotFoundError,
)
from meho_backplane.ui.auth.middleware import UISessionContext, require_ui_session
from meho_backplane.ui.csrf import mint_csrf_token
from meho_backplane.ui.query_filters import EMPTY_STR_TO_NONE
from meho_backplane.ui.routes.runbooks.editor import set_csrf_cookie
from meho_backplane.ui.templating import get_templates

__all__ = ["register_runs_routes"]

log = structlog.get_logger(__name__)

#: The closed run-state vocabulary the list filter accepts, matching the
#: ``RunSummary.state`` / ``ListRunsFilter.status`` literal. An out-of-vocab
#: value trips a clean 422 at FastAPI's query boundary (mirroring the REST
#: ``list_runs`` handler's typed ``status`` query param) rather than
#: constructing an invalid :class:`ListRunsFilter`.
_RunStateFilter = Literal["in_progress", "completed", "abandoned"]

#: Default + cap on the number of run rows surfaced. The service caps at
#: 500; the console mirrors the REST default of 100 so a tenant with a
#: large run history still renders deterministically.
_DEFAULT_LIMIT: Final[int] = 100

#: Cap on the free-text ``assignee`` filter value (admin-only control).
#: Generous for any operator subject id while keeping the query string
#: representable and out of unbounded-input territory.
_MAX_ASSIGNEE_LENGTH: Final[int] = 256

#: Cap on the number of published template slugs offered in the start
#: modal's ``<datalist>``. The picker is a convenience (free text is still
#: accepted), so a generous-but-bounded list keeps the fragment small.
_PUBLISHED_PICKER_LIMIT: Final[int] = 200

#: Cap on the ``template_slug`` / ``target`` form fields. Slugs follow the
#: template ``SLUG_PATTERN`` (short); ``target`` is a host / cluster /
#: thumbprint label. Both are bounded here as defence-in-depth before the
#: value reaches the service.
_MAX_SLUG_LENGTH: Final[int] = 256
_MAX_TARGET_LENGTH: Final[int] = 512

#: Cap on the ``work_ref`` form field (an opaque external change-ticket
#: reference -- a GitHub issue ref, a Jira key, a CR id).
_MAX_WORK_REF_LENGTH: Final[int] = 512

#: Cap on the raw ``params`` JSON textarea before parsing. A substitution
#: context is small; this guards the JSON parser against an unbounded paste.
_MAX_PARAMS_JSON_LENGTH: Final[int] = 16_384

#: Module-level ``Depends`` closure for the operator-session gate (B008
#: idiom -- no call in a default arg). Matches the read surface's
#: ``_require_session``.
_require_session = Depends(require_ui_session)


async def _list_run_summaries(
    session: UISessionContext,
    *,
    is_admin: bool,
    assignee: str | None,
    status: _RunStateFilter | None,
) -> list[RunSummary]:
    """Fetch the run projection for the current tenant, scoped to the caller.

    Builds a :class:`ListRunsFilter` and passes ``caller_is_admin`` through
    to :meth:`RunbookRunService.list_runs`. The service enforces the
    visibility split: an ``OPERATOR`` (``is_admin=False``) only ever sees
    their own runs even if ``assignee`` names someone else; a
    ``TENANT_ADMIN`` honours the filter as-is.
    """
    filter_ = ListRunsFilter(assignee=assignee, status=status)
    return await RunbookRunService().list_runs(
        tenant_id=session.tenant_id,
        caller_sub=session.operator_sub,
        caller_is_admin=is_admin,
        filter_=filter_,
        limit=_DEFAULT_LIMIT,
    )


def _runs_context(
    summaries: list[RunSummary],
    *,
    is_admin: bool,
    assignee: str | None,
    status: _RunStateFilter | None,
    operator_sub: str,
    csrf_token: str,
) -> dict[str, object]:
    """Assemble the shared template context for the runs page + fragment."""
    return {
        "runs": summaries,
        "is_admin": is_admin,
        "assignee_filter": assignee or "",
        "status_filter": status or "",
        "operator_sub": operator_sub,
        "csrf_token": csrf_token,
        "active_surface": "runbooks",
        "active_tab": "runs",
        "page_title": "Runbook runs",
    }


async def _render_runs(
    request: Request,
    session: UISessionContext,
    assignee: str | None,
    status: _RunStateFilter | None,
) -> HTMLResponse:
    """Render the runs list page, or the HTMX ``_runs_list.html`` fragment.

    ``HX-Request: true`` -> the fragment only (the filter controls swap
    ``#runbook-runs-list`` in place). Direct navigation -> the full page.
    Both share the same projection so the markup is identical. The role lift
    fails soft to operator privileges, so the ``assignee`` filter is only
    honoured for a real ``tenant_admin`` (the service ignores it otherwise).
    """
    from meho_backplane.ui.routes.runbooks.routes import _is_admin

    is_admin = await _is_admin(session)
    # The assignee control is admin-only; an operator never renders it, and
    # the service would ignore a forged value anyway. Drop it pre-call so an
    # operator's run list is never even filter-shaped by a stray query param.
    effective_assignee = assignee if is_admin else None
    summaries = await _list_run_summaries(
        session, is_admin=is_admin, assignee=effective_assignee, status=status
    )
    csrf_token = mint_csrf_token(str(session.session_id))
    context = _runs_context(
        summaries,
        is_admin=is_admin,
        assignee=effective_assignee,
        status=status,
        operator_sub=session.operator_sub,
        csrf_token=csrf_token,
    )
    if request.headers.get("HX-Request") == "true":
        return get_templates().TemplateResponse(request, "runbooks/_runs_list.html", context)
    response = get_templates().TemplateResponse(request, "runbooks/runs.html", context)
    set_csrf_cookie(response, csrf_token)
    return response


async def _render_start_modal(
    request: Request,
    session: UISessionContext,
) -> HTMLResponse:
    """Render the HTMX-loaded start-run modal fragment.

    Pre-populates a ``<datalist>`` of the tenant's published template slugs
    (newest-edited first) so the operator can pick one; free text is still
    accepted because the service resolves the slug to its latest published
    version. The form echoes the freshly-minted CSRF token via
    ``hx-headers`` and this same response refreshes the ``meho_csrf`` cookie
    so the immediately-following start POST's double-submit pair lines up
    (the #1693 desync footgun).
    """
    published = await RunbookTemplateService().list_templates(
        session.tenant_id,
        ListTemplatesFilter(status="published", target_kind=None),
        limit=_PUBLISHED_PICKER_LIMIT,
    )
    csrf_token = mint_csrf_token(str(session.session_id))
    context = {
        "published_slugs": [tpl.slug for tpl in published],
        "csrf_token": csrf_token,
        "error_message": None,
    }
    response = get_templates().TemplateResponse(request, "runbooks/_start_modal.html", context)
    set_csrf_cookie(response, csrf_token)
    return response


def _parse_params_or_422(raw: str) -> dict[str, object]:
    """Parse the ``params`` JSON textarea to an object, or 422.

    Empty / whitespace -> ``{}`` (the service default). A non-empty value
    must be a JSON **object** (the substitution context is a mapping of
    ``${run.params.X}`` names to values); a JSON array / scalar, or
    unparseable text, is a malformed request and surfaces as a 422 at the
    handler boundary rather than a 500 deeper in. This is the client-side
    "reject non-object" guard the issue scopes; ``MissingParamsError``
    covers the rest (a well-formed object that omits a referenced key).
    """
    import json

    if not raw or not raw.strip():
        return {}
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail="params must be valid JSON") from exc
    if not isinstance(parsed, dict):
        raise HTTPException(status_code=422, detail="params must be a JSON object")
    return parsed


async def _render_start_error(
    request: Request,
    session: UISessionContext,
    message: str,
) -> HTMLResponse:
    """Re-render the start modal carrying an inline ``alert-error``.

    A typed start error (deprecated / not-found / missing-params) is an
    operator-recoverable condition, not a server fault: the modal stays open
    with the alert (HTTP 200 fragment) so the operator can correct the
    template slug / target / params and resubmit. The fragment re-mints the
    CSRF token + refreshes the cookie so the retry's double-submit pair
    still matches (the prior token was consumed by this POST). The published
    ``<datalist>`` is re-populated so the picker survives the error render.
    """
    published = await RunbookTemplateService().list_templates(
        session.tenant_id,
        ListTemplatesFilter(status="published", target_kind=None),
        limit=_PUBLISHED_PICKER_LIMIT,
    )
    csrf_token = mint_csrf_token(str(session.session_id))
    context = {
        "published_slugs": [tpl.slug for tpl in published],
        "csrf_token": csrf_token,
        "error_message": message,
    }
    response = get_templates().TemplateResponse(request, "runbooks/_start_modal.html", context)
    set_csrf_cookie(response, csrf_token)
    return response


@dataclass(frozen=True)
class _StartForm:
    """The raw start-run form fields, before validation."""

    template_slug: str
    target: str
    params_raw: str
    work_ref: str


async def _build_start_request(
    request: Request,
    session: UISessionContext,
    form: _StartForm,
) -> StartRunRequest | HTMLResponse:
    """Validate the form into a :class:`StartRunRequest`, or an error response.

    Returns the typed request on success. A missing slug / target re-renders
    the modal with an inline alert (returned :class:`HTMLResponse`); a
    malformed ``params`` value raises 422 inside :func:`_parse_params_or_422`.
    """
    slug = form.template_slug.strip()
    target = form.target.strip()
    if not slug:
        return await _render_start_error(request, session, "Template slug is required.")
    if not target:
        return await _render_start_error(request, session, "Target is required.")
    params = _parse_params_or_422(form.params_raw)
    work_ref = form.work_ref.strip() or None
    return StartRunRequest(template_slug=slug, target=target, params=params, work_ref=work_ref)


async def _start_run(
    request: Request,
    session: UISessionContext,
    form: _StartForm,
) -> HTMLResponse:
    """Start a run and return a 204 HX-Redirect, or an inline-alert fragment.

    Validates the form (via :func:`_build_start_request`) then calls
    :meth:`RunbookRunService.start_run` with the session's tenant +
    ``operator_sub`` (the identity the service records as ``assigned_to``).
    On success returns 204 + ``HX-Redirect`` to the T2 driver. The three
    typed start errors map to a re-rendered modal with an ``alert-error``
    (200) so the operator sees a recoverable message (the missing-params
    text names which ``${run.params.X}`` to supply) rather than a 500.
    """
    built = await _build_start_request(request, session, form)
    if isinstance(built, HTMLResponse):
        return built

    structlog.contextvars.bind_contextvars(
        operator_sub=session.operator_sub,
        tenant_id=str(session.tenant_id),
        audit_op_id="runbook.start_run",
        audit_op_class="write",
    )
    try:
        result = await RunbookRunService().start_run(session.tenant_id, session.operator_sub, built)
    except (DeprecatedTemplateError, TemplateNotFoundError, MissingParamsError) as exc:
        log.info(
            "ui_runbook_run_start_rejected",
            tenant_id=str(session.tenant_id),
            operator_sub=session.operator_sub,
            template_slug=built.template_slug,
            reason=type(exc).__name__,
        )
        return await _render_start_error(request, session, str(exc))

    log.info(
        "ui_runbook_run_start",
        tenant_id=str(session.tenant_id),
        operator_sub=session.operator_sub,
        template_slug=built.template_slug,
        run_id=str(result.run_id),
    )
    return HTMLResponse(
        status_code=204,
        headers={"HX-Redirect": f"/ui/runbooks/runs/{result.run_id}"},
    )


def register_runs_routes(router: APIRouter) -> None:
    """Register the run-surface routes onto the runbooks *router*.

    Called by
    :func:`meho_backplane.ui.routes.runbooks.routes.build_runbooks_router`
    **before** the ``/ui/runbooks/{slug}`` catch-all so FastAPI's
    first-match-wins routing does not bind the literal ``runs`` segment as a
    slug parameter. Within this function ``/ui/runbooks/runs/start`` is
    registered **before** ``/ui/runbooks/runs/{run_id}`` would be (the
    ``{run_id}`` driver is T2 #1893, not yet present) so coordination with
    T2 keeps the literal ``start`` segment out of the ``{run_id}`` param
    route's reach. The start POST shares the ``/ui/runbooks/runs`` path with
    the list GET (distinct verbs).
    """

    @router.get("/ui/runbooks/runs", response_class=HTMLResponse)
    async def runbooks_runs(
        request: Request,
        session: UISessionContext = _require_session,
        assignee: str | None = Query(default=None, max_length=_MAX_ASSIGNEE_LENGTH),
        status: Annotated[_RunStateFilter | None, EMPTY_STR_TO_NONE, Query()] = None,
    ) -> HTMLResponse:
        return await _render_runs(request, session, assignee, status)

    @router.get("/ui/runbooks/runs/start", response_class=HTMLResponse)
    async def runbooks_runs_start_modal(
        request: Request,
        session: UISessionContext = _require_session,
    ) -> HTMLResponse:
        return await _render_start_modal(request, session)

    @router.post("/ui/runbooks/runs", response_class=HTMLResponse)
    async def runbooks_runs_start(
        request: Request,
        session: UISessionContext = _require_session,
        template_slug: str = Form(default="", max_length=_MAX_SLUG_LENGTH),
        target: str = Form(default="", max_length=_MAX_TARGET_LENGTH),
        params: str = Form(default="", max_length=_MAX_PARAMS_JSON_LENGTH),
        work_ref: str = Form(default="", max_length=_MAX_WORK_REF_LENGTH),
    ) -> HTMLResponse:
        form = _StartForm(
            template_slug=template_slug,
            target=target,
            params_raw=params,
            work_ref=work_ref,
        )
        return await _start_run(request, session, form)
