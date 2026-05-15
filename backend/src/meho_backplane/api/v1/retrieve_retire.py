# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``POST /api/v1/retrieve/retire-checklist`` — retire-decision verdict surface.

G4.3-T6 (#445) of Initiative #373. The route is the HTTP face of the
:func:`meho_backplane.retrieval.retire.compute_retire_checklist`
service — combines T2's eval results (precision@5 + MEHO-vs-baseline)
with T5's audit-log-backed usage telemetry (daily-use date + operator
breadth) and a caller-supplied count of open
``retrieval-migration-blocker`` issues to produce the five-criterion
per-surface green/yellow/red checklist Goal #215 decision #2 locked.

The CLI verb ``meho retrieval retire-checklist`` (also T6) runs the
``gh issue list`` lookup locally and passes the surface-bucketed count
in the request body — the backend has no GitHub credentials and no
operator-facing audit-trail justification for outbound API calls.

RBAC
----

``operator`` role minimum (mirrors
:mod:`~meho_backplane.api.v1.retrieve_usage`). ``operator`` callers
are scoped to their own ``operator.tenant_id``; passing a non-null
``tenant_filter`` returns 403. Only ``tenant_admin`` may cross
tenants — the retire decision spans a tenant's corpus + audit-log
history, so cross-tenant inspection is a tenant_admin concern.

Audit + broadcast contract
--------------------------

Mirrors T2 (#441) eval + T5 (#444) usage telemetry posture: the route
binds two audit-override contextvars *before* the service runs so a
handler exception still produces an audit row with the partial
payload, and so the broadcast publisher emits an ``audit_query``-class
aggregate-only event (the retire-checklist combines eval queries +
usage filters, both of which can leak operator intent — the surface
filter alone reveals which retrieval surface an operator is preparing
to retire).

* ``audit_op_id = "meho.retrieval.retire_checklist"`` — canonical
  op_id for every audit row this route writes. Operators querying
  audit_log for "everyone who triggered a retire-checklist" filter
  on ``payload->>'op_id' = 'meho.retrieval.retire_checklist'``.
* ``audit_op_class = "audit_query"`` — flips the broadcast event
  into aggregate-only mode, matching every other retrieval-audit
  surface and the G8 audit-query API.

Two further enrichment fields land in the audit_log row's payload:

* ``audit_surfaces`` — which surfaces the operator requested.
* ``audit_tenant_scope`` — ``"self"`` when scoped to the operator's
  own tenant; ``"other"`` when a tenant_admin crosses tenants.

``audit_row_count`` is bound after the service returns so the
broadcast event's ``row_count`` field reflects the number of
surfaces evaluated in this report (the meaningful aggregate
cardinality for this verb), not the underlying audit_log scan size.
"""

from __future__ import annotations

from typing import Literal
from uuid import UUID

import structlog
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.auth.rbac import require_role
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.retrieval.retire import (
    SURFACE_VERDICT_ORDER,
    ChecklistSurface,
    RetireChecklistReport,
    compute_retire_checklist,
)
from meho_backplane.retrieval.usage import SUPPORTED_SURFACES

__all__ = ["router"]

router = APIRouter(prefix="/api/v1/retrieve", tags=["retrieval"])

#: Module-level ``Depends`` closure for the route's RBAC gate. Built
#: once at import time to satisfy ruff's B008 rule, matching the
#: pattern :mod:`~meho_backplane.api.v1.retrieve_usage` +
#: :mod:`~meho_backplane.api.v1.retrieve_eval` established.
_require_operator = Depends(require_role(TenantRole.OPERATOR))

#: The surface-filter literal accepted by the request body. ``"all"``
#: expands to every supported surface; the per-surface labels narrow
#: the report to one surface.
_RetireRequestSurface = Literal["kb", "memory", "operations", "all"]


class RetireChecklistRequest(BaseModel):
    """POST body for ``/api/v1/retrieve/retire-checklist``.

    Frozen + ``extra="forbid"`` so a typo (``surfaces`` instead of
    ``surface``, or ``blocker_count`` instead of ``blocker_counts``)
    fails 422 at the framework boundary rather than silently running
    the default and giving the caller a confusing report.

    ``blocker_counts`` is the surface→count mapping the CLI fills in
    from a local ``gh issue list`` lookup. ``None`` (the default) is
    valid — the service reports criterion 5 as yellow (unknown). Every
    key must be one of the three supported surfaces; the
    ``dict[Literal, int]`` shape pins that at the Pydantic boundary.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    surface: _RetireRequestSurface = Field(default="all")
    blocker_counts: dict[ChecklistSurface, int] | None = Field(default=None)
    tenant_filter: UUID | None = Field(default=None)


def _resolve_surfaces(
    surface: _RetireRequestSurface,
) -> list[ChecklistSurface]:
    """Expand the request-body surface filter to the service-level list.

    ``"all"`` expands to :data:`SURFACE_VERDICT_ORDER` so the response
    surface order is stable for the CLI table renderer. Per-surface
    values produce a single-element list.
    """
    if surface == "all":
        return list(SURFACE_VERDICT_ORDER)
    return [surface]


def _bind_request_audit_context(
    *,
    surfaces: list[ChecklistSurface],
    tenant_filter: UUID | None,
    operator_tenant_id: UUID,
) -> None:
    """Bind the audit overrides + enrichment fields for this request.

    Called **before** :func:`compute_retire_checklist` runs so a
    handler exception still produces an audit row with partial
    payload. The two override keys (``audit_op_id`` /
    ``audit_op_class``) are honoured by the chassis broadcast
    publisher per
    :func:`meho_backplane.audit._publish_broadcast_event`;
    ``audit_tenant_scope`` distinguishes own-tenant inspection from
    genuine cross-tenant audit-trail queries.
    """
    structlog.contextvars.bind_contextvars(
        audit_op_id="meho.retrieval.retire_checklist",
        audit_op_class="audit_query",
        audit_surfaces=",".join(surfaces),
        audit_tenant_scope=(
            "other" if tenant_filter is not None and tenant_filter != operator_tenant_id else "self"
        ),
    )


@router.post(
    "/retire-checklist",
    response_model=RetireChecklistReport,
)
async def retire_checklist_endpoint(
    body: RetireChecklistRequest,
    operator: Operator = _require_operator,
) -> RetireChecklistReport:
    """Return the five-criterion retire-decision checklist per surface.

    Tenant scoping mirrors the sibling usage route:
    ``operator`` / ``read_only`` callers are scoped to
    ``operator.tenant_id``; passing a non-null ``tenant_filter``
    returns 403. Only ``tenant_admin`` may cross tenants. The request
    body's ``surface=all`` (default) walks every supported surface in
    the order :data:`SURFACE_VERDICT_ORDER` pins.

    Audit overrides + enrichment contextvars are bound before the
    service kicks off so a handler exception still produces an audit
    row with partial payload (incident postmortem hook).
    ``audit_row_count`` is bound after the service returns so the
    broadcast event's ``row_count`` field reflects the number of
    surfaces evaluated, not the underlying audit_log scan size.
    """
    if body.tenant_filter is not None and operator.tenant_role != TenantRole.TENANT_ADMIN:
        raise HTTPException(
            status_code=403,
            detail="tenant_filter_requires_tenant_admin",
        )

    surfaces = _resolve_surfaces(body.surface)
    target_tenant = body.tenant_filter if body.tenant_filter is not None else operator.tenant_id

    _bind_request_audit_context(
        surfaces=surfaces,
        tenant_filter=body.tenant_filter,
        operator_tenant_id=operator.tenant_id,
    )

    # ``blocker_counts`` may carry keys that aren't part of the
    # currently-requested surface scope; Pydantic has already pinned
    # the keys to the three supported surfaces, so the service-side
    # ``Mapping[surface, int].get(...)`` semantics handle the narrow.
    # Defensively guarantee no unsupported surface leaks through by
    # building a fresh dict that mirrors the requested scope.
    blocker_counts = body.blocker_counts
    if blocker_counts is not None:
        unknown = set(blocker_counts.keys()) - set(SUPPORTED_SURFACES)
        if unknown:
            # Belt-and-braces: ``dict[Literal[...], int]`` should make
            # this unreachable from a well-typed client, but a hand-
            # rolled JSON consumer could still post an unknown key
            # before Pydantic widens the type. 422 is the spec-correct
            # response surface for "your body shape is wrong".
            raise HTTPException(
                status_code=422,
                detail=f"unknown surfaces in blocker_counts: {sorted(unknown)!r}",
            )

    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        report = await compute_retire_checklist(
            session=session,
            surfaces=surfaces,
            tenant_id=target_tenant,
            blocker_counts=blocker_counts,
        )

    structlog.contextvars.bind_contextvars(audit_row_count=len(report.surfaces))
    return report
