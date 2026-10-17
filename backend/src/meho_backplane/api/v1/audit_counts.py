# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``POST /api/v1/audit/class-counts`` -- rows per event class (#3747).

Takes the same filters as ``POST /api/v1/audit/query`` (without
``event_class``, ``limit`` and ``cursor``) and answers one row count per
event class through :func:`~meho_backplane.audit_query.count_event_classes`.
Old rows count as ``legacy_request``; nothing is reclassified.

Same posture as the query routes in :mod:`meho_backplane.api.v1.audit`:
``operator`` role minimum, tenant from the JWT (never the body), and the
request is audited as an aggregate-only audit query.
"""

from __future__ import annotations

import structlog
from fastapi import APIRouter, Depends, HTTPException

from meho_backplane.api.v1.audit_models import AuditClassCounts, AuditClassCountsRequest
from meho_backplane.audit_query import DurationParseError, count_event_classes
from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.auth.rbac import require_role
from meho_backplane.db.engine import get_sessionmaker

__all__ = ["router"]

router = APIRouter(prefix="/api/v1/audit", tags=["audit"])

_require_operator = Depends(require_role(TenantRole.OPERATOR))


@router.post("/class-counts", response_model=AuditClassCounts)
async def class_counts(
    body: AuditClassCountsRequest,
    operator: Operator = _require_operator,
) -> AuditClassCounts:
    """Count the matching rows per event class, tenant-scoped (#3747)."""
    structlog.contextvars.bind_contextvars(
        audit_op_id="meho.audit.query",
        audit_op_class="audit_query",
    )
    try:
        filters = body.to_filters()
    except DurationParseError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    async with get_sessionmaker()() as session:
        counts = await count_event_classes(filters, tenant_id=operator.tenant_id, session=session)
    total = sum(counts.values())
    structlog.contextvars.bind_contextvars(audit_row_count=total)
    return AuditClassCounts(counts=counts, total=total)
