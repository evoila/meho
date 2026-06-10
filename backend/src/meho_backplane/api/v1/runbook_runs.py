# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``/api/v1/runbooks/runs*`` -- REST surface for the runbook *run* lifecycle.

G12.3-T5 (#1311) of Initiative #1198. Five routes mounted under
``/api/v1/runbooks/runs*`` that wrap
:class:`~meho_backplane.runbooks.run_service.RunbookRunService` (T3
#1308). The MCP tools (T6 #1313) wrap the same service directly over
the MCP transport; this module is the HTTP front of the runbook *run*
backplane. Sibling to :mod:`meho_backplane.api.v1.runbook_templates`
(T3 #1297, template lifecycle); both files live in the same flat
``api/v1/`` directory and mount independent ``APIRouter`` prefixes
under ``/api/v1/runbooks/*``.

Route inventory
---------------

* ``POST /api/v1/runbooks/runs`` -- begin a new run on the latest
  non-deprecated published template. Body:
  :class:`~meho_backplane.runbooks.runs_schemas.StartRunRequest`. Returns
  :class:`~meho_backplane.runbooks.runs_schemas.CurrentStepResponse` (one
  step body, the opacity-by-construction shape) with HTTP 201. Role:
  ``operator``; the caller is auto-assigned as the run's assignee.
* ``POST /api/v1/runbooks/runs/{run_id}/next`` -- advance the run one
  step, gated by the substrate's verify oracle. Body:
  :class:`~meho_backplane.runbooks.runs_schemas.NextStepRequest`. Returns
  :data:`~meho_backplane.runbooks.runs_schemas.NextStepResponse`
  (discriminated on ``kind``: a :class:`CurrentStepResponse` carrying
  the next step's body, or a :class:`RunCompletedResponse` marker).
  Role: ``operator``; service rejects callers other than the assignee
  with ``NotRunAssigneeError`` regardless of role (per Initiative
  #1198's single-assignee discipline).
* ``POST /api/v1/runbooks/runs/{run_id}/abort`` -- terminate a run
  mid-flight with a required reason. Body:
  :class:`~meho_backplane.runbooks.runs_schemas.AbortRunRequest`.
  Returns :class:`AbortRunResponse`. Role: ``operator`` at the gate;
  the service permits caller ∈ ``{run.assigned_to, any TENANT_ADMIN}``
  via the ``caller_is_admin`` flag the handler passes through.
* ``POST /api/v1/runbooks/runs/{run_id}/reassign`` -- transfer
  ownership of a run to a new assignee. Body:
  :class:`~meho_backplane.runbooks.runs_schemas.ReassignRunRequest`.
  Returns :class:`ReassignRunResponse`. Role: ``tenant_admin`` -- the
  load-bearing escalation knob (per #1198, ``runbook_reassign`` is the
  *only* way for a senior to take over a junior's stuck run).
* ``GET /api/v1/runbooks/runs`` -- list runs the caller can see. Query
  params: ``assignee``, ``status``, ``template_slug``, ``limit``,
  ``envelope``. Returns a :class:`RunbookListRunsResponse` envelope
  wrapping ``list[RunSummary]`` by default; ``?envelope=v2`` returns
  the unified ``{"items": [...], "next_cursor": null}`` shape per
  ``docs/codebase/api-shape-conventions.md`` §2 (G0.22-T6 #1611).
  Role: ``operator`` at the gate; the service branches on the
  handler-computed ``caller_is_admin`` flag so an ``OPERATOR`` only
  ever sees their own runs (filter ``assignee`` is ignored) while a
  ``TENANT_ADMIN`` sees all tenant runs (filter honoured as-is).

Tenant scoping
--------------

Every route derives ``tenant_id`` from the JWT-validated
:class:`~meho_backplane.auth.operator.Operator`. No surface accepts a
tenant id from the body, query string, or path -- cross-tenant access
is impossible by construction. A cross-tenant ``next`` / ``abort`` /
``reassign`` probe on someone else's ``run_id`` surfaces as 404 (the
service's tenant filter makes the row invisible, so
:class:`RunNotFoundError` fires exactly as it would for a genuinely
absent id); the conflation prevents enumerating another tenant's runs
via status-code differential. Same posture as
:mod:`meho_backplane.api.v1.runbook_templates`.

Role floor
----------

``start`` / ``next`` / ``abort`` / ``list`` accept ``operator`` (and
``tenant_admin``); ``reassign`` requires ``tenant_admin``. The
single-assignee invariant is enforced at the *service* layer (not
the role gate) for ``next``: a ``tenant_admin`` who is not the run's
assignee still gets a 403, because the right way for a senior to
take over is :func:`reassign_run`, not a role-based bypass. The
``abort`` route is the one place where the route layer explicitly
informs the service of the caller's role -- it passes
``caller_is_admin=True`` so the service widens its allowance from
"only the assignee" to "the assignee or any TENANT_ADMIN" (per #1198,
admins can mop up abandoned runs without reassigning to themselves
first).

Typed-exception -> HTTP-status mapping
--------------------------------------

The service (T3) and the engine (T2) raise a typed-exception
vocabulary; each route maps it to the caller-facing status:

* :class:`~meho_backplane.runbooks.run_service.RunNotFoundError` -> 404
* :class:`~meho_backplane.runbooks.run_service.NotRunAssigneeError` -> 403
* :class:`~meho_backplane.runbooks.run_service.RunAlreadyTerminalError` -> 400
* :class:`~meho_backplane.runbooks.run_service.PreviousStepFailedError` -> 400
* :class:`~meho_backplane.runbooks.engine.PreviousStepNotVerifiedError` -> 400
* :class:`~meho_backplane.runbooks.run_service.MissingParamsError` -> 422
* :class:`~meho_backplane.runbooks.engine.VerifyResponseRequiredError` -> 422
* :class:`~meho_backplane.runbooks.engine.VerifyResponseMismatchError` -> 422
* :class:`~meho_backplane.runbooks.run_service.DeprecatedTemplateError` -> 400
* :class:`~meho_backplane.runbooks.service.TemplateNotFoundError` -> 404

Why some 400 vs 422 distinctions:

* ``PreviousStepFailedError`` / ``RunAlreadyTerminalError`` /
  ``PreviousStepNotVerifiedError`` / ``DeprecatedTemplateError`` -> 400
  (state-machine refusal -- request was syntactically valid but
  conflicts with the run's current state).
* ``MissingParamsError`` / ``VerifyResponseRequiredError`` /
  ``VerifyResponseMismatchError`` -> 422 (request body / params shape
  doesn't satisfy what the engine needs -- Pydantic-style validation
  failure surface).

The mapping is registered with the shared
:func:`~meho_backplane.api.v1._errors.http_for` emitter at module
import (see the ``register_error`` block below). The 422 entries emit
the Pydantic validation-error LIST shape
(``{"detail": [{"loc": [...], "msg": ..., "type": ...}]}``) so the body
matches the ``HTTPValidationError`` schema FastAPI declares for the
route's 422 response -- a typed client generated from the OpenAPI spec
deserializes it cleanly and keys on ``detail[0].type``
(``missing_params`` / ``verify_response_required`` /
``verify_response_mismatch``). The non-422 entries keep the plain
``{"detail": "<string>"}`` body (conformant -- the OpenAPI schemas for
400 / 403 / 404 don't declare a structured shape). See
:mod:`meho_backplane.api.v1._errors` and
``docs/codebase/error-message-shape.md`` for the convention.

Audit + broadcast contract
--------------------------

Every route binds two contextvars **before** the service call so the
chassis :class:`~meho_backplane.audit.AuditMiddleware` (G2.3-T1) and
the publish-on-write broadcast hook (G6.1-T3) classify the row
correctly:

* ``audit_op_id`` -- one of ``runbook.start_run`` /
  ``runbook.next_step`` / ``runbook.abort_run`` /
  ``runbook.reassign_run`` / ``runbook.list_runs`` (the canonical
  operation identifiers tracked under :data:`_RUNBOOK_RUN_OP_IDS`).
* ``audit_op_class`` -- ``"read"`` for ``list_runs``, ``"write"`` for
  the four state-changing routes. Bound explicitly because
  :func:`~meho_backplane.broadcast.events.classify_op` would not
  reliably suffix-match these op ids (no ``.list`` / ``.get`` /
  ``.create`` / ``.update`` tail) and they would otherwise fall
  through to the ``other`` bucket and broadcast under the wrong
  sensitivity class. Same gotcha
  :mod:`meho_backplane.api.v1.kb` and
  :mod:`meho_backplane.api.v1.runbook_templates` document.

For the ``next`` route there is a *second* audit row when the verify
predicate is an ``operation_call``: the route binds
``audit_op_id="runbook.next_step"`` on the outer envelope row, and the
service binds ``run_id_var`` + ``step_id_var`` (per G12.1-T2 #1294)
around the dispatched verify call. That inner audit row carries its
own ``audit_op_id`` (the verify call's op id) but is correlated to
this run via ``run_id`` + ``step_id``. Two audit rows per
``runbook_next`` invocation on an ``operation_call`` verify; one row
on a ``confirm`` verify.

Out of scope
------------

* MCP tools -- G12.3-T6 (#1313) wraps the same service over MCP.
* CLI verbs -- G12.5.
* Architecture doc -- G12.3-T7 (#1314).
* Template-side routes (``/api/v1/runbooks/templates/*``) -- live in
  :mod:`meho_backplane.api.v1.runbook_templates` (T3 #1297).
"""

from __future__ import annotations

import uuid
from typing import Final, Literal

import structlog
from fastapi import APIRouter, Depends, Query
from fastapi import status as http_status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict

from meho_backplane.api.v1._envelope import (
    ENVELOPE_QUERY,
    EnvelopeVersion,
    wrap_v2_envelope,
)
from meho_backplane.api.v1._errors import http_for, register_error
from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.auth.rbac import require_role
from meho_backplane.runbooks.engine import (
    PreviousStepNotVerifiedError,
    VerifyResponseMismatchError,
    VerifyResponseRequiredError,
)
from meho_backplane.runbooks.run_service import (
    DeprecatedTemplateError,
    MissingParamsError,
    NotRunAssigneeError,
    PreviousStepFailedError,
    RunAlreadyTerminalError,
    RunbookRunService,
    RunNotFoundError,
)
from meho_backplane.runbooks.runs_schemas import (
    AbortRunRequest,
    AbortRunResponse,
    ListRunsFilter,
    NextStepRequest,
    NextStepResponse,
    ReassignRunRequest,
    ReassignRunResponse,
    RunSummary,
    StartRunRequest,
)
from meho_backplane.runbooks.service import TemplateNotFoundError

__all__ = ["router"]

router = APIRouter(prefix="/api/v1/runbooks/runs", tags=["runbooks"])

#: Module-level ``Depends`` closures -- required to satisfy ruff B008
#: (calls in default argument positions are disallowed). Same shape
#: as :mod:`meho_backplane.api.v1.runbook_templates`.
_require_operator = Depends(require_role(TenantRole.OPERATOR))
_require_admin = Depends(require_role(TenantRole.TENANT_ADMIN))

#: Canonical operation identifiers bound into ``audit_op_id`` per
#: route. Pinned as module constants so the contract is greppable
#: from tests + G8 dashboards and a typo in a handler surfaces at
#: first call rather than as a silent broadcast under the wrong
#: op_id.
_RUNBOOK_RUN_OP_IDS: Final[dict[str, str]] = {
    "start": "runbook.start_run",
    "next": "runbook.next_step",
    "abort": "runbook.abort_run",
    "reassign": "runbook.reassign_run",
    "list": "runbook.list_runs",
}

#: Typed-exception -> HTTP mapping, registered with the shared
#: :func:`~meho_backplane.api.v1._errors.http_for` emitter at module
#: import. Centralised so each handler maps a caught exception with one
#: ``raise http_for(exc) from exc`` line rather than open-coding the
#: ``HTTPException`` body N times. The 422 entries carry a ``type_tag``
#: discriminator and a ``loc`` path so :func:`http_for` emits the
#: Pydantic validation-error LIST shape the OpenAPI 422 schema declares
#: (a typed client keys on ``detail[0].type``); the non-422 entries
#: need neither (their ``{"detail": "<string>"}`` body is already
#: schema-conformant). None of these exception types subclass each
#: other (the engine and service vocabularies inherit only from stdlib
#: ``ValueError`` / ``LookupError`` / ``PermissionError``), so the
#: registry's concrete-type lookup is unambiguous.
register_error(RunNotFoundError, status=http_status.HTTP_404_NOT_FOUND)
register_error(TemplateNotFoundError, status=http_status.HTTP_404_NOT_FOUND)
register_error(NotRunAssigneeError, status=http_status.HTTP_403_FORBIDDEN)
register_error(DeprecatedTemplateError, status=http_status.HTTP_400_BAD_REQUEST)
register_error(RunAlreadyTerminalError, status=http_status.HTTP_400_BAD_REQUEST)
register_error(PreviousStepFailedError, status=http_status.HTTP_400_BAD_REQUEST)
register_error(PreviousStepNotVerifiedError, status=http_status.HTTP_400_BAD_REQUEST)
register_error(
    MissingParamsError,
    status=http_status.HTTP_422_UNPROCESSABLE_CONTENT,
    type_tag="missing_params",
    loc=("body", "params"),
)
register_error(
    VerifyResponseRequiredError,
    status=http_status.HTTP_422_UNPROCESSABLE_CONTENT,
    type_tag="verify_response_required",
    loc=("body", "verify_response"),
)
register_error(
    VerifyResponseMismatchError,
    status=http_status.HTTP_422_UNPROCESSABLE_CONTENT,
    type_tag="verify_response_mismatch",
    loc=("body", "verify_response"),
)


class RunbookListRunsResponse(BaseModel):
    """Response envelope for ``GET /api/v1/runbooks/runs``.

    Wrapped in ``{"runs": [...]}`` so a future paging / cursor field
    can land non-breakingly -- same shape
    :mod:`meho_backplane.api.v1.runbook_templates` adopted for its
    list response. Per-entry shape is the substrate's
    :class:`~meho_backplane.runbooks.runs_schemas.RunSummary` (no
    step body content, only the run-level coordinates).
    """

    model_config = ConfigDict(frozen=True)

    runs: list[RunSummary]


def _runbook_run_service() -> RunbookRunService:
    """Construct a fresh :class:`RunbookRunService` per request.

    The service is stateless and method-scoped (it opens its own
    :class:`AsyncSession` per call), so a per-request instance is
    cheap and keeps the tests easy to patch (the ``Depends`` overrides
    just replace the factory). Exposed as a module-level closure
    (:data:`_RUNBOOK_RUN_SERVICE`) so it can be re-bound through
    FastAPI's ``app.dependency_overrides`` in higher-level tests.
    """
    return RunbookRunService()


_RUNBOOK_RUN_SERVICE = Depends(_runbook_run_service)


@router.post(
    "",
    status_code=http_status.HTTP_201_CREATED,
)
async def start_run(
    request: StartRunRequest,
    operator: Operator = _require_operator,
    service: RunbookRunService = _RUNBOOK_RUN_SERVICE,
) -> NextStepResponse:
    """Begin a new run on the latest non-deprecated published template version.

    Role floor: ``operator``. The caller is auto-assigned as the run's
    assignee (``run.assigned_to = operator.sub``); the service refuses
    to start a new run for any other principal. Returns
    :class:`CurrentStepResponse` (the opacity-by-construction shape
    carrying *only* the first step's body, with ``${run.target}`` /
    ``${run.params.X}`` substitutions resolved); never a
    :class:`RunCompletedResponse` (a fresh run is not terminal).

    Typed-error mapping:

    * :class:`DeprecatedTemplateError` -> 400 (every version of the
      slug is deprecated; the only forward path is publishing a new
      version).
    * :class:`TemplateNotFoundError` -> 404 (no published or
      deprecated row exists for this slug in the operator's tenant;
      cross-tenant probes collapse here too).
    * :class:`MissingParamsError` -> 422 (the template references a
      ``${run.params.X}`` the request body did not supply; caught at
      start time so the run row never lands).

    Binds ``audit_op_id="runbook.start_run"`` + ``audit_op_class=
    "write"`` before the service call so a handler exception still
    produces an audit row classified under the canonical op id.

    The response type is declared as the broader
    :data:`NextStepResponse` union (rather than
    :class:`CurrentStepResponse` directly) so the OpenAPI schema
    documents the *same* response surface as ``next`` -- callers can
    parse both endpoints' bodies through one discriminated-union
    decoder. The service's start path always returns the
    ``current_step`` variant; never the ``completed`` one.
    """
    structlog.contextvars.bind_contextvars(
        audit_op_id=_RUNBOOK_RUN_OP_IDS["start"],
        audit_op_class="write",
    )
    try:
        return await service.start_run(operator.tenant_id, operator.sub, request)
    except (DeprecatedTemplateError, TemplateNotFoundError, MissingParamsError) as exc:
        raise http_for(exc) from exc


@router.post("/{run_id}/next")
async def advance_run(
    run_id: uuid.UUID,
    request: NextStepRequest,
    operator: Operator = _require_operator,
    service: RunbookRunService = _RUNBOOK_RUN_SERVICE,
) -> NextStepResponse:
    """Advance the run one step, gated by the verify predicate.

    Role floor: ``operator``. The service enforces the single-assignee
    invariant: a caller other than ``run.assigned_to`` -- including a
    ``tenant_admin`` -- gets 403 :class:`NotRunAssigneeError`. The
    right way for a senior to take over a junior's run is
    :func:`reassign_run`, not a role-based bypass. The substrate is
    the verify oracle; the caller's ``last_verified`` claim is
    informational only and never trusted as a gate.

    Typed-error mapping:

    * :class:`RunNotFoundError` -> 404 (no run with that id in the
      caller's tenant; cross-tenant probes collapse here).
    * :class:`NotRunAssigneeError` -> 403 (caller is not the
      assignee; the right path is :func:`reassign_run`).
    * :class:`RunAlreadyTerminalError` -> 400 (run is already
      ``completed`` / ``abandoned``).
    * :class:`PreviousStepFailedError` -> 400 (current step is in
      ``failed`` state; the only forward path is :func:`abort_run`).
    * :class:`PreviousStepNotVerifiedError` -> 400 (engine-side gate:
      previous step never transitioned to ``verified``).
    * :class:`VerifyResponseRequiredError` -> 422 (the step needs a
      verify response and the request omitted it).
    * :class:`VerifyResponseMismatchError` -> 422 (the response's
      shape did not match the step's verify type).

    Returns :data:`NextStepResponse` -- discriminated on ``kind``:
    a :class:`CurrentStepResponse` carrying the next step's body, or
    a :class:`RunCompletedResponse` marker when the previous step was
    the last one.

    Binds ``audit_op_id="runbook.next_step"`` + ``audit_op_class=
    "write"`` before the service call. When the step's verify is an
    ``operation_call``, the service binds ``run_id_var`` +
    ``step_id_var`` (G12.1-T2 #1294) around the dispatched call, so
    the inner audit row carries the run/step correlation columns
    populated -- one audit row for the route envelope, one for the
    dispatched verify call.
    """
    structlog.contextvars.bind_contextvars(
        audit_op_id=_RUNBOOK_RUN_OP_IDS["next"],
        audit_op_class="write",
    )
    try:
        return await service.next_step(operator.tenant_id, operator.sub, run_id, request)
    except (
        RunNotFoundError,
        NotRunAssigneeError,
        RunAlreadyTerminalError,
        PreviousStepFailedError,
        PreviousStepNotVerifiedError,
        VerifyResponseRequiredError,
        VerifyResponseMismatchError,
    ) as exc:
        raise http_for(exc) from exc


@router.post("/{run_id}/abort", response_model=AbortRunResponse)
async def abort_run(
    run_id: uuid.UUID,
    request: AbortRunRequest,
    operator: Operator = _require_operator,
    service: RunbookRunService = _RUNBOOK_RUN_SERVICE,
) -> AbortRunResponse:
    """Mark the run ``abandoned`` and write an audit row with the reason.

    Role floor: ``operator`` at the gate. The service then permits
    caller ∈ ``{run.assigned_to, any TENANT_ADMIN}``; the handler
    passes ``caller_is_admin=True`` when the operator's
    ``tenant_role`` is ``TENANT_ADMIN`` so the service widens its
    allowance. Non-assignee non-admin callers get
    :class:`NotRunAssigneeError` from the service -> 403.

    Typed-error mapping:

    * :class:`RunNotFoundError` -> 404.
    * :class:`NotRunAssigneeError` -> 403 (caller is neither the
      assignee nor a tenant admin).
    * :class:`RunAlreadyTerminalError` -> 400 (re-aborting a
      terminal run would invalidate the audit story).

    Binds ``audit_op_id="runbook.abort_run"`` + ``audit_op_class=
    "write"`` before the service call. The service also writes a
    *direct* audit row (not through the dispatcher) with
    ``method="DISPATCH"`` + ``path="runbook.abort"`` carrying the
    abort ``reason`` in ``raw_payload`` -- the route's envelope row
    and the service's direct row are both present; queries can use
    either to find the abort.
    """
    structlog.contextvars.bind_contextvars(
        audit_op_id=_RUNBOOK_RUN_OP_IDS["abort"],
        audit_op_class="write",
    )
    caller_is_admin = operator.tenant_role == TenantRole.TENANT_ADMIN
    try:
        return await service.abort_run(
            operator.tenant_id,
            operator.sub,
            run_id,
            request,
            caller_is_admin=caller_is_admin,
        )
    except (RunNotFoundError, NotRunAssigneeError, RunAlreadyTerminalError) as exc:
        raise http_for(exc) from exc


@router.post("/{run_id}/reassign", response_model=ReassignRunResponse)
async def reassign_run(
    run_id: uuid.UUID,
    request: ReassignRunRequest,
    operator: Operator = _require_admin,
    service: RunbookRunService = _RUNBOOK_RUN_SERVICE,
) -> ReassignRunResponse:
    """Transfer ownership of the run to a new assignee.

    Role floor: ``tenant_admin``. The route gate is the *only* RBAC
    enforcement on this surface -- the service does no role check
    (it is willing to flip ``assigned_to`` for any caller, by design:
    the senior taking over is the documented escalation knob from
    Initiative #1198; an operator hitting this route gets 403 at the
    gate before the service is touched).

    Typed-error mapping:

    * :class:`RunNotFoundError` -> 404 (no run with that id in the
      admin's tenant; cross-tenant probes collapse here).
    * :class:`RunAlreadyTerminalError` -> 400 (a terminal run has no
      meaningful next-assignee).

    Binds ``audit_op_id="runbook.reassign_run"`` + ``audit_op_class=
    "write"`` before the service call so the ownership transfer is
    classified as a write in the audit log.
    """
    structlog.contextvars.bind_contextvars(
        audit_op_id=_RUNBOOK_RUN_OP_IDS["reassign"],
        audit_op_class="write",
    )
    try:
        return await service.reassign_run(operator.tenant_id, operator.sub, run_id, request)
    except (RunNotFoundError, RunAlreadyTerminalError) as exc:
        raise http_for(exc) from exc


@router.get("", response_model=RunbookListRunsResponse)
async def list_runs(
    assignee: str | None = Query(default=None),
    status: Literal["in_progress", "completed", "abandoned"] | None = Query(default=None),
    template_slug: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    operator: Operator = _require_operator,
    service: RunbookRunService = _RUNBOOK_RUN_SERVICE,
    envelope: EnvelopeVersion | None = ENVELOPE_QUERY,
) -> RunbookListRunsResponse | JSONResponse:
    """List runs for the operator's tenant, scoped by role.

    Role floor: ``operator``. The handler computes
    ``caller_is_admin`` from ``operator.tenant_role`` and passes it
    to the service:

    * ``caller_is_admin=False`` (operator path) -- the service forces
      ``assignee=operator.sub`` regardless of what the query string
      says, so an ``OPERATOR`` only ever sees their own runs even if
      they try to filter to another assignee. The filter is a
      *narrow* surface for their own view, never an *escape* into
      another operator's.
    * ``caller_is_admin=True`` (admin path) -- the filter is honoured
      as-is, so a ``TENANT_ADMIN`` can pass ``?assignee=<other-sub>``
      to inspect a junior's runs.

    Tenant-scoped to ``operator.tenant_id`` -- no surface accepts a
    tenant id from the query string. ``status`` is typed as the
    closed ``in_progress`` / ``completed`` / ``abandoned`` vocabulary,
    so an out-of-vocabulary value trips a clean 422 at FastAPI's
    query-parameter validation boundary (before the handler body
    runs) rather than a 500 from constructing the
    :class:`~meho_backplane.runbooks.runs_schemas.ListRunsFilter`
    internally.

    The default response is the v0.8.0 keyed
    :class:`RunbookListRunsResponse` shape (``{"runs": [...]}``).
    Passing ``?envelope=v2`` returns the unified ``{"items": [...],
    "next_cursor": null}`` shape per
    ``docs/codebase/api-shape-conventions.md`` §2 -- the listing is
    not cursor-paginated (``limit`` truncates), so ``next_cursor`` is
    always ``null`` under the opt-in, matching the sibling unpaged
    adopters. Omitting the param keeps the keyed default so no client
    breaks (G0.22-T6 #1611, joining the shape unified in #1356/#1366).

    The v2 envelope is emitted via a raw :class:`JSONResponse` rather
    than a union return type: that keeps ``response_model`` (and so
    the documented OpenAPI 200 schema, and the typed CLI client
    generated from it) as the named :class:`RunbookListRunsResponse`
    while still letting the opt-in branch return the unified shape.

    Binds ``audit_op_id="runbook.list_runs"`` + ``audit_op_class=
    "read"`` before the service call. ``runbook.list_runs`` matches
    no ``.list`` / ``.get`` suffix the broadcast classifier
    recognises, so the explicit override is load-bearing to keep
    classification from defaulting to ``op_class="other"`` (same
    gotcha :mod:`meho_backplane.api.v1.runbook_templates`'s
    ``list_templates`` documents).
    """
    structlog.contextvars.bind_contextvars(
        audit_op_id=_RUNBOOK_RUN_OP_IDS["list"],
        audit_op_class="read",
    )
    caller_is_admin = operator.tenant_role == TenantRole.TENANT_ADMIN
    filter_ = ListRunsFilter(assignee=assignee, status=status, template_slug=template_slug)
    summaries = await service.list_runs(
        tenant_id=operator.tenant_id,
        caller_sub=operator.sub,
        caller_is_admin=caller_is_admin,
        filter_=filter_,
        limit=limit,
    )
    if envelope == "v2":
        return JSONResponse(
            wrap_v2_envelope(
                [summary.model_dump(mode="json") for summary in summaries],
                next_cursor=None,
            )
        )
    return RunbookListRunsResponse(runs=summaries)
