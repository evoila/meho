# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``/api/v1/runbooks/templates*`` -- REST surface for the runbook template layer.

G12.2-T3 (#1297) of Initiative #1197. Six routes mounted under
``/api/v1/runbooks/templates*`` that expose
:class:`~meho_backplane.runbooks.service.RunbookTemplateService` (T2
#1296) to operators and ops UIs. The MCP tools (T4 #1298) wrap the same
service directly over the MCP transport; this module is the HTTP front
of the runbook template backplane.

Route inventory
---------------

* ``POST /api/v1/runbooks/templates`` -- create a new draft. Body:
  :class:`~meho_backplane.runbooks.schemas.DraftTemplateRequest`. Returns
  :class:`~meho_backplane.runbooks.schemas.DraftTemplateResponse` with
  HTTP 201. Role: ``tenant_admin``.
* ``GET /api/v1/runbooks/templates`` -- list the latest version of each
  slug for the operator's tenant. Query params: ``status``,
  ``target_kind``, ``limit``. Returns
  :class:`RunbookTemplateListResponse`. Role: ``operator``.
* ``GET /api/v1/runbooks/templates/{slug}`` -- fetch the full body of one
  template. Query param: ``version`` (optional -> latest). Returns
  :class:`~meho_backplane.runbooks.schemas.ShowTemplateResponse`. 404 when
  absent. Role: ``tenant_admin``.
* ``PATCH /api/v1/runbooks/templates/{slug}`` -- edit a draft in place or
  fork a new draft from the latest published version (the service picks
  the path). Body:
  :class:`~meho_backplane.runbooks.schemas.RunbookTemplateBody`. Returns
  :class:`~meho_backplane.runbooks.schemas.EditTemplateResponse` whose
  ``forked_from`` is populated only on the fork path. Role:
  ``tenant_admin``.
* ``POST /api/v1/runbooks/templates/{slug}/publish`` -- promote a draft to
  published. Body: ``{"version": int}``. Role: ``tenant_admin``.
* ``POST /api/v1/runbooks/templates/{slug}/deprecate`` -- retire a
  published version. Body: ``{"version": int}``. Role: ``tenant_admin``.

Tenant scoping
--------------

Every route derives ``tenant_id`` from the JWT-validated
:class:`~meho_backplane.auth.operator.Operator`. No surface accepts a
tenant id from the body or query string -- cross-tenant access is
impossible by construction. A cross-tenant ``show`` / ``publish`` /
``deprecate`` / ``edit`` probe surfaces as 404 (the service's tenant
filter makes the row invisible, so :class:`TemplateNotFoundError` fires
exactly as it would for a genuinely absent slug); the conflation
prevents enumerating another tenant's templates via status-code
differential. Same posture as :mod:`meho_backplane.api.v1.kb`.

Role floor
----------

``list`` accepts ``operator`` (and ``tenant_admin``); the remaining five
routes require ``tenant_admin``. ``show`` is deliberately admin-only --
an operator gets a clean 403 (the opacity floor). The post-completion
operator exception (an operator who owns an in-flight run may read the
template that run is pinned to) lives on the run surface in G12.3, not on
this direct-template-read route.

Typed-exception -> HTTP-status mapping
--------------------------------------

The service raises a small typed-exception vocabulary; each route maps it
to the caller-facing status:

* :class:`~meho_backplane.runbooks.service.TemplateNotFoundError` -> 404
* :class:`~meho_backplane.runbooks.service.TemplateNotDraftError` -> 400
* :class:`~meho_backplane.runbooks.service.TemplateNotPublishedError` -> 400
* :class:`~meho_backplane.runbooks.service.DuplicateDraftError` -> 409
* :class:`~meho_backplane.kb.schemas.InvalidKbSlugError` -> 422

Audit + broadcast contract
--------------------------

Every route binds two contextvars **before** the service call so the
chassis :class:`~meho_backplane.audit.AuditMiddleware` (G2.3-T1) and the
publish-on-write broadcast hook (G6.1-T3) classify the row correctly:

* ``audit_op_id`` -- one of ``runbook.draft_template`` /
  ``runbook.list_templates`` / ``runbook.show_template`` /
  ``runbook.edit_template`` / ``runbook.publish_template`` /
  ``runbook.deprecate_template`` (the canonical operation identifiers
  tracked under :data:`_RUNBOOK_OP_IDS`).
* ``audit_op_class`` -- ``"read"`` for ``list`` / ``show``, ``"write"``
  for ``draft`` / ``edit`` / ``publish`` / ``deprecate``. Bound
  explicitly because
  :func:`~meho_backplane.broadcast.events.classify_op` would not reliably
  suffix-match these op ids -- ``runbook.list_templates`` /
  ``runbook.show_template`` (no ``.list`` / ``.get`` / ``.info`` tail)
  and the write ops (no ``.create`` / ``.update`` / ``.delete`` tail)
  would otherwise fall through to the ``other`` bucket and broadcast
  under the wrong sensitivity class. Same gotcha
  :mod:`meho_backplane.api.v1.kb` documents.

The ``audit_slug`` (all per-slug routes) and ``audit_version`` (the
version-targeted publish / deprecate routes) are bound so the audit_log
payload (and the broadcast ``params``) carries the operation's
coordinates -- the template body / step contents are **never** in the
payload, only the slug + version.

Out of scope
------------

* MCP tools -- G12.2-T4 (#1298) wraps the same service over MCP.
* Run lifecycle routes (``/api/v1/runbooks/runs/*``) -- G12.3 ships them
  in a sibling ``runbook_runs.py``.
* The post-completion operator ``show_template`` exception -- G12.3 (the
  run-state-conditional check, not this template-read route).
* CLI verbs -- G12.5.
"""

from __future__ import annotations

from typing import Final, Literal

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi import status as http_status
from pydantic import BaseModel, ConfigDict

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.auth.rbac import require_role
from meho_backplane.kb.schemas import InvalidKbSlugError, validate_slug
from meho_backplane.runbooks.schemas import (
    DeprecateTemplateRequest,
    DeprecateTemplateResponse,
    DraftTemplateRequest,
    DraftTemplateResponse,
    EditTemplateRequest,
    EditTemplateResponse,
    ListTemplatesFilter,
    PublishTemplateRequest,
    PublishTemplateResponse,
    RunbookTemplateBody,
    ShowTemplateResponse,
    TemplateSummary,
)
from meho_backplane.runbooks.service import (
    DuplicateDraftError,
    RunbookTemplateService,
    TemplateNotDraftError,
    TemplateNotFoundError,
    TemplateNotPublishedError,
)

__all__ = ["router"]

router = APIRouter(prefix="/api/v1/runbooks/templates", tags=["runbooks"])

#: Module-level ``Depends`` closures -- required to satisfy ruff B008
#: (calls in default argument positions are disallowed). Same shape as
#: :mod:`meho_backplane.api.v1.kb`.
_require_operator = Depends(require_role(TenantRole.OPERATOR))
_require_admin = Depends(require_role(TenantRole.TENANT_ADMIN))

#: Canonical operation identifiers bound into ``audit_op_id`` per route.
#: Pinned as module constants so the contract is greppable from tests +
#: G8 dashboards and a typo in a handler surfaces at first call rather
#: than as a silent broadcast under the wrong op_id.
_RUNBOOK_OP_IDS: Final[dict[str, str]] = {
    "draft": "runbook.draft_template",
    "list": "runbook.list_templates",
    "show": "runbook.show_template",
    "edit": "runbook.edit_template",
    "publish": "runbook.publish_template",
    "deprecate": "runbook.deprecate_template",
}


class RunbookTemplateListResponse(BaseModel):
    """Response envelope for ``GET /api/v1/runbooks/templates``.

    Wrapped in ``{"templates": [...]}`` so a future paging / cursor field
    can land non-breakingly -- same shape :mod:`meho_backplane.api.v1.kb`
    adopted for its list response. Per-entry shape is the substrate's
    :class:`~meho_backplane.runbooks.schemas.TemplateSummary`.
    """

    model_config = ConfigDict(frozen=True)

    templates: list[TemplateSummary]


class _VersionBody(BaseModel):
    """Request body for the publish / deprecate routes -- carries ``version`` only.

    The slug is the URL's job (the route's ``{slug}`` path parameter); the
    body carries just the integer version to act on. ``extra="forbid"``
    rejects a stray ``slug`` in the body at 422 rather than silently
    ignoring it -- the URL is the single source of truth for which
    template the operation targets, and a body that smuggled a different
    slug would otherwise be a confused-deputy footgun.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    version: int


@router.post(
    "",
    response_model=DraftTemplateResponse,
    status_code=http_status.HTTP_201_CREATED,
)
async def draft_template(
    request: DraftTemplateRequest,
    operator: Operator = _require_admin,
) -> DraftTemplateResponse:
    """Create a new draft template for ``request.slug``.

    ``tenant_admin`` only -- drafting is a privileged authoring action;
    ``operator`` role gets 403 via :func:`require_role`. The slug already
    having any version (a live draft, or published / deprecated history)
    surfaces as 409 ``DuplicateDraftError`` -- the caller wants the edit
    route (which forks or mutates), not a second v1. A slug that fails
    :data:`~meho_backplane.kb.schemas.SLUG_PATTERN` at the service's
    defense-in-depth revalidation surfaces as 422; the request model
    already enforces the pattern, so this is the belt to that
    suspenders.

    Binds ``audit_op_id="runbook.draft_template"`` + ``audit_op_class=
    "write"`` + ``audit_slug`` before the service call so a handler
    exception still produces an audit row classified under the canonical
    op id.
    """
    structlog.contextvars.bind_contextvars(
        audit_op_id=_RUNBOOK_OP_IDS["draft"],
        audit_op_class="write",
        audit_slug=request.slug,
    )
    service = RunbookTemplateService()
    try:
        return await service.create_draft(
            operator.tenant_id,
            operator.sub,
            request,
        )
    except DuplicateDraftError as exc:
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail=str(exc),
        ) from exc
    except InvalidKbSlugError as exc:
        raise HTTPException(
            status_code=http_status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        ) from exc


@router.get("", response_model=RunbookTemplateListResponse)
async def list_templates(
    status: Literal["draft", "published", "deprecated"] | None = Query(default=None),
    target_kind: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    operator: Operator = _require_operator,
) -> RunbookTemplateListResponse:
    """List the latest version of each template slug for the operator's tenant.

    Tenant-scoped to ``operator.tenant_id`` -- no surface accepts a tenant
    id from the query string. ``read_only`` operators get 403 via
    :func:`require_role` before reaching this handler; ``operator`` and
    ``tenant_admin`` both pass.

    The optional ``status`` / ``target_kind`` filters narrow the rows
    considered before the latest-per-slug projection; ``limit`` is capped
    at 500. ``status`` is typed as the closed ``draft`` / ``published`` /
    ``deprecated`` vocabulary, so an out-of-vocabulary value trips a clean
    422 at FastAPI's query-parameter validation boundary (before the
    handler body runs) rather than a 500 from constructing the
    :class:`~meho_backplane.runbooks.schemas.ListTemplatesFilter`
    internally.

    Binds ``audit_op_id="runbook.list_templates"`` + ``audit_op_class=
    "read"`` before the service call.
    """
    structlog.contextvars.bind_contextvars(
        audit_op_id=_RUNBOOK_OP_IDS["list"],
        audit_op_class="read",
    )
    template_filter = ListTemplatesFilter(status=status, target_kind=target_kind)
    service = RunbookTemplateService()
    summaries = await service.list_templates(
        operator.tenant_id,
        template_filter,
        limit=limit,
    )
    return RunbookTemplateListResponse(templates=summaries)


@router.get("/{slug}", response_model=ShowTemplateResponse)
async def show_template(
    slug: str,
    version: int | None = Query(default=None, ge=1),
    operator: Operator = _require_admin,
) -> ShowTemplateResponse:
    """Return the full body of one template by slug (latest, or ``version``).

    ``tenant_admin`` only -- the opacity floor. An ``operator`` gets a
    clean 403 via :func:`require_role`; the post-completion operator
    exception (read the template a run you own is pinned to) lives on the
    run surface in G12.3, not here.

    Cross-tenant probes surface as 404 (not 403): the service's
    tenant-scoped query makes another tenant's slug invisible, so
    :class:`TemplateNotFoundError` fires exactly as it would for a
    genuinely absent slug. The conflation prevents enumerating another
    tenant's templates via status-code differential -- same posture
    :mod:`meho_backplane.api.v1.kb` uses.

    Binds ``audit_op_id="runbook.show_template"`` + ``audit_op_class=
    "read"`` + ``audit_slug`` before the service call; ``runbook.
    show_template`` matches no read suffix, so the explicit override is
    load-bearing to keep the broadcast classifier from defaulting to
    ``op_class="other"``.
    """
    structlog.contextvars.bind_contextvars(
        audit_op_id=_RUNBOOK_OP_IDS["show"],
        audit_op_class="read",
        audit_slug=slug,
    )
    service = RunbookTemplateService()
    try:
        return await service.show_template(operator.tenant_id, slug, version=version)
    except TemplateNotFoundError as exc:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc


@router.patch("/{slug}", response_model=EditTemplateResponse)
async def edit_template(
    slug: str,
    body: RunbookTemplateBody,
    operator: Operator = _require_admin,
) -> EditTemplateResponse:
    """Edit a draft in place, or fork a new draft from the latest published.

    ``tenant_admin`` only. The service picks the path (the caller does
    not): a live draft is mutated in place (version unchanged,
    ``forked_from=null``); a slug whose only versions are published /
    deprecated forks a new draft at ``max(version)+1`` and reports the
    source version + its ``in_flight_run_count`` in ``forked_from``. A
    slug with no versions at all surfaces as 404
    :class:`TemplateNotFoundError` (nothing to edit or fork). Cross-tenant
    edits collapse to the same 404 by construction.

    The ``{slug}`` from the path is the operation's subject; it is rebound
    onto the request's ``slug`` so a body that omitted it (or carried a
    different one) cannot retarget the write -- the URL is the single
    source of truth for which template is edited. A path slug that fails
    :data:`~meho_backplane.kb.schemas.SLUG_PATTERN` surfaces as 422
    :class:`InvalidKbSlugError` -- checked **before** the audit/broadcast
    contextvars are bound so a rejected slug never fires a write-classified
    audit row (the same posture ``GET /{slug}`` achieves by passing the raw
    slug to the service, which 404s on the unresolved row).

    Binds ``audit_op_id="runbook.edit_template"`` + ``audit_op_class=
    "write"`` + ``audit_slug`` before the service call.
    """
    try:
        validate_slug(slug)
    except InvalidKbSlugError as exc:
        raise HTTPException(
            status_code=http_status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        ) from exc
    structlog.contextvars.bind_contextvars(
        audit_op_id=_RUNBOOK_OP_IDS["edit"],
        audit_op_class="write",
        audit_slug=slug,
    )
    edit_request = EditTemplateRequest(slug=slug, body=body)
    service = RunbookTemplateService()
    try:
        return await service.update_or_fork(
            operator.tenant_id,
            operator.sub,
            edit_request,
        )
    except TemplateNotFoundError as exc:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc


@router.post("/{slug}/publish", response_model=PublishTemplateResponse)
async def publish_template(
    slug: str,
    body: _VersionBody,
    operator: Operator = _require_admin,
) -> PublishTemplateResponse:
    """Promote ``(slug, body.version)`` from draft to published.

    ``tenant_admin`` only. Idempotent at the service layer: re-publishing
    an already-published version is a no-op. A missing
    ``(tenant, slug, version)`` triple surfaces as 404
    :class:`TemplateNotFoundError` (cross-tenant probes collapse here
    too); a version that exists but is deprecated surfaces as 400
    :class:`TemplateNotDraftError` (cannot publish a retired version).

    A path slug that fails :data:`~meho_backplane.kb.schemas.SLUG_PATTERN`
    surfaces as 422 :class:`InvalidKbSlugError` -- checked **before** the
    audit/broadcast contextvars are bound so a rejected slug never fires a
    write-classified audit row (consistent with ``GET /{slug}``, which
    404s an unresolved slug rather than 500ing).

    Binds ``audit_op_id="runbook.publish_template"`` + ``audit_op_class=
    "write"`` + ``audit_slug`` + ``audit_version`` before the service
    call.
    """
    try:
        validate_slug(slug)
    except InvalidKbSlugError as exc:
        raise HTTPException(
            status_code=http_status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        ) from exc
    structlog.contextvars.bind_contextvars(
        audit_op_id=_RUNBOOK_OP_IDS["publish"],
        audit_op_class="write",
        audit_slug=slug,
        audit_version=body.version,
    )
    publish_request = PublishTemplateRequest(slug=slug, version=body.version)
    service = RunbookTemplateService()
    try:
        return await service.publish(operator.tenant_id, publish_request)
    except TemplateNotFoundError as exc:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc
    except TemplateNotDraftError as exc:
        raise HTTPException(
            status_code=http_status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc


@router.post("/{slug}/deprecate", response_model=DeprecateTemplateResponse)
async def deprecate_template(
    slug: str,
    body: _VersionBody,
    operator: Operator = _require_admin,
) -> DeprecateTemplateResponse:
    """Retire ``(slug, body.version)`` from published to deprecated.

    ``tenant_admin`` only. Idempotent at the service layer: re-deprecating
    an already-deprecated version is a no-op. A missing
    ``(tenant, slug, version)`` triple surfaces as 404
    :class:`TemplateNotFoundError` (cross-tenant probes collapse here
    too); a version that exists but is still a draft surfaces as 400
    :class:`TemplateNotPublishedError` (cannot deprecate something never
    published).

    A path slug that fails :data:`~meho_backplane.kb.schemas.SLUG_PATTERN`
    surfaces as 422 :class:`InvalidKbSlugError` -- checked **before** the
    audit/broadcast contextvars are bound so a rejected slug never fires a
    write-classified audit row (consistent with ``GET /{slug}``, which
    404s an unresolved slug rather than 500ing).

    Binds ``audit_op_id="runbook.deprecate_template"`` + ``audit_op_class=
    "write"`` + ``audit_slug`` + ``audit_version`` before the service
    call.
    """
    try:
        validate_slug(slug)
    except InvalidKbSlugError as exc:
        raise HTTPException(
            status_code=http_status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        ) from exc
    structlog.contextvars.bind_contextvars(
        audit_op_id=_RUNBOOK_OP_IDS["deprecate"],
        audit_op_class="write",
        audit_slug=slug,
        audit_version=body.version,
    )
    deprecate_request = DeprecateTemplateRequest(slug=slug, version=body.version)
    service = RunbookTemplateService()
    try:
        return await service.deprecate(operator.tenant_id, deprecate_request)
    except TemplateNotFoundError as exc:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc
    except TemplateNotPublishedError as exc:
        raise HTTPException(
            status_code=http_status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc
