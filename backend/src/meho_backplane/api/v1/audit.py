# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``/api/v1/audit/*`` — REST surface for the audit-query substrate (G8.1-T2).

Mounts four routes, all of which dispatch through the T1
:func:`~meho_backplane.audit_query.query_audit` handler:

* ``POST /api/v1/audit/query`` — full filter; body is
  :class:`~meho_backplane.api.v1.audit_models.AuditQueryRequest`.
* ``GET /api/v1/audit/who-touched/{target}`` — pre-canned shortcut
  bound to ``target=<path>``.
* ``GET /api/v1/audit/my-recent`` — pre-canned shortcut bound to
  ``principal=<operator.sub>``.
* ``GET /api/v1/audit/show/{audit_id}`` — single-row fetch. 404 (not
  403) when the audit row is not in the operator's tenant, so the
  route never leaks the existence of an audit row across tenants.

Tenant scoping
==============

Every route passes ``operator.tenant_id`` (lifted from the JWT by
:func:`~meho_backplane.auth.rbac.require_role`) to ``query_audit`` as
the mandatory keyword-only ``tenant_id`` argument. Client-supplied
``tenant_id`` in the POST body is silently dropped — Pydantic v2's
default ``extra="ignore"`` policy means a field absent from
:class:`AuditQueryRequest` never reaches the router. Cross-tenant
queries are impossible by construction.

Audit-on-audit-query (decision #3, ``docs/planning/v0.2-decisions.md``)
======================================================================

Every route binds two audit-override contextvars before calling
``query_audit`` so the row this request writes (via the chassis
:class:`~meho_backplane.audit.AuditMiddleware`) carries the canonical
audit-query identity:

* ``audit_op_id = "meho.audit.query"`` — every audit-query call,
  regardless of which of the four routes the operator hit, writes
  under this canonical op_id. Operators querying ``audit_log`` for
  "everyone who used the audit-query surface" filter on
  ``payload->>'op_id' = 'meho.audit.query'``.
* ``audit_op_class = "audit_query"`` — flips the broadcast event into
  aggregate-only mode per
  :func:`~meho_backplane.broadcast.events.classify_op` policy: SSE feed
  + Slack subscribers see ``{op_id, result_status, row_count}`` only,
  never the request filter contents. Audit-query filter shapes encode
  the investigation target and the investigator's hunch — both are
  privacy-sensitive.

``audit_row_count`` is bound after ``query_audit`` returns so the
broadcast event's row-count field reflects the actual returned
cardinality.

RBAC
====

All four routes require ``operator`` role minimum
(:class:`~meho_backplane.auth.operator.TenantRole.OPERATOR`).
``read_only`` gets 403; ``tenant_admin`` gets 200. The shape mirrors
:mod:`~meho_backplane.api.v1.retrieve` and
:mod:`~meho_backplane.api.v1.retrieve_usage`.

Error mapping
=============

* :class:`~meho_backplane.audit_query.DurationParseError`
  (``since`` / ``until`` shorthand) → 400 with the parser's message.
* :class:`~meho_backplane.audit_query.InvalidCursorError` (tampered
  cursor) → 400 with the substrate's message.
* :class:`~meho_backplane.audit_query.UnsupportedFilterError`
  (``parent_audit_id`` / ``agent_session_id`` in v0.2) → 400 with the
  column-name message from the substrate.

Other exceptions propagate; the chassis middleware turns them into 500.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Final

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query

from meho_backplane.api.v1.audit_models import AuditQueryRequest
from meho_backplane.audit_query import (
    AuditEntry,
    AuditQueryFilters,
    AuditQueryResult,
    DurationParseError,
    InvalidCursorError,
    UnsupportedFilterError,
    parse_duration,
    query_audit,
)
from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.auth.rbac import require_role
from meho_backplane.db.engine import get_sessionmaker

__all__ = ["router"]

router = APIRouter(prefix="/api/v1/audit", tags=["audit"])

#: Module-level :class:`Depends` closure for the routes' RBAC gate.
#: Built once at import time to satisfy ruff's B008 rule, matching the
#: convention :mod:`~meho_backplane.api.v1.retrieve_usage` established.
_require_operator = Depends(require_role(TenantRole.OPERATOR))

#: Canonical op_id every audit-query call (POST + 3 GET shortcuts) emits
#: via the ``audit_op_id`` contextvar override honoured by
#: :func:`~meho_backplane.audit._publish_broadcast_event`. The companion
#: ``op_class="audit_query"`` flips the broadcast event into aggregate-
#: only mode.
_AUDIT_QUERY_OP_ID: Final[str] = "meho.audit.query"

#: Default ``since`` for the two duration-shorthand GET routes. 24
#: hours matches the issue body's default and the chassis's general
#: "what happened today" framing.
_DEFAULT_SINCE: Final[str] = "24h"


def _bind_audit_overrides() -> None:
    """Bind the audit-override contextvars BEFORE the substrate call.

    Binding early — i.e. before :func:`query_audit` runs — means a
    handler exception still produces an audit row carrying the
    correct ``op_id`` / ``op_class``; the row-count is bound after
    success because a partial query never produced rows. Mirrors
    :func:`~meho_backplane.api.v1.retrieve_usage._bind_request_audit_context`.
    """
    structlog.contextvars.bind_contextvars(
        audit_op_id=_AUDIT_QUERY_OP_ID,
        audit_op_class="audit_query",
    )


async def _dispatch(
    filters: AuditQueryFilters,
    *,
    tenant_id: uuid.UUID,
) -> AuditQueryResult:
    """Acquire a session, call :func:`query_audit`, bind ``audit_row_count``.

    Centralises the substrate dispatch so each route handler stays
    focused on filter construction. Maps the substrate's operator-
    facing validation errors (:class:`InvalidCursorError`,
    :class:`UnsupportedFilterError`) to 400 — other exceptions
    propagate and the chassis turns them into 500.
    """
    sessionmaker = get_sessionmaker()
    try:
        async with sessionmaker() as session:
            result = await query_audit(filters, tenant_id=tenant_id, session=session)
    except (InvalidCursorError, UnsupportedFilterError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    # Row count flows into the broadcast event's ``row_count`` field
    # via ``audit_row_count`` (per ``broadcast/events.py::_maybe_row_count``),
    # giving SSE / Slack subscribers an aggregate signal without
    # revealing the request filter or the matched audit rows.
    structlog.contextvars.bind_contextvars(audit_row_count=len(result.rows))
    return result


@router.post("/query", response_model=AuditQueryResult)
async def query(
    body: AuditQueryRequest,
    operator: Operator = _require_operator,
) -> AuditQueryResult:
    """Run a tenant-scoped audit query with arbitrary filter combinations.

    Body fields mirror :class:`AuditQueryFilters` except ``since`` /
    ``until`` are duration strings (``"24h"`` / ``"7d"`` / ISO-8601)
    parsed at the router layer. Client-supplied ``tenant_id`` (or any
    other unknown field) in the body is rejected at 422
    ``extra_forbidden`` per :class:`AuditQueryRequest`'s
    ``extra="forbid"`` config; the route always passes
    ``operator.tenant_id`` to the substrate.
    """
    _bind_audit_overrides()
    now = datetime.now(UTC)
    try:
        since = parse_duration(body.since, now=now) if body.since is not None else None
        until = parse_duration(body.until, now=now) if body.until is not None else None
    except DurationParseError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    filters = AuditQueryFilters(
        target=body.target,
        principal=body.principal,
        op_id=body.op_id,
        op_class=body.op_class,
        result_status=body.result_status,
        since=since,
        until=until,
        audit_id=body.audit_id,
        parent_audit_id=body.parent_audit_id,
        agent_session_id=body.agent_session_id,
        limit=body.limit,
        cursor=body.cursor,
    )
    return await _dispatch(filters, tenant_id=operator.tenant_id)


@router.get("/who-touched/{target}", response_model=AuditQueryResult)
async def who_touched(
    target: str,
    since: str = Query(default=_DEFAULT_SINCE, max_length=32),
    limit: int = Query(default=100, ge=1, le=1000),
    operator: Operator = _require_operator,
) -> AuditQueryResult:
    """Audit rows that touched *target* within the *since* window.

    Pre-canned shortcut over :func:`query_audit` with the ``target``
    filter bound to the path param. The substrate matches *target*
    against the ``targets`` table scoped to the operator's tenant; a
    non-matching name returns an empty result, never an error.
    """
    _bind_audit_overrides()
    now = datetime.now(UTC)
    try:
        since_dt = parse_duration(since, now=now)
    except DurationParseError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    filters = AuditQueryFilters(target=target, since=since_dt, limit=limit)
    return await _dispatch(filters, tenant_id=operator.tenant_id)


@router.get("/my-recent", response_model=AuditQueryResult)
async def my_recent(
    since: str = Query(default=_DEFAULT_SINCE, max_length=32),
    limit: int = Query(default=100, ge=1, le=1000),
    operator: Operator = _require_operator,
) -> AuditQueryResult:
    """Audit rows the calling operator produced within the *since* window.

    Principal is taken from ``operator.sub`` (the JWT subject); an
    operator cannot see another operator's recent activity through
    this route — for that, the full POST surface filters on
    ``principal``.
    """
    _bind_audit_overrides()
    now = datetime.now(UTC)
    try:
        since_dt = parse_duration(since, now=now)
    except DurationParseError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    filters = AuditQueryFilters(principal=operator.sub, since=since_dt, limit=limit)
    return await _dispatch(filters, tenant_id=operator.tenant_id)


@router.get("/show/{audit_id}", response_model=AuditEntry)
async def show(
    audit_id: uuid.UUID,
    operator: Operator = _require_operator,
) -> AuditEntry:
    """Fetch a single audit row by id, scoped to the operator's tenant.

    Cross-tenant probe semantics: the substrate's first WHERE clause
    is always ``tenant_id = operator.tenant_id``, so a request for an
    ``audit_id`` that exists but belongs to another tenant returns
    zero rows. The route then surfaces 404 — never 403 — so a probing
    operator cannot distinguish "row doesn't exist" from "row belongs
    to another tenant".
    """
    _bind_audit_overrides()
    filters = AuditQueryFilters(audit_id=audit_id, limit=1)
    result = await _dispatch(filters, tenant_id=operator.tenant_id)
    if not result.rows:
        raise HTTPException(status_code=404, detail="audit row not found")
    return result.rows[0]
