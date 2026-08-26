# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``GET /api/v1/audit/reflex`` -- reflex-adoption KPIs over ``audit_log``.

Task #3134 (Initiative #3128). Exposes the four reflex-adoption metrics
computed by :func:`meho_backplane.reflex.compute_reflex_report` over a
time window, per tenant, split by surface (``agent`` / ``cli_rest``).
Follows the #444 usage-telemetry route shape exactly. Operator
workflow:

* ``GET /api/v1/audit/reflex`` — operator's own tenant, default 7-day
  window ending now.
* ``GET /api/v1/audit/reflex?since=30d`` / ``?since=2026-08-01`` —
  widen the window (relative ``<N>d`` / ``<N>h`` or ISO-8601 date).
* ``GET /api/v1/audit/reflex?until=1d`` — cap the window's upper bound
  (same grammar; default is now).
* ``GET /api/v1/audit/reflex?tenant_filter=<uuid>`` — cross-tenant view,
  gated behind the ``platform_admin`` capability (#1638).

The route backs ``meho audit reflex``.

Audit + broadcast contract
--------------------------

Reading ``audit_log`` is privacy-sensitive (decision #3,
``docs/decisions/locked-decisions.md``): a free-text broadcast of
"operator X read reflex KPIs for tenant Y" leaks the investigation
target. The route binds the same two audit overrides the G8 audit-query
and #444 usage surfaces use:

* ``audit_op_id = "meho.audit.reflex"`` — the canonical op_id every
  audit row this route writes carries.
* ``audit_op_class = "audit_query"`` — flips the broadcast event into
  aggregate-only mode (``{op_class, result_status, row_count}``).

``audit_since`` / ``audit_until`` enrich the row's ``payload`` for
forensic queries (aggregation parameters, not raw queries, so no
redaction needed). ``audit_row_count`` is bound after
:func:`compute_reflex_report` returns to the number of write-class
operations scored — the aggregate cardinality the broadcast subscriber
cares about.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.auth.rbac import authorize_tenant_scope, require_role
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.reflex.adoption import (
    DEFAULT_SINCE,
    ReflexReport,
    SinceValueError,
    compute_reflex_report,
    parse_since,
)

__all__ = ["router"]

router = APIRouter(prefix="/api/v1/audit", tags=["audit"])

#: Module-level :class:`Depends` closure for the route's RBAC gate.
#: Built once at import time to satisfy ruff's B008 rule, matching the
#: pattern :mod:`~meho_backplane.api.v1.retrieve_usage` established.
_require_operator = Depends(require_role(TenantRole.OPERATOR))


def _bind_request_audit_context(
    *,
    since: str,
    until: str | None,
    tenant_filter: UUID | None,
    operator_tenant_id: UUID,
) -> None:
    """Bind the audit overrides + enrichment fields for this request.

    Called **before** :func:`compute_reflex_report` runs so a handler
    exception still produces an audit row with partial payload. Mirrors
    :func:`meho_backplane.api.v1.retrieve_usage._bind_request_audit_context`.
    """
    structlog.contextvars.bind_contextvars(
        audit_op_id="meho.audit.reflex",
        audit_op_class="audit_query",
        audit_since=since,
        audit_until=until if until is not None else "now",
        audit_tenant_scope=(
            "other" if tenant_filter is not None and tenant_filter != operator_tenant_id else "self"
        ),
    )


@router.get("/reflex", response_model=ReflexReport)
async def reflex_endpoint(
    since: str = Query(default=DEFAULT_SINCE, max_length=32),
    until: str | None = Query(default=None, max_length=32),
    tenant_filter: UUID | None = Query(default=None),
    operator: Operator = _require_operator,
) -> ReflexReport:
    """Return reflex-adoption KPIs for *operator* over the requested window.

    Tenant scoping: callers are scoped to ``operator.tenant_id``; a
    ``tenant_filter`` naming a different tenant returns 403
    ``cross_tenant_requires_platform_admin`` unless the caller holds the
    ``platform_admin`` capability (#1638). *since* / *until* accept
    ``<N>d`` / ``<N>h`` (relative) or an ISO-8601 date; malformed → 400.
    *until* defaults to now. An empty window is a structured zero report
    (both surfaces present, ``None`` ratios), not 404.
    """
    target_tenant = authorize_tenant_scope(operator, tenant_filter)

    now = datetime.now(UTC)
    try:
        since_dt = parse_since(since, now=now)
        until_dt = parse_since(until, now=now) if until is not None else now
    except SinceValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    _bind_request_audit_context(
        since=since,
        until=until,
        tenant_filter=tenant_filter,
        operator_tenant_id=operator.tenant_id,
    )

    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        report = await compute_reflex_report(
            session=session,
            since=since_dt,
            until=until_dt,
            tenant_id=target_tenant,
        )

    # The broadcast event's ``row_count`` reflects the aggregate the
    # subscriber cares about: the number of write-class operations
    # scored across surfaces, not the raw rows paged through.
    row_count = sum(surface.announce_coverage_write_ops for surface in report.surfaces)
    structlog.contextvars.bind_contextvars(audit_row_count=row_count)
    return report
