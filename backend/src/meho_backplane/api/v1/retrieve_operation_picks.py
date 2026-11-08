# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``GET /api/v1/retrieve/operation-picks`` -- did the agent pick from the search?

Per connector and per day: how many operation searches ran, how many calls
used an operation from the search's results (at rank 1, in the top 3,
anywhere), how many of those returned ``ok``, and how many calls had no
search before them (#4750). Computed by
:func:`meho_backplane.operation_picks.report.compute_pick_report` from the
search and call records. MEHO writes those records only when
``REQUEST_USAGE_RETENTION_DAYS`` is set, so the report is empty when that
setting is off.

The CLI verb is ``meho retrieval picks``. The tenant rule is the same as
``GET /api/v1/retrieve/usage``: the caller's own tenant, and another tenant
only for a platform admin. Like that route, the audit row is classed
``audit_query`` under the op id ``meho.retrieval.operation_picks``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.auth.rbac import authorize_tenant_scope, require_role
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.operation_picks.report import OperationPickReport, compute_pick_report
from meho_backplane.retrieval.usage import DEFAULT_SINCE, SinceValueError, parse_since

__all__ = ["router"]

router = APIRouter(prefix="/api/v1/retrieve", tags=["retrieval"])

_require_operator = Depends(require_role(TenantRole.OPERATOR))


@router.get("/operation-picks", response_model=OperationPickReport)
async def operation_picks_endpoint(
    since: str = Query(default=DEFAULT_SINCE, max_length=32),
    connector_id: str | None = Query(default=None, min_length=1, max_length=512),
    tenant_filter: UUID | None = Query(default=None),
    operator: Operator = _require_operator,
) -> OperationPickReport:
    """Return the operation pick numbers per connector and day.

    *since* accepts ``<N>d`` / ``<N>h`` or an ISO-8601 date; a malformed
    value is a 400. *connector_id* narrows the report to one connector. A
    *tenant_filter* naming another tenant is a 403 unless the caller is a
    platform admin. No records in the window is an empty ``buckets`` list.
    """
    target_tenant = authorize_tenant_scope(operator, tenant_filter)
    now = datetime.now(UTC)
    try:
        since_dt = parse_since(since, now=now)
    except SinceValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    structlog.contextvars.bind_contextvars(
        audit_op_id="meho.retrieval.operation_picks",
        audit_op_class="audit_query",
        audit_since=since,
        audit_tenant_scope="other" if target_tenant != operator.tenant_id else "self",
    )
    async with get_sessionmaker()() as session:
        report = await compute_pick_report(
            session,
            tenant_id=target_tenant,
            since=since_dt,
            until=now,
            connector_id=connector_id,
        )
    structlog.contextvars.bind_contextvars(audit_row_count=len(report.buckets))
    return report
