# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``/api/v1/checks/*`` — the gateway assignment + result-ingest REST surface.

Initiative #2415 (#2499). Three routes mounted under ``/api/v1/checks/`` —
inside the runner route cage
(:data:`~meho_backplane.middleware.RUNNER_ALLOWED_PATH_PREFIXES`), so a
``principal_kind=runner`` token may reach them (and only them + the
long-poll gateway plane):

* ``PUT /api/v1/checks/assignment/{runner}`` — **operator**-authenticated
  (:func:`~meho_backplane.auth.rbac.require_role` at ``OPERATOR``, the same
  tier as scheduled-trigger authoring). Full-document replace of a runner's
  assignment. Each item is validated at create time (target resolves, op
  resolves to an enabled ``safety_level == 'safe'`` descriptor); a failing
  item is a structured 422 and nothing is stored.

* ``GET /api/v1/checks/assignment?runner=…[&known_version=…]`` —
  **runner**-authenticated (:func:`~meho_backplane.auth.runner_guard.require_runner`
  + :func:`~meho_backplane.auth.runner_guard.assert_runner_scope`, so a
  runner fetches only its own assignment). Returns a
  :class:`~meho_backplane.runner.wire.RunnerAssignment` with per-item
  resolved target descriptors + ``handler_ref`` / ``safety_level`` /
  principal context, materialised from live rows. ``known_version`` echoing
  the current digest yields ``304 Not Modified`` with an empty body.

* ``POST /api/v1/checks/results`` — **runner**-authenticated
  (own-results-only). Idempotent batch ingest keyed on
  ``(tenant, runner, result_uid)``; ``received_at`` is central-stamped.

All authorization stays central (Initiative #2415 principle): the runner
never self-authorizes. The wire shapes are the ones
:mod:`meho_backplane.runner.wire` defines — one schema on both ends by
construction.
"""

from __future__ import annotations

from typing import Annotated, Final

import structlog
from fastapi import APIRouter, Depends, Path, Query
from fastapi import status as http_status
from fastapi.responses import Response

from meho_backplane.api.v1._errors import http_for, register_error
from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.auth.rbac import require_role
from meho_backplane.auth.runner_guard import assert_runner_scope, require_runner
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.gateway import assignment_service
from meho_backplane.gateway.deadman import clear_runner_stale
from meho_backplane.gateway.errors import (
    AssignmentOpNotSafeError,
    AssignmentOpUnknownError,
    AssignmentTargetUnknownError,
    AssignmentValidationError,
)
from meho_backplane.gateway.repository import ingest_results
from meho_backplane.gateway.schemas import (
    AssignmentDocument,
    AssignmentDocumentResponse,
    ResultIngestResponse,
)
from meho_backplane.runner import wire

__all__ = ["router"]

_log = structlog.get_logger(__name__)

router = APIRouter(prefix="/api/v1/checks", tags=["checks"])

#: Module-level Depends closures — required to satisfy ruff B008 (calls in
#: default argument positions are disallowed). Same shape as
#: :mod:`meho_backplane.api.v1.scheduler`.
_require_operator = Depends(require_role(TenantRole.OPERATOR))
_require_runner = Depends(require_runner())

#: Canonical op ids bound into ``audit_op_id`` per route (greppable; a typo
#: surfaces as a mis-classified broadcast rather than a silent 500).
_CHECKS_OP_IDS: Final[dict[str, str]] = {
    "put_assignment": "checks.assignment.put",
    "get_assignment": "checks.assignment.get",
    "post_results": "checks.results.post",
}


# The gateway authoring errors surface as HTTPValidationError-conformant
# 422s (``detail[0].type`` carries the machine-readable code) so the
# generated CLI client deserialises them — the api/v1/_errors mould.
register_error(
    AssignmentTargetUnknownError,
    status=http_status.HTTP_422_UNPROCESSABLE_CONTENT,
    type_tag="assignment_target_unknown",
    loc=("body", "items"),
)
register_error(
    AssignmentOpUnknownError,
    status=http_status.HTTP_422_UNPROCESSABLE_CONTENT,
    type_tag="assignment_op_unknown",
    loc=("body", "items"),
)
register_error(
    AssignmentOpNotSafeError,
    status=http_status.HTTP_422_UNPROCESSABLE_CONTENT,
    type_tag="assignment_op_not_safe",
    loc=("body", "items"),
)


@router.put("/assignment/{runner}", response_model=AssignmentDocumentResponse)
async def put_assignment(
    runner: Annotated[str, Path(min_length=1)],
    body: AssignmentDocument,
    operator: Annotated[Operator, _require_operator],
) -> AssignmentDocumentResponse:
    """Replace runner *runner*'s assignment document (operator-authenticated).

    Validates every authored item before storing; a failing item is a
    structured 422 (``assignment_target_unknown`` / ``assignment_op_unknown``
    / ``assignment_op_not_safe``) and nothing is written.
    """
    structlog.contextvars.bind_contextvars(
        audit_op_id=_CHECKS_OP_IDS["put_assignment"],
        audit_op_class="write",
    )
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        try:
            await assignment_service.store_assignment(
                session,
                tenant_id=operator.tenant_id,
                runner_name=runner,
                document=body,
            )
        except AssignmentValidationError as exc:
            raise http_for(exc) from exc
        await session.commit()
    return AssignmentDocumentResponse(runner=runner, items=list(body.items))


@router.get("/assignment", response_model=wire.RunnerAssignment)
async def get_assignment(
    operator: Annotated[Operator, _require_runner],
    runner: Annotated[str, Query(min_length=1)],
    known_version: Annotated[str | None, Query()] = None,
) -> wire.RunnerAssignment | Response:
    """Return runner *runner*'s versioned assignment (runner-authenticated).

    A runner may fetch only its own assignment
    (:func:`assert_runner_scope`). ``known_version`` matching the current
    content digest yields ``304 Not Modified`` (empty body); the runner
    keeps its cache.
    """
    structlog.contextvars.bind_contextvars(
        audit_op_id=_CHECKS_OP_IDS["get_assignment"],
        audit_op_class="read",
    )
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        await assert_runner_scope(operator, runner_name=runner, session=session)
        assignment = await assignment_service.materialize_assignment(
            session,
            tenant_id=operator.tenant_id,
            runner_name=runner,
            principal=_principal_from_operator(operator),
        )
    if known_version is not None and known_version == assignment.assignment_version:
        return Response(status_code=http_status.HTTP_304_NOT_MODIFIED)
    return assignment


@router.post("/results", response_model=ResultIngestResponse)
async def post_results(
    batch: wire.RunnerResultBatch,
    operator: Annotated[Operator, _require_runner],
) -> ResultIngestResponse:
    """Ingest a runner's result batch idempotently (runner-authenticated).

    The runner submits only its own results (``batch.runner_id`` is the
    runner name; :func:`assert_runner_scope` binds it to the token's
    ``runner_id``). ``received_at`` is central-stamped; re-posts from the
    runner's retry spool are deduped by ``result_uid``.
    """
    structlog.contextvars.bind_contextvars(
        audit_op_id=_CHECKS_OP_IDS["post_results"],
        audit_op_class="write",
    )
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        await assert_runner_scope(operator, runner_name=batch.runner_id, session=session)
        accepted, duplicates = await ingest_results(
            session,
            tenant_id=operator.tenant_id,
            runner_name=batch.runner_id,
            results=list(batch.results),
        )
        # Recovery clear seam (#2501): an accepted result batch proves the
        # runner is alive and reporting, so reset any dead-man flip marker
        # the central sweeper set. The sweeper only ever flips; this is the
        # only clear path. Idempotent when the runner was never flipped.
        await clear_runner_stale(session, tenant_id=operator.tenant_id, runner_name=batch.runner_id)
        await session.commit()
    return ResultIngestResponse(accepted=accepted, duplicates=duplicates)


def _principal_from_operator(operator: Operator) -> wire.RunnerPrincipal:
    """Project the authenticated runner operator into the wire principal context.

    Rides on each work item so the runner reconstructs a read-only
    ``Operator`` locally without minting a token (the op was authorized
    centrally).
    """
    return wire.RunnerPrincipal(
        sub=operator.sub,
        tenant_id=operator.tenant_id,
        tenant_role=operator.tenant_role,
        principal_kind=operator.principal_kind,
    )
