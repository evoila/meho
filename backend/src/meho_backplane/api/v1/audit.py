# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``/api/v1/audit/*`` — REST surface for the audit-query substrate (G8.1-T2).

Mounts six routes. Five dispatch through the T1
:func:`~meho_backplane.audit_query.query_audit` handler; the sixth
(replay, G8.2-T4) dispatches through
:func:`~meho_backplane.audit_query.replay_session`:

* ``POST /api/v1/audit/query`` — full filter; body is
  :class:`~meho_backplane.api.v1.audit_models.AuditQueryRequest`.
* ``GET /api/v1/audit/who-touched/{target}`` — pre-canned shortcut
  bound to ``target=<path>``.
* ``GET /api/v1/audit/by-work-ref/{ref}`` — pre-canned shortcut bound
  to ``work_ref=<path>`` (exact match); the "show every write
  authorised by change-ticket X" lookup (work_ref I1-T1 #1655).
* ``GET /api/v1/audit/my-recent`` — pre-canned shortcut bound to
  ``principal=<operator.sub>``.
* ``GET /api/v1/audit/show/{audit_id}`` — single-row fetch. 404 (not
  403) when the audit row is not in the operator's tenant, so the
  route never leaks the existence of an audit row across tenants.
* ``GET /api/v1/audit/sessions/{session_id}/replay`` — per-session
  parent/child replay tree (G8.2-T4), dispatching through
  :func:`~meho_backplane.audit_query.replay_session`. An unknown or
  foreign session yields ``root=[]`` / ``row_count=0`` (never 404 —
  same cross-tenant non-leakage posture as ``show``). A session
  larger than :data:`_REPLAY_ROW_CAP` rows returns 413 from a
  count-first guard evaluated *before* the recursive tree build runs,
  so a runaway session cannot produce an unbounded response or pay
  the cost of materializing the tree just to reject it.

Tenant scoping
==============

Every route passes ``operator.tenant_id`` (lifted from the JWT by
:func:`~meho_backplane.auth.rbac.require_role`) to the substrate as
the mandatory keyword-only ``tenant_id`` argument. Client-supplied
``tenant_id`` (or any other unknown field) in the POST body is
rejected at 422 ``extra_forbidden`` per
:class:`AuditQueryRequest`'s ``extra="forbid"`` config (G0.9-T2 /
#729); the route never reads tenant from the body. Cross-tenant
queries are impossible by construction.

Audit-on-audit-query (decision #3, ``docs/decisions/locked-decisions.md``)
======================================================================

Every route binds two audit-override contextvars before calling the
substrate so the row this request writes (via the chassis
:class:`~meho_backplane.audit.AuditMiddleware`) carries the canonical
audit-surface identity:

* ``audit_op_id`` — the four query routes write under
  ``"meho.audit.query"`` (regardless of which one the operator hit);
  the replay route writes under ``"meho.audit.replay"``. Operators
  querying ``audit_log`` for "everyone who used the audit-query
  surface" filter on ``payload->>'op_id' = 'meho.audit.query'``, and
  on ``'meho.audit.replay'`` for replay usage.
* ``audit_op_class = "audit_query"`` — bound by *every* route
  (including replay). It flips the broadcast event into aggregate-only
  mode per :func:`~meho_backplane.broadcast.events.classify_op`
  policy: SSE feed + Slack subscribers see
  ``{op_id, result_status, row_count}`` only, never the request filter
  contents or the replayed ``ReplayNode`` payload. Audit-query filter
  shapes and a replayed session tree both encode the investigation
  target and the investigator's hunch — both are privacy-sensitive.

``audit_row_count`` is bound after the substrate call returns so the
broadcast event's row-count field reflects the actual returned
cardinality.

RBAC
====

The five **flat / self-scoped** routes (``query``, ``who-touched``,
``by-work-ref``, ``my-recent``, ``show``) require ``operator`` role
minimum (:class:`~meho_backplane.auth.operator.TenantRole.OPERATOR`):
``read_only`` gets 403; ``tenant_admin`` gets 200. The shape mirrors
:mod:`~meho_backplane.api.v1.retrieve` and
:mod:`~meho_backplane.api.v1.retrieve_usage`. These match the MCP
``query_audit`` tool, which is also ``operator``-gated — including its
``shape="tree"`` *self-session* replay, locked to the caller's own
session id.

The **cross-session replay** route
(``GET /api/v1/audit/sessions/{session_id}/replay``) takes an
*arbitrary* ``session_id`` and reconstructs another principal's full
session trace — a privileged forensic act. It requires ``tenant_admin``
(#1843): ``read_only`` and ``operator`` get 403; ``tenant_admin`` gets
200. This aligns the REST surface with the MCP posture, where
cross-session replay is the ``tenant_admin``-gated ``meho.audit.replay``
tool (the operator-level ``query_audit`` ``shape="tree"`` path replays
*only your own* session). Before #1843 the REST route gated cross-session
replay at ``operator``, making the web/CLI surface more permissive than
MCP and than ``docs/cross-repo/audit-replay.md``; tightening to
``tenant_admin`` closes that gap. Self-scoped audit access
(``my-recent``, an operator querying their own principal) stays at
``operator`` — only the arbitrary-session forensic path is lifted.

Error mapping
=============

* :class:`~meho_backplane.audit_query.DurationParseError`
  (``since`` / ``until`` shorthand) → 400 with the parser's message.
* :class:`~meho_backplane.audit_query.InvalidCursorError` (tampered
  cursor) → 400 with the substrate's message.
* :class:`~meho_backplane.audit_query.UnsupportedFilterError`
  (``parent_audit_id`` in v0.2 — ``agent_session_id`` is a *supported*
  flat filter as of G8.2 #1009) → 400 with the column-name message from
  the substrate.

Other exceptions propagate; the chassis middleware turns them into 500.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Final

import sqlalchemy as sa
import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.api.v1.audit_models import AuditQueryRequest, AuditReplayResult
from meho_backplane.audit_query import (
    AuditEntry,
    AuditQueryFilters,
    AuditQueryResult,
    DurationParseError,
    InvalidCursorError,
    MyRecentPage,
    UnsupportedFilterError,
    parse_duration,
    query_audit,
    replay_session,
)
from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.auth.rbac import require_role
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AuditLog

__all__ = ["router"]

router = APIRouter(prefix="/api/v1/audit", tags=["audit"])

#: Module-level :class:`Depends` closure for the flat / self-scoped
#: routes' RBAC gate (``operator`` minimum). Built once at import time to
#: satisfy ruff's B008 rule, matching the convention
#: :mod:`~meho_backplane.api.v1.retrieve_usage` established.
_require_operator = Depends(require_role(TenantRole.OPERATOR))

#: RBAC gate for the cross-session replay route (#1843). Replaying an
#: *arbitrary* ``session_id`` exposes another principal's full session
#: trace, so it is a ``tenant_admin`` forensic act — matching the MCP
#: ``meho.audit.replay`` tool and ``docs/cross-repo/audit-replay.md``.
#: The operator-level paths above stay self-/flat-scoped.
_require_tenant_admin = Depends(require_role(TenantRole.TENANT_ADMIN))

#: Canonical op_id every audit-query call (POST + 3 GET shortcuts) emits
#: via the ``audit_op_id`` contextvar override honoured by
#: :func:`~meho_backplane.audit._publish_broadcast_event`. The companion
#: ``op_class="audit_query"`` flips the broadcast event into aggregate-
#: only mode.
_AUDIT_QUERY_OP_ID: Final[str] = "meho.audit.query"

#: op_id the replay route (G8.2-T4) emits. Distinct from
#: :data:`_AUDIT_QUERY_OP_ID` so operators can tell replay usage apart
#: from flat-query usage in ``audit_log``; it shares the same
#: ``op_class="audit_query"`` so the broadcast event stays aggregate-
#: only (no ``ReplayNode`` tree in the SSE / Slack payload).
_AUDIT_REPLAY_OP_ID: Final[str] = "meho.audit.replay"

#: Hard cap on the number of anchor rows a single replay may carry. A
#: session above this returns 413 from the route's count-first guard
#: (before the recursive tree build runs). The CLI (T5) turns the 413
#: into a redirect to ``meho audit query --session-id``.
_REPLAY_ROW_CAP: Final[int] = 10_000

#: Default ``since`` for the two duration-shorthand GET routes. 24
#: hours matches the issue body's default and the chassis's general
#: "what happened today" framing.
_DEFAULT_SINCE: Final[str] = "24h"


def _bind_audit_overrides(op_id: str = _AUDIT_QUERY_OP_ID) -> None:
    """Bind the audit-override contextvars BEFORE the substrate call.

    Binding early — i.e. before the substrate runs — means a handler
    exception still produces an audit row carrying the correct
    ``op_id`` / ``op_class``; the row-count is bound after success
    because a partial query never produced rows. Mirrors
    :func:`~meho_backplane.api.v1.retrieve_usage._bind_request_audit_context`.

    :param op_id: the ``audit_op_id`` to bind. Defaults to the
        canonical query op_id so the four query routes keep their
        existing behaviour with no call-site change; the replay route
        passes :data:`_AUDIT_REPLAY_OP_ID`. ``op_class`` is always
        ``"audit_query"`` — every route on this surface is
        aggregate-only by policy.
    """
    structlog.contextvars.bind_contextvars(
        audit_op_id=op_id,
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
        work_ref=body.work_ref,
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


@router.get("/by-work-ref/{ref:path}", response_model=AuditQueryResult)
async def by_work_ref(
    ref: str,
    since: str | None = Query(default=None, max_length=32),
    limit: int = Query(default=100, ge=1, le=1000),
    operator: Operator = _require_operator,
) -> AuditQueryResult:
    """Audit rows authorised by the external change-ticket reference *ref*.

    Pre-canned shortcut over :func:`query_audit` with the ``work_ref``
    filter bound to the path param (work_ref I1-T1 #1655) — the headline
    "show every write authorised by ticket X" lookup. The match is
    **exact**: ``ref`` is an opaque identifier (e.g. ``gh:evoila/meho#1``),
    not a search term, so a non-matching value returns an empty result,
    never an error.

    Unlike :func:`who_touched` / :func:`my_recent`, ``since`` has **no**
    default window: a change-ticket lookup wants the whole governed history
    of that ref, not just the last 24h. Passing ``?since=`` narrows it when
    a window is wanted.

    The path converter is ``{ref:path}`` (not the default ``{ref}``) because a
    work_ref carries embedded slashes — ``gh:evoila/meho#1`` — that the default
    converter would refuse to match (404). The ``#`` is passed percent-encoded
    (``%23``); FastAPI decodes it. The OpenAPI path string is still
    ``/api/v1/audit/by-work-ref/{ref}``.
    """
    _bind_audit_overrides()
    since_dt = None
    if since is not None:
        now = datetime.now(UTC)
        try:
            since_dt = parse_duration(since, now=now)
        except DurationParseError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    filters = AuditQueryFilters(work_ref=ref, since=since_dt, limit=limit)
    return await _dispatch(filters, tenant_id=operator.tenant_id)


@router.get("/my-recent", response_model=MyRecentPage)
async def my_recent(
    since: str = Query(default=_DEFAULT_SINCE, max_length=32),
    limit: int = Query(default=100, ge=1, le=1000),
    operator: Operator = _require_operator,
) -> MyRecentPage:
    """Audit rows the calling operator produced within the *since* window.

    Principal is taken from ``operator.sub`` (the JWT subject); an
    operator cannot see another operator's recent activity through
    this route — for that, the full POST surface filters on
    ``principal``.

    Returns the unified ``{"items": [...], "next_cursor": ...}`` list
    envelope per ``docs/codebase/api-shape-conventions.md`` §2 (#2338
    breaking pass — the response shape converged from the v0.8.0
    :class:`AuditQueryResult` ``{"rows": [...]}`` shape onto the
    reference envelope, renaming ``rows`` -> ``items``; the
    ``?envelope=v2`` opt-in that bridged the migration was retired),
    carrying the same forward-only cursor. The sibling audit-query
    endpoints (``/query`` / ``/who-touched`` / ``/by-work-ref``) keep
    the :class:`AuditQueryResult` ``rows`` shape and are out of the §2
    reference set.
    """
    _bind_audit_overrides()
    now = datetime.now(UTC)
    try:
        since_dt = parse_duration(since, now=now)
    except DurationParseError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    filters = AuditQueryFilters(principal=operator.sub, since=since_dt, limit=limit)
    result = await _dispatch(filters, tenant_id=operator.tenant_id)
    return MyRecentPage(items=result.rows, next_cursor=result.next_cursor)


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


async def _count_session_rows(
    session_id: uuid.UUID,
    *,
    tenant_id: uuid.UUID,
    session: AsyncSession,
) -> int:
    """Cheap tenant-scoped count of a session's *anchor* rows.

    ``SELECT count(*) FROM audit_log WHERE agent_session_id = :id AND
    tenant_id = :tid`` — hits the ``audit_log_agent_session_id_idx``
    b-tree, materializes no rows, and never walks lineage. It is the
    bound the count-first 413 guard evaluates and the ``row_count``
    the 200 body echoes, so both report the same number.

    The closure :func:`~meho_backplane.audit_query.replay_session`
    walks can pull in NULL-session lineage children (a composite
    ``dispatch_child`` whose own ``agent_session_id`` is NULL); those
    are deliberately *not* counted — "session rows" are defined by the
    ``agent_session_id`` anchor, matching the issue's WHERE clause.
    """
    stmt = (
        sa.select(sa.func.count())
        .select_from(AuditLog)
        .where(
            AuditLog.agent_session_id == session_id,
            AuditLog.tenant_id == tenant_id,
        )
    )
    return await session.scalar(stmt) or 0


@router.get("/sessions/{session_id}/replay", response_model=AuditReplayResult)
async def replay(
    session_id: uuid.UUID,
    operator: Operator = _require_tenant_admin,
) -> AuditReplayResult:
    """Replay one agent session as a tenant-scoped parent/child tree.

    **RBAC (#1843): ``tenant_admin`` required.** This route takes an
    *arbitrary* ``session_id`` and reconstructs another principal's full
    session trace — a privileged forensic act, not self-service. It
    therefore gates at ``tenant_admin`` (``read_only`` / ``operator`` →
    403), matching the MCP ``meho.audit.replay`` tool and
    ``docs/cross-repo/audit-replay.md``. An operator replaying *their
    own* session uses the MCP ``query_audit`` ``shape="tree"`` path
    instead (operator-level, self-session-only). Cross-tenant isolation
    is unchanged and orthogonal to this gate (see below).

    Dispatches through
    :func:`~meho_backplane.audit_query.replay_session`. ``tenant_id``
    is always ``operator.tenant_id`` from the JWT, never client input,
    so a session belonging to another tenant — or one that does not
    exist — yields ``root=[]`` / ``row_count=0`` (never 404; a foreign
    session is indistinguishable from an empty one, the same
    non-leakage posture :func:`show` takes).

    A session larger than :data:`_REPLAY_ROW_CAP` anchor rows returns
    413 from a count-first guard. The count runs *before*
    :func:`replay_session`, so a runaway session never materializes its
    tree just to be rejected — the recursive build is skipped entirely.
    """
    _bind_audit_overrides(_AUDIT_REPLAY_OP_ID)
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        row_count = await _count_session_rows(
            session_id,
            tenant_id=operator.tenant_id,
            session=session,
        )
        if row_count > _REPLAY_ROW_CAP:
            # Bind the count so the audit-on-replay broadcast still
            # reports the (rejected) cardinality, then refuse before
            # the recursive build runs.
            structlog.contextvars.bind_contextvars(audit_row_count=row_count)
            # FastAPI 0.136 renamed the 413 constant to
            # ``HTTP_413_CONTENT_TOO_LARGE`` (same value, 413) and
            # deprecated the old ``..._REQUEST_ENTITY_TOO_LARGE`` spelling.
            raise HTTPException(
                status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                detail={"detail": "session_too_large", "row_count": row_count},
            )

        root = await replay_session(
            session_id,
            tenant_id=operator.tenant_id,
            session=session,
        )

    structlog.contextvars.bind_contextvars(audit_row_count=row_count)
    return AuditReplayResult(
        root=root,
        session_id=session_id,
        tenant_id=operator.tenant_id,
        row_count=row_count,
    )
