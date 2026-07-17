# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``GET /api/v1/retrieve/usage`` -- audit-backed retrieval usage telemetry.

G4.3-T5 (#444) of Initiative #373. Exposes per-day, per-surface,
per-tenant aggregates of the retrieval-class MCP meta-tool invocations
that ``audit_log`` already records, computed by
:func:`meho_backplane.retrieval.usage.compute_usage`. Operator workflow:

* ``GET /api/v1/retrieve/usage`` — operator's own tenant; defaults to
  the last 30 days, all three surfaces.
* ``GET /api/v1/retrieve/usage?surface=kb`` — narrow to one surface.
* ``GET /api/v1/retrieve/usage?since=2026-04-01`` — absolute ISO date.
* ``GET /api/v1/retrieve/usage?tenant_filter=<uuid>`` —
  ``tenant_admin``-only cross-tenant view. ``operator`` role with a
  non-null ``tenant_filter`` returns 403.

The route is the API surface backing ``meho retrieval usage``; the
retire-checklist verb (T6, #445) consumes the same endpoint with
``--json`` to ingest the structured shape.

Audit + broadcast contract
--------------------------

Reading audit_log is itself privacy-sensitive (decision #3,
``docs/decisions/locked-decisions.md``): a free-text broadcast of "operator
X queried usage for tenant Y in window Z" would leak the investigation
target. The route binds two audit overrides via the chassis contextvar
mechanism (see :func:`meho_backplane.audit._publish_broadcast_event`):

* ``audit_op_id = "meho.retrieval.usage"`` — the canonical op_id for
  every audit row this route writes. Operators querying ``audit_log``
  for "everyone who hit the usage telemetry surface" filter on
  ``payload->>'op_id' = 'meho.retrieval.usage'``.
* ``audit_op_class = "audit_query"`` — flips the broadcast event into
  aggregate-only mode (``{op_class, result_status, row_count}``), the
  same posture the G8 audit-query API ships under (#334).

Two further audit_* contextvars enrich the audit_log row's
``payload`` JSON for forensic queries — ``audit_surfaces`` (which
surfaces the operator requested) and ``audit_since`` (the raw
``--since`` value). Both are aggregation parameters, not raw queries,
so they do not require redaction.

``audit_row_count`` is bound after :func:`compute_usage` returns so
the broadcast event's ``row_count`` field reflects the actual
aggregate cardinality (the ``total_searches`` field of the response),
not the number of audit_log rows scanned. That choice matches the
G8 audit-query convention.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal
from uuid import UUID

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.auth.rbac import authorize_tenant_scope, require_role
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.retrieval.usage import (
    DEFAULT_SINCE,
    SUPPORTED_SURFACES,
    SinceValueError,
    UsageReport,
    compute_usage,
    parse_since,
)

__all__ = ["router"]

router = APIRouter(prefix="/api/v1/retrieve", tags=["retrieval"])

#: Module-level :class:`Depends` closure for the route's RBAC gate.
#: Built once at import time to satisfy ruff's B008 rule, matching the
#: pattern :mod:`~meho_backplane.api.v1.retrieve` and
#: :mod:`~meho_backplane.api.v1.operations` established.
_require_operator = Depends(require_role(TenantRole.OPERATOR))


def _resolve_surfaces(
    surface: Literal["kb", "memory", "operations", "all"],
) -> list[str]:
    """Expand the route's surface query param to the service-level list."""
    if surface == "all":
        return list(SUPPORTED_SURFACES)
    return [surface]


def _bind_request_audit_context(
    *,
    surfaces: list[str],
    since: str,
    tenant_filter: UUID | None,
    operator_tenant_id: UUID,
) -> None:
    """Bind the audit overrides + enrichment fields for this request.

    Called **before** :func:`compute_usage` runs so a handler
    exception still produces an audit row with partial payload
    (incident postmortem hook). The two override keys
    (``audit_op_id`` / ``audit_op_class``) are honoured by the
    chassis broadcast publisher per
    :func:`meho_backplane.audit._publish_broadcast_event`;
    ``audit_tenant_scope`` distinguishes own-tenant inspection
    from genuine cross-tenant audit-trail queries.
    """
    structlog.contextvars.bind_contextvars(
        audit_op_id="meho.retrieval.usage",
        audit_op_class="audit_query",
        audit_surfaces=",".join(surfaces),
        audit_since=since,
        audit_tenant_scope=(
            "other" if tenant_filter is not None and tenant_filter != operator_tenant_id else "self"
        ),
    )


@router.get("/usage", response_model=UsageReport)
async def usage_endpoint(
    since: str = Query(default=DEFAULT_SINCE, max_length=32),
    surface: Literal["kb", "memory", "operations", "all"] = Query(default="all"),
    tenant_filter: UUID | None = Query(default=None),
    operator: Operator = _require_operator,
) -> UsageReport:
    """Return audit-backed retrieval usage aggregates for *operator*.

    Tenant scoping: callers are scoped to ``operator.tenant_id``; a
    ``tenant_filter`` naming a different tenant returns 403
    ``cross_tenant_requires_platform_admin`` unless the caller holds the
    ``platform_admin`` cross-tenant capability (#1638). *since*
    accepts ``<N>d`` / ``<N>h`` (relative) or an ISO-8601 date;
    malformed → 400. ``surface=all`` (default) covers all three;
    ``surface=<one>`` narrows. Empty result is a structured zero
    report, not 404. Audit overrides + enrichment contextvars are
    bound before :func:`compute_usage` runs so a handler exception
    still produces an audit row with partial payload.
    """
    target_tenant = authorize_tenant_scope(operator, tenant_filter)

    surfaces = _resolve_surfaces(surface)
    now = datetime.now(UTC)
    try:
        since_dt = parse_since(since, now=now)
    except SinceValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    _bind_request_audit_context(
        surfaces=surfaces,
        since=since,
        tenant_filter=tenant_filter,
        operator_tenant_id=operator.tenant_id,
    )

    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        report = await compute_usage(
            session=session,
            since=since_dt,
            until=now,
            surfaces=surfaces,
            tenant_id=target_tenant,
        )

    # ``audit_row_count`` becomes the broadcast event's ``row_count``
    # field (per ``broadcast/events.py::_maybe_row_count``). Use the
    # aggregate cardinality (``total_searches``), not the raw scan
    # count — the broadcast subscriber wants to know "how much
    # retrieval activity was reported", not "how many rows the helper
    # paged through".
    structlog.contextvars.bind_contextvars(audit_row_count=report.total_searches)
    return report
