# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tenant-scoped query handler for the audit-query substrate.

The handler is the substrate T2 (REST), T3 (CLI), T4 (MCP) dispatch through.
``tenant_id`` is a mandatory positional argument — never on
:class:`~meho_backplane.audit_query.schemas.AuditQueryFilters` — so cross-tenant
queries are impossible by construction. The first SQL WHERE clause is always
``audit_log.tenant_id = :tenant_id``.

Ordering and pagination
=======================

Rows return in ``(occurred_at DESC, id DESC)`` order. The forward-only
opaque cursor encodes the last returned row's ``(occurred_at, id)`` pair;
the next page applies the lex-compare ``(occurred_at, id) < (cursor.ts,
cursor.id)`` so the next row is strictly older than the cursor's row.
``LIMIT N+1`` detects whether more rows exist; the (N+1)th row is dropped
from the returned page and its ``(occurred_at, id)`` is encoded as
``next_cursor``.

Filter coverage
===============

* Column-mapped filters are SQL-side: ``audit_id`` (exact), ``principal``
  (``operator_sub ILIKE`` partial match — LIKE metacharacters escaped so
  ``%`` / ``_`` in the operator-supplied value match literally),
  ``since`` / ``until`` (range on ``occurred_at``), ``target`` (name match
  against ``targets`` in the same tenant — alias resolution is the T2
  router's job).
* ``op_id`` (glob with ``*`` ↔ ``%``) and ``op_class`` (exact) match either
  the value stored in ``payload`` JSON (MCP-written rows carry ``op_id`` /
  ``op_class`` in ``payload`` per ``mcp/handlers.py:214-221``) **OR** the
  value derived for HTTP rows — ``f"http.{method.lower()}:{path}"`` for
  ``op_id``. The OR-shaped predicate keeps the substrate honest across both
  write paths.
* ``result_status`` (one of ``"ok"`` / ``"pending"`` / ``"error"`` /
  ``"denied"``) maps to ``status_code`` ranges that mirror
  :func:`~meho_backplane.audit._classify_http_status` (``"pending"`` is
  the G11.2-T3/T4 202 "awaiting approval" synthetic code).
* ``agent_session_id`` is a real column (G8.2-T1 #1009, migration ``0014``).
  The filter narrows to ``audit_log.agent_session_id = :agent_session_id``;
  the column is also surfaced on every returned ``AuditEntry`` and drives the
  per-session replay query
  (:func:`~meho_backplane.audit_query.replay.replay_session`).
* ``work_ref`` is a real column (work_ref I1-T1 #1655, migration ``0039``).
  The filter narrows to ``audit_log.work_ref = :work_ref`` — exact match (an
  opaque change-ticket reference such as ``"gh:evoila/meho#1"`` is an
  identifier, not a search term, so the predicate is deterministic equality
  per #1177, not ``ILIKE``); the column is also surfaced on every returned
  ``AuditEntry``. NULL on rows with no bound work_ref, so a ``work_ref``
  filter excludes them.
* ``parent_audit_id`` is a real column too (G0.6-T7 #398, migration ``0006``)
  and is surfaced on every returned ``AuditEntry``, but the *flat filter* on
  it still raises :class:`UnsupportedFilterError` — un-gating that filter is
  out of scope for G8.2 (#377); replay reads the column directly via its
  recursive CTE instead.
"""

from __future__ import annotations

import uuid

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.broadcast.events import classify_op
from meho_backplane.db.models import AuditLog, Target

from .cursor import CursorPosition, decode_cursor, encode_cursor
from .schemas import AuditEntry, AuditQueryFilters, AuditQueryResult

__all__ = [
    "UnsupportedFilterError",
    "query_audit",
]


class UnsupportedFilterError(ValueError):
    """Raised when a filter targets a field the v0.2 substrate cannot evaluate.

    Distinct from a validation error: the field is on
    :class:`AuditQueryFilters` (so router-side validation accepts it) but
    the column it would filter against does not exist yet. The handler
    surfaces this synchronously before issuing any SQL so the caller can
    return a structured 400 / -32602 with the column name in the message.
    """


#: Escape character for LIKE patterns built from operator-controllable input.
#: ``%`` / ``_`` in the raw value are protected via this escape so only the
#: explicit ``*`` glob (in ``op_id``) translates to a wildcard, and substring
#: matches (in ``principal``) treat their input verbatim.
_LIKE_ESCAPE: str = "\\"


def _escape_like_literal(raw: str) -> str:
    """Escape SQL LIKE metacharacters in *raw* so it matches literally.

    Order matters: the backslash is escaped first so a literal ``\\`` in the
    input doesn't double-escape later additions, then ``%`` and ``_`` (the
    two SQL LIKE wildcards). Paired with ``escape="\\"`` on the
    :meth:`like` / :meth:`ilike` call so the SQL ``ESCAPE`` clause treats the
    backslash as a literal-marker on both PostgreSQL and SQLite.
    """
    return (
        raw.replace(_LIKE_ESCAPE, _LIKE_ESCAPE + _LIKE_ESCAPE)
        .replace("%", _LIKE_ESCAPE + "%")
        .replace("_", _LIKE_ESCAPE + "_")
    )


def _build_op_id_like_pattern(raw: str) -> str:
    """Escape SQL LIKE metacharacters in *raw*, then translate glob ``*`` to ``%``.

    Builds on :func:`_escape_like_literal` and adds the consumer-facing glob
    translation: a ``*`` in the input becomes a SQL ``%`` wildcard. Literal
    ``%`` / ``_`` in the operator-supplied value are still matched verbatim
    because the escape pass runs first.
    """
    return _escape_like_literal(raw).replace("*", "%")


async def query_audit(
    filters: AuditQueryFilters,
    *,
    tenant_id: uuid.UUID,
    session: AsyncSession,
) -> AuditQueryResult:
    """Tenant-scoped paginated query over ``audit_log``.

    ``tenant_id`` is a mandatory keyword-only argument — never sourced from
    :class:`AuditQueryFilters` — so cross-tenant queries are impossible by
    construction. The first SQL WHERE clause is always
    ``audit_log.tenant_id = :tenant_id``; the LEFT JOIN to ``targets`` for
    ``target_name`` denormalization is *also* scoped on ``Target.tenant_id``
    so a cross-tenant ``target_id`` (allowed today because ``audit_log``
    keeps no FK on the column per v0.2's soft-FK discipline) resolves to
    ``target_name=None`` rather than leaking the other tenant's target name.
    """
    if filters.parent_audit_id is not None:
        raise UnsupportedFilterError(
            "parent_audit_id filter not supported in v0.2 — column lands with G0.6-T7 (#398)",
        )

    stmt = (
        sa.select(AuditLog, Target.name.label("target_name"))
        .outerjoin(
            Target,
            sa.and_(
                AuditLog.target_id == Target.id,
                Target.tenant_id == tenant_id,
            ),
        )
        .where(AuditLog.tenant_id == tenant_id)
    )

    stmt = _apply_filters(stmt, filters, tenant_id=tenant_id)

    stmt = stmt.order_by(AuditLog.occurred_at.desc(), AuditLog.id.desc()).limit(
        filters.limit + 1,
    )

    result = await session.execute(stmt)
    raw_rows = list(result.all())
    has_more = len(raw_rows) > filters.limit
    page = raw_rows[: filters.limit]

    entries = [_build_audit_entry(row.AuditLog, row.target_name) for row in page]

    next_cursor: str | None = None
    if has_more and entries:
        last = entries[-1]
        next_cursor = encode_cursor(CursorPosition(ts=last.ts, id=last.id))

    return AuditQueryResult(rows=entries, next_cursor=next_cursor)


# A flat chain of independent `if filters.X is not None` guards — one per
# optional filter field — is the clearest shape for optional filter application;
# splitting it into sub-helpers would fragment cohesive logic for no readability
# code-quality-allow: gain (C901 is inherent to the optional-field count).
def _apply_filters(
    stmt: sa.Select[tuple[AuditLog, str]],
    filters: AuditQueryFilters,
    *,
    tenant_id: uuid.UUID,
) -> sa.Select[tuple[AuditLog, str]]:
    """Apply every set column filter (and the cursor) to *stmt*.

    Extracted from :func:`query_audit` so the handler stays a readable
    fetch-build-page skeleton; each clause is added only when its filter field
    is populated. The tenant scope is *not* re-applied here — the caller already
    anchored it as the first WHERE clause — but ``target`` resolution re-scopes
    its subquery on ``tenant_id`` so a name lookup never crosses the boundary.
    """
    if filters.audit_id is not None:
        stmt = stmt.where(AuditLog.id == filters.audit_id)

    if filters.agent_session_id is not None:
        stmt = stmt.where(AuditLog.agent_session_id == filters.agent_session_id)

    if filters.work_ref is not None:
        stmt = stmt.where(AuditLog.work_ref == filters.work_ref)

    if filters.principal is not None:
        escaped = _escape_like_literal(filters.principal)
        stmt = stmt.where(
            AuditLog.operator_sub.ilike(f"%{escaped}%", escape=_LIKE_ESCAPE),
        )

    if filters.since is not None:
        stmt = stmt.where(AuditLog.occurred_at >= filters.since)

    if filters.until is not None:
        stmt = stmt.where(AuditLog.occurred_at <= filters.until)

    if filters.target is not None:
        target_ids_subq = (
            sa.select(Target.id)
            .where(Target.tenant_id == tenant_id, Target.name == filters.target)
            .scalar_subquery()
        )
        stmt = stmt.where(AuditLog.target_id.in_(target_ids_subq))

    if filters.op_id is not None:
        like_pattern = _build_op_id_like_pattern(filters.op_id)
        payload_op_id = AuditLog.payload["op_id"].as_string()
        derived_op_id = (
            sa.literal("http.") + sa.func.lower(AuditLog.method) + sa.literal(":") + AuditLog.path
        )
        stmt = stmt.where(
            sa.or_(
                payload_op_id.like(like_pattern, escape=_LIKE_ESCAPE),
                derived_op_id.like(like_pattern, escape=_LIKE_ESCAPE),
            ),
        )

    if filters.op_class is not None:
        payload_op_class = AuditLog.payload["op_class"].as_string()
        stmt = stmt.where(payload_op_class == filters.op_class)

    if filters.result_status is not None:
        stmt = stmt.where(_result_status_predicate(filters.result_status))

    if filters.cursor is not None:
        pos = decode_cursor(filters.cursor)
        stmt = stmt.where(
            sa.or_(
                AuditLog.occurred_at < pos.ts,
                sa.and_(AuditLog.occurred_at == pos.ts, AuditLog.id < pos.id),
            ),
        )

    return stmt


def _result_status_predicate(result_status: str) -> sa.ColumnElement[bool]:
    """Translate the broadcast-shape result_status to a status_code predicate.

    Mirrors :func:`~meho_backplane.audit._classify_http_status` without the
    handler-exception arm (audit rows are post-fact; the exception bit is
    not stored). Unknown values resolve to ``FALSE`` — never an error,
    just an empty result, so a typo in the router surfaces as "no rows"
    rather than a 500.
    """
    if result_status == "ok":
        # 202 is the pending synthetic code (G11.2-T3/T4) — a 2xx, but
        # semantically "awaiting approval", not "ok". Exclude it so the
        # ``ok`` filter and the ``pending`` filter partition cleanly.
        return sa.and_(
            AuditLog.status_code < 400,
            AuditLog.status_code != 202,
        )
    if result_status == "pending":
        return AuditLog.status_code == 202
    if result_status == "denied":
        return AuditLog.status_code.in_([401, 403])
    if result_status == "error":
        return sa.and_(
            AuditLog.status_code >= 400,
            AuditLog.status_code.notin_([401, 403]),
        )
    return sa.false()


def _build_audit_entry(row: AuditLog, target_name: str | None) -> AuditEntry:
    """Construct an :class:`AuditEntry` from one audit-log row + denormalized name.

    Computes the three derived fields — ``op_id``, ``op_class``,
    ``result_status`` — exactly as the broadcast middleware would on the
    publish side, so a row returned by the audit-query API and a
    BroadcastEvent observed on the SSE feed for the same ``audit_id``
    agree on the trio.
    """
    payload = dict(row.payload) if row.payload else {}

    op_id_raw = payload.get("op_id")
    op_id = (
        op_id_raw
        if isinstance(op_id_raw, str) and op_id_raw
        else f"http.{row.method.lower()}:{row.path}"
    )

    op_class_raw = payload.get("op_class")
    op_class = (
        op_class_raw if isinstance(op_class_raw, str) and op_class_raw else classify_op(op_id)
    )

    # G0.15-T3 #1212 — surface ``principal_name`` when the writer landed
    # one in payload (MCP rows since #1212 carry ``Operator.name`` /
    # ``Operator.email`` derived from the JWT). HTTP-chassis rows remain
    # ``None`` because ``verify_jwt_and_bind`` does not bind ``name`` to
    # contextvars — fixing that is a separate, broader change. Same
    # ``isinstance(str)`` defence as ``op_class`` / ``op_id`` so a row
    # whose payload was hand-edited to a non-string value falls back to
    # ``None`` instead of raising at the validation layer.
    principal_name_raw = payload.get("principal_name")
    principal_name = (
        principal_name_raw if isinstance(principal_name_raw, str) and principal_name_raw else None
    )

    return AuditEntry(
        id=row.id,
        ts=row.occurred_at,
        tenant_id=row.tenant_id,
        principal_sub=row.operator_sub,
        principal_name=principal_name,
        target_id=row.target_id,
        target_name=target_name,
        method=row.method,
        path=row.path,
        status_code=row.status_code,
        request_id=row.request_id,
        duration_ms=row.duration_ms,
        payload=payload,
        op_id=op_id,
        op_class=op_class,
        result_status=_derive_result_status(row.status_code),
        parent_audit_id=row.parent_audit_id,
        agent_session_id=row.agent_session_id,
        work_ref=row.work_ref,
        broadcast_event_id=None,
    )


def _derive_result_status(status_code: int) -> str:
    """Map a stored ``status_code`` back to the broadcast-shape result_status.

    Audit rows do not carry the handler-exception bit, so the post-fact
    derivation collapses to: 401/403 → denied, 202 → pending (the
    G11.2-T3/T4 "awaiting approval" synthetic code), other 4xx/5xx →
    error, everything else → ok. The 202 → ``pending`` arm keeps the
    audit-query API and the broadcast feed in agreement for the same
    ``audit_id`` — the dispatcher publishes ``pending`` on the broadcast
    side, so the read path must derive the same rather than collapsing
    202 (a 2xx) to ``ok``.
    """
    if status_code in (401, 403):
        return "denied"
    if status_code == 202:
        return "pending"
    if status_code >= 400:
        return "error"
    return "ok"
