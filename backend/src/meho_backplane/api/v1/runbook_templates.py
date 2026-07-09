# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``/api/v1/runbooks/templates*`` -- REST surface for the runbook template layer.

G12.2-T3 (#1297) of Initiative #1197. Seven routes mounted under
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
  ``target_kind``, ``limit``, ``envelope``. Returns
  :class:`RunbookTemplateListResponse` by default; ``?envelope=v2``
  returns the unified ``{"items": [...], "next_cursor": null}`` shape
  per ``docs/codebase/api-shape-conventions.md`` §2 (G0.22-T6 #1611).
  Role: ``operator``.
* ``GET /api/v1/runbooks/templates/{slug}`` -- fetch the full body of one
  template. Query param: ``version`` (optional -> latest). Returns
  :class:`~meho_backplane.runbooks.schemas.ShowTemplateResponse`. 404 when
  absent for admins; 403 ``opacity_floor`` for operators without a
  matching completed/abandoned run. Role: ``operator`` (with
  run-state-conditional carve-out, see "Role floor" below).
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
* ``POST /api/v1/runbooks/templates/{slug}/discard`` -- delete an
  unpublished draft version (the delete-for-drafts lifecycle leg). Body:
  ``{"version": int}``. Role: ``tenant_admin``.

Tenant scoping
--------------

Every route derives ``tenant_id`` from the JWT-validated
:class:`~meho_backplane.auth.operator.Operator`. No surface accepts a
tenant id from the body or query string -- cross-tenant access is
impossible by construction. A cross-tenant ``show`` / ``publish`` /
``deprecate`` / ``discard`` / ``edit`` probe surfaces as 404 (the service's tenant
filter makes the row invisible, so :class:`TemplateNotFoundError` fires
exactly as it would for a genuinely absent slug); the conflation
prevents enumerating another tenant's templates via status-code
differential. Same posture as :mod:`meho_backplane.api.v1.kb`.

Role floor
----------

``list`` accepts ``operator`` (and ``tenant_admin``); ``draft`` /
``edit`` / ``publish`` / ``deprecate`` / ``discard`` require
``tenant_admin``. ``show``
admits ``operator`` at the role gate but applies a *run-state-conditional*
carve-out in the handler (G12.3-T4 / #1309): a ``tenant_admin`` is a
pass-through, an ``operator`` is granted the read only when
:meth:`~meho_backplane.runbooks.run_service.RunbookRunService.can_show_template_post_completion`
returns ``True`` for the resolved ``(slug, version)`` -- i.e. the operator
has a ``completed`` or ``abandoned`` run against that pinned version. An
operator with no such run, or with only ``in_progress`` runs, still gets a
403 (the opacity floor is real during a live run). The 403 carries
``detail="opacity_floor"`` regardless of whether the slug exists, the
version exists, or just the predicate fired -- the same anti-enumeration
posture the cross-tenant 404 contract uses.

Typed-exception -> HTTP-status mapping
--------------------------------------

The service raises a small typed-exception vocabulary; each route maps it
to the caller-facing status:

* :class:`~meho_backplane.runbooks.service.TemplateNotFoundError` -> 404
* :class:`~meho_backplane.runbooks.service.TemplateNotDraftError` -> 400
* :class:`~meho_backplane.runbooks.service.TemplateNotPublishedError` -> 400
* :class:`~meho_backplane.runbooks.service.DuplicateDraftError` -> 409
* :class:`~meho_backplane.kb.schemas.InvalidKbSlugError` -> 422 (emitted
  in the OpenAPI ``HTTPValidationError`` LIST shape via the shared
  :func:`~meho_backplane.api.v1._errors.http_for` emitter so typed
  clients deserialize it -- #1364; the non-422 mappings raise
  ``HTTPException`` inline with the conformant plain string detail).

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
  for ``draft`` / ``edit`` / ``publish`` / ``deprecate`` / ``discard``. Bound
  explicitly because
  :func:`~meho_backplane.broadcast.events.classify_op` would not reliably
  suffix-match these op ids -- ``runbook.list_templates`` /
  ``runbook.show_template`` (no ``.list`` / ``.get`` / ``.info`` tail)
  and the write ops (no ``.create`` / ``.update`` / ``.delete`` tail)
  would otherwise fall through to the ``other`` bucket and broadcast
  under the wrong sensitivity class. Same gotcha
  :mod:`meho_backplane.api.v1.kb` documents.

The ``audit_slug`` (all per-slug routes) and ``audit_version`` (the
version-targeted publish / deprecate / discard routes) are bound so the audit_log
payload (and the broadcast ``params``) carries the operation's
coordinates -- the template body / step contents are **never** in the
payload, only the slug + version.

Out of scope
------------

* MCP tools -- G12.2-T4 (#1298) wraps the same service over MCP.
* Run lifecycle routes (``/api/v1/runbooks/runs/*``) -- G12.3 ships them
  in a sibling ``runbook_runs.py``.
* CLI verbs -- G12.5.
"""

from __future__ import annotations

from typing import Any, Final, Literal

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi import status as http_status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, ValidationError

from meho_backplane.api.v1._envelope import (
    ENVELOPE_QUERY,
    EnvelopeVersion,
    wrap_v2_envelope,
)
from meho_backplane.api.v1._errors import http_for, register_error
from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.auth.rbac import require_role
from meho_backplane.kb.schemas import InvalidKbSlugError, validate_slug
from meho_backplane.runbooks.hydration_errors import (
    TEMPLATE_BODY_VALIDATION_FAILED,
    build_template_body_validation_detail,
)
from meho_backplane.runbooks.run_service import RunbookRunService
from meho_backplane.runbooks.schemas import (
    DeprecateTemplateRequest,
    DeprecateTemplateResponse,
    DiscardTemplateRequest,
    DiscardTemplateResponse,
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
    "discard": "runbook.discard_template",
}

#: Register the one typed exception this surface maps through the shared
#: ``http_for`` emitter -- a slug failing SLUG_PATTERN on the five write
#: routes (draft / edit / publish / deprecate / discard); ``type_tag`` / ``loc``
#: feed the OpenAPI 422 validation-error LIST shape (see ``_errors``).
register_error(
    InvalidKbSlugError,
    status=http_status.HTTP_422_UNPROCESSABLE_CONTENT,
    type_tag="invalid_kb_slug",
    loc=("path", "slug"),
)


#: OpenAPI 500 declaration for ``GET /{slug}`` (#2239). Hydrating a stored
#: template re-validates the ``steps`` JSONB back through
#: :class:`~meho_backplane.runbooks.schemas.RunbookTemplateBody`; a row that
#: predates a schema tightening (the #2122 non-empty-body constraint) fails
#: that re-validation with a :class:`pydantic.ValidationError`. Declaring the
#: structured envelope here (instead of letting it leak as a bare
#: ``text/plain`` 500 through Starlette's default handler) means the
#: generated CLI / SDK pick it up. Same posture as topology's ``_REFRESH_RESPONSES``
#: (#2092); the shape is built by
#: :func:`~meho_backplane.runbooks.hydration_errors.build_template_body_validation_detail`.
_SHOW_RESPONSES: Final[dict[int | str, dict[str, Any]]] = {
    500: {
        "description": (
            "The stored template body fails the current step schema and "
            "cannot be hydrated -- e.g. an empty step body predating the "
            "v0.20.0 non-empty-body requirement. Apply Alembic migration "
            "0054 to backfill legacy rows; see "
            "docs/codebase/runbook-template-hydration.md."
        ),
        "content": {
            "application/json": {
                "schema": {
                    "type": "object",
                    "properties": {
                        "detail": {
                            "type": "object",
                            "properties": {
                                "error": {
                                    "type": "string",
                                    "enum": [TEMPLATE_BODY_VALIDATION_FAILED],
                                },
                                "slug": {"type": "string"},
                                # OpenAPI 3.0.3 nullable form (the snapshot is 3.0.3,
                                # not 3.1) -- the array-type ``["integer", "null"]``
                                # 3.1 form breaks oapi-codegen.
                                "version": {"type": "integer", "nullable": True},
                                "errors": {
                                    "type": "array",
                                    "items": {
                                        "type": "object",
                                        "properties": {
                                            "type": {"type": "string"},
                                            # ``loc`` mixes str + int segments
                                            # (e.g. ["steps", 0, "manual", "body"]);
                                            # an untyped item schema renders as
                                            # ``interface{}`` in the generated client.
                                            "loc": {"type": "array", "items": {}},
                                            "msg": {"type": "string"},
                                        },
                                        "required": ["type", "loc", "msg"],
                                    },
                                },
                                "message": {"type": "string"},
                            },
                            "required": [
                                "error",
                                "slug",
                                "version",
                                "errors",
                                "message",
                            ],
                        },
                    },
                    "required": ["detail"],
                },
            },
        },
    },
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
    """Request body for the publish / deprecate / discard routes -- carries ``version`` only.

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
        raise http_for(exc) from exc


@router.get("", response_model=RunbookTemplateListResponse)
async def list_templates(
    status: Literal["draft", "published", "deprecated"] | None = Query(default=None),
    target_kind: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    operator: Operator = _require_operator,
    envelope: EnvelopeVersion | None = ENVELOPE_QUERY,
) -> RunbookTemplateListResponse | JSONResponse:
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

    The default response is the v0.8.0 keyed
    :class:`RunbookTemplateListResponse` shape (``{"templates": [...]}``).
    Passing ``?envelope=v2`` returns the unified ``{"items": [...],
    "next_cursor": null}`` shape per
    ``docs/codebase/api-shape-conventions.md`` §2 -- the listing is not
    cursor-paginated (``limit`` truncates), so ``next_cursor`` is always
    ``null`` under the opt-in, matching the sibling unpaged adopters.
    Omitting the param keeps the keyed default so no client breaks
    (G0.22-T6 #1611, joining the shape unified in #1356/#1366).

    The v2 envelope is emitted via a raw :class:`JSONResponse` rather
    than a union return type: that keeps ``response_model`` (and so the
    documented OpenAPI 200 schema, and the typed CLI client generated
    from it) as the named :class:`RunbookTemplateListResponse` while
    still letting the opt-in branch return the unified shape.

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
    if envelope == "v2":
        return JSONResponse(
            wrap_v2_envelope(
                [summary.model_dump(mode="json") for summary in summaries],
                next_cursor=None,
            )
        )
    return RunbookTemplateListResponse(templates=summaries)


@router.get("/{slug}", response_model=ShowTemplateResponse, responses=_SHOW_RESPONSES)
async def show_template(
    slug: str,
    version: int | None = Query(default=None, ge=1),
    operator: Operator = _require_operator,
) -> ShowTemplateResponse:
    """Return the full body of one template by slug (latest, or ``version``).

    Role floor: ``operator``. A ``tenant_admin`` is a pass-through (the
    authoring / review surface). An ``operator`` is granted the read only
    when
    :meth:`~meho_backplane.runbooks.run_service.RunbookRunService.can_show_template_post_completion`
    returns ``True`` for the resolved ``(slug, version)`` -- i.e. the
    operator already has a ``completed`` or ``abandoned`` run against that
    pinned version (post-mortem / learning carve-out, G12.3-T4 #1309).
    Anything else (no run, in-flight run, wrong version, cross-tenant
    probe) collapses to ``HTTP 403`` with ``detail="opacity_floor"``.

    Differential-timing posture for the operator path: when ``version`` is
    omitted, the latest version is resolved **first** (so the authorization
    check uses the same version the response would return). Resolution
    failure on a slug the operator has no run against still surfaces as
    ``opacity_floor`` 403 rather than 404 -- a slug-existence channel an
    operator can't read into would otherwise leak template-presence.
    Anti-enumeration matches the cross-tenant 404 contract for admins:
    same status regardless of which leg of the predicate failed.

    Cross-tenant probes for ``tenant_admin`` callers surface as 404 (the
    service's tenant-scoped query makes the other tenant's row invisible,
    so :class:`TemplateNotFoundError` fires exactly as it would for a
    genuinely absent slug -- same posture
    :mod:`meho_backplane.api.v1.kb` uses).

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
    template_service = RunbookTemplateService()
    if operator.tenant_role == TenantRole.TENANT_ADMIN:
        return await _show_template_admin(template_service, operator, slug, version)
    return await _show_template_operator(
        template_service, RunbookRunService(), operator, slug, version
    )


async def _show_template_admin(
    template_service: RunbookTemplateService,
    operator: Operator,
    slug: str,
    version: int | None,
) -> ShowTemplateResponse:
    """Admin pass-through. Tenant-scoped 404 on a missing row.

    Same shape as the original admin-only handler before G12.3-T4: the
    service-raised :class:`TemplateNotFoundError` maps to ``HTTP 404`` (and
    cross-tenant probes collapse here too -- the service's tenant filter
    makes the other tenant's row invisible).
    """
    try:
        return await template_service.show_template(operator.tenant_id, slug, version=version)
    except TemplateNotFoundError as exc:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc
    except ValidationError as exc:
        raise _template_body_validation_failed(slug, version, exc) from exc


async def _show_template_operator(
    template_service: RunbookTemplateService,
    run_service: RunbookRunService,
    operator: Operator,
    slug: str,
    version: int | None,
) -> ShowTemplateResponse:
    """Operator path with the post-completion carve-out.

    The authorization check needs to know which version it is granting
    before granting it:

    * **version omitted** -- resolve the latest version via the service
      *first*, then check the predicate against that resolved version. A
      slug that does not exist for this tenant collapses to the same
      ``opacity_floor`` 403 the predicate-failure path emits -- operators
      cannot discover slugs they have never run against via status-code
      differential.
    * **version supplied** -- check the predicate up front, then fetch. A
      predicate-true read against a missing version is a corner case (the
      operator would have had to complete a run against it for the
      predicate to be true), but the service's
      :class:`TemplateNotFoundError` still surfaces as 403 for the same
      anti-enumeration reason.
    """
    if version is None:
        try:
            template = await template_service.show_template(operator.tenant_id, slug, version=None)
        except TemplateNotFoundError as exc:
            raise _opacity_floor() from exc
        except ValidationError as exc:
            # A corrupt stored body surfaces as the structured 500 even on the
            # pre-authorization latest-resolve leg: the row genuinely exists
            # and is genuinely broken, so a server-side data fault is honest.
            # This is not a clean enumeration oracle (normal templates return
            # the opacity_floor 403; only the rare corrupt row 500s) and it
            # matches the pre-#2239 bare-500 behaviour on this path -- see
            # docs/codebase/runbook-template-hydration.md.
            raise _template_body_validation_failed(slug, version, exc) from exc
        if await run_service.can_show_template_post_completion(
            operator.tenant_id, operator.sub, slug, template.version
        ):
            return template
        raise _opacity_floor()

    if not await run_service.can_show_template_post_completion(
        operator.tenant_id, operator.sub, slug, version
    ):
        raise _opacity_floor()
    try:
        return await template_service.show_template(operator.tenant_id, slug, version=version)
    except TemplateNotFoundError as exc:
        raise _opacity_floor() from exc
    except ValidationError as exc:
        raise _template_body_validation_failed(slug, version, exc) from exc


def _opacity_floor() -> HTTPException:
    """Construct the canonical opacity-floor 403.

    Centralised so every operator-path denial uses the same status + detail
    -- anti-enumeration relies on the response being identical regardless
    of which leg of the predicate failed (slug missing, version missing,
    no completed run).
    """
    return HTTPException(
        status_code=http_status.HTTP_403_FORBIDDEN,
        detail="opacity_floor",
    )


def _template_body_validation_failed(
    slug: str,
    version: int | None,
    exc: ValidationError,
) -> HTTPException:
    """Map a stored-template hydration ``ValidationError`` to a structured 500.

    The stored ``steps`` JSONB fails re-validation back through
    :class:`~meho_backplane.runbooks.schemas.RunbookTemplateBody` (the
    documented fail-closed read posture) -- typically a legacy empty step
    body that predates the #2122 non-empty-body constraint. Rather than
    leak a bare ``text/plain`` 500 through Starlette's default handler,
    surface the shared structured envelope
    (:func:`~meho_backplane.runbooks.hydration_errors.build_template_body_validation_detail`,
    the same builder the MCP tool uses so the two transports can't drift)
    inside ``HTTPException.detail``. The full validation error is also
    logged for operator triage.
    """
    detail = build_template_body_validation_detail(slug=slug, version=version, exc=exc)
    structlog.get_logger().warning(
        "runbook_template_body_validation_failed",
        error=TEMPLATE_BODY_VALIDATION_FAILED,
        slug=slug,
        version=version,
        errors=detail["errors"],
    )
    return HTTPException(
        status_code=http_status.HTTP_500_INTERNAL_SERVER_ERROR,
        detail=detail,
    )


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
        raise http_for(exc) from exc
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
        raise http_for(exc) from exc
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
        raise http_for(exc) from exc
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


@router.post("/{slug}/discard", response_model=DiscardTemplateResponse)
async def discard_template(
    slug: str,
    body: _VersionBody,
    operator: Operator = _require_admin,
) -> DiscardTemplateResponse:
    """Delete an **unpublished draft** ``(slug, body.version)``.

    ``tenant_admin`` only -- the delete-for-drafts leg of the template
    lifecycle. A draft version is removed outright (subsequent ``GET
    /{slug}`` / ``list`` no longer surface it). A missing
    ``(tenant, slug, version)`` triple surfaces as 404
    :class:`TemplateNotFoundError` (cross-tenant probes collapse here too,
    and a re-discard of an already-removed draft is a 404 rather than a
    silent success). A version that exists but is ``published`` /
    ``deprecated`` surfaces as 400 :class:`TemplateNotDraftError` -- those
    are retired via ``/deprecate`` (preserving lifecycle history), never
    discarded.

    A path slug that fails :data:`~meho_backplane.kb.schemas.SLUG_PATTERN`
    surfaces as 422 :class:`InvalidKbSlugError` -- checked **before** the
    audit/broadcast contextvars are bound so a rejected slug never fires a
    write-classified audit row (consistent with the sibling write routes).

    Binds ``audit_op_id="runbook.discard_template"`` + ``audit_op_class=
    "write"`` + ``audit_slug`` + ``audit_version`` before the service call.
    """
    try:
        validate_slug(slug)
    except InvalidKbSlugError as exc:
        raise http_for(exc) from exc
    structlog.contextvars.bind_contextvars(
        audit_op_id=_RUNBOOK_OP_IDS["discard"],
        audit_op_class="write",
        audit_slug=slug,
        audit_version=body.version,
    )
    discard_request = DiscardTemplateRequest(slug=slug, version=body.version)
    service = RunbookTemplateService()
    try:
        return await service.discard(operator.tenant_id, discard_request)
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
