# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Pydantic schemas for the audit-query substrate.

Three consumer-facing models — :class:`AuditQueryFilters` (input),
:class:`AuditEntry` (one row), :class:`AuditQueryResult` (page of rows +
next-cursor). All frozen via ``ConfigDict(frozen=True)`` so a row handed to a
caller cannot mutate after construction.

Substrate-vs-issue-body reconciliation
======================================

The G8.1 Initiative (#334) and Task body (#465) advertise a richer
:class:`AuditEntry` shape than the ``audit_log`` table actually carries today.
The substrate ships the consumer-facing shape so T2 (REST) / T3 (CLI) / T4
(MCP) dispatch through a stable surface, but several fields are not yet
backed by real columns and are populated as :data:`None` until follow-up
tasks land the schema additions:

* ``principal_name`` — partially populated as of G0.15-T3 #1212. The MCP
  audit writer (``meho_backplane.mcp.audit.write_mcp_audit_row``) now
  merges ``Operator.name`` and ``Operator.email`` (both JWT-derived) into
  ``audit_log.payload`` under the keys ``principal_name`` /
  ``principal_email``, and ``_build_audit_entry`` reads
  ``payload['principal_name']`` onto this field. The chassis HTTP audit
  middleware (``meho_backplane.audit``) still does not bind the name claim
  to contextvars and continues to leave the field None on HTTP-method
  rows; closing that gap is a separate follow-up.
* ``parent_audit_id`` — composite-operation lineage column landed by
  G0.6-T7 (#398) via migration ``0006``. Populated on the returned row from
  ``audit_log.parent_audit_id`` (G8.2-T3 #1011); the *flat filter* on
  ``parent_audit_id`` still raises :class:`UnsupportedFilterError` (un-gating
  it is out of scope for #377).
* ``agent_session_id`` — the MCP-session correlation column landed by
  G8.2-T1 (#1009) via migration ``0014``. Populated on the returned row from
  ``audit_log.agent_session_id``; the filter is un-gated (G8.2-T3 #1011) and
  drives the per-session replay query
  (:func:`~meho_backplane.audit_query.replay.replay_session`).
* ``broadcast_event_id`` — the FK direction is the reverse of what the
  Task body implies: :class:`BroadcastEvent.audit_id` points at the audit
  row, not vice versa. Always None.

The three computed fields — ``op_id``, ``op_class``, ``result_status`` — are
derived at query time using the same logic the broadcast middleware uses on
the publish side (``meho_backplane.audit._publish_broadcast_event``,
``meho_backplane.broadcast.events.classify_op``,
``meho_backplane.audit._classify_http_status``). MCP-written rows carry
``op_id`` / ``op_class`` inside the ``payload`` JSON dict (see
``mcp/handlers.py:214-221``); HTTP-written rows derive them from
``f"http.{method.lower()}:{path}"``.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "AuditEntry",
    "AuditQueryFilters",
    "AuditQueryResult",
    "MyRecentPage",
    "ReplayNode",
]


class AuditQueryFilters(BaseModel):
    """Filters for the ``query_audit`` handler.

    ``tenant_id`` is **not** on this model — it is a separate mandatory
    argument to :func:`~meho_backplane.audit_query.query.query_audit` so it
    can never be supplied by an operator-controllable input. The handler
    injects it from the validated JWT.

    Glob semantics: ``op_id`` accepts shell-style ``*`` wildcards
    (``"vsphere.vm.*"``), which the query handler translates to SQL ``LIKE``
    with ``%`` substitution. ``?`` and bracket expressions are not supported
    in v0.2 — the consumer use case is prefix matching, not arbitrary glob.

    Duration shorthand (``"24h"`` / ``"7d"``) is **not** parsed here — that
    belongs in the T2 / T3 router layer. T1 accepts absolute :class:`datetime`
    values only; the router parses the shorthand into an absolute timestamp
    before constructing the filter object.
    """

    model_config = ConfigDict(frozen=True)

    target: str | None = None
    principal: str | None = None
    op_id: str | None = None
    op_class: str | None = None
    result_status: str | None = None
    since: datetime | None = None
    until: datetime | None = None
    audit_id: uuid.UUID | None = None
    parent_audit_id: uuid.UUID | None = None
    agent_session_id: uuid.UUID | None = None
    work_ref: str | None = None
    limit: int = Field(default=100, ge=1, le=1000)
    cursor: str | None = None


class AuditEntry(BaseModel):
    """One row of the audit query result.

    Field-to-column mapping (see module docstring for the substrate
    reconciliation):

    * ``id`` ← ``audit_log.id``
    * ``ts`` ← ``audit_log.occurred_at``
    * ``tenant_id`` ← ``audit_log.tenant_id``
    * ``principal_sub`` ← ``audit_log.operator_sub``
    * ``target_id`` ← ``audit_log.target_id``
    * ``target_name`` ← LEFT JOIN ``targets.name`` ON ``audit_log.target_id``
    * ``method`` / ``path`` / ``status_code`` / ``request_id`` / ``duration_ms``
      / ``payload`` ← columns of the same name
    * ``op_id`` / ``op_class`` / ``result_status`` — computed at query time
    * ``parent_audit_id`` ← ``audit_log.parent_audit_id`` (lineage; #398)
    * ``agent_session_id`` ← ``audit_log.agent_session_id`` (MCP session; #1009)
    * ``work_ref`` ← ``audit_log.work_ref`` (external change-ticket reference;
      work_ref I1-T1 #1655). NULL until a bind source lands (I1-T2); the flat
      ``work_ref`` filter on :class:`AuditQueryFilters` is exact-match.
    * ``principal_name`` ← ``payload['principal_name']`` when set. The MCP
      audit-write helper (``write_mcp_audit_row``) populates it from
      ``Operator.name`` since G0.15-T3 #1212; HTTP-chassis rows remain
      None pending a separate fix to bind ``name`` to contextvars in
      ``verify_jwt_and_bind``.
    * ``broadcast_event_id`` — v0.2 placeholder, always None (FK direction
      is reversed: ``BroadcastEvent.audit_id`` points at the audit row).
    """

    model_config = ConfigDict(frozen=True)

    id: uuid.UUID
    ts: datetime
    tenant_id: uuid.UUID | None
    principal_sub: str
    principal_name: str | None
    target_id: uuid.UUID | None
    target_name: str | None
    method: str
    path: str
    status_code: int
    request_id: uuid.UUID | None
    duration_ms: Decimal | None
    payload: dict[str, Any]
    op_id: str
    op_class: str
    result_status: str
    parent_audit_id: uuid.UUID | None
    agent_session_id: uuid.UUID | None
    work_ref: str | None
    # Policy-gate verdict stamped on the row (#130): ``auto-execute`` /
    # ``needs-approval`` / ``deny``, or ``None`` on rows where no gate ran
    # (pre-#130 rows, pre-gate usage errors, system-internal writers). Sourced
    # from the real ``audit_log.policy_decision`` column, so a consumer reads
    # the verdict directly instead of joining ``method``+``path`` + parsing
    # ``payload``.
    policy_decision: str | None
    broadcast_event_id: uuid.UUID | None


class ReplayNode(AuditEntry):
    """One node of a per-session audit-replay tree (G8.2-T3).

    Subclasses :class:`AuditEntry` so it carries every audit field verbatim —
    forward-compatible with the v0.2.next compliance-export contract, which
    treats a replay node as an audit row plus its position in the session
    graph. Two structural fields are added:

    * ``depth`` — distance from the session root. ``0`` for roots; assigned
      during tree assembly in :func:`~meho_backplane.audit_query.replay.replay_session`.
    * ``children`` — the node's direct children, ordered by ``(occurred_at,
      id)``. Self-referential — the forward reference is resolved by the
      module-level :func:`ReplayNode.model_rebuild` call below.

    Frozen like its parent: a node handed to a caller cannot mutate after the
    tree is built.
    """

    model_config = ConfigDict(frozen=True)

    depth: int
    children: list[ReplayNode] = Field(default_factory=list)


# Resolve the ``list[ReplayNode]`` self-reference now that the class exists.
ReplayNode.model_rebuild()


class AuditQueryResult(BaseModel):
    """Page of audit rows plus the forward-only continuation cursor.

    ``next_cursor`` is :data:`None` when fewer than ``limit`` rows were
    available (the query reached the end of the matching set). Consumers
    iterate by re-issuing the same filter with ``cursor = next_cursor``
    until ``next_cursor`` is None.
    """

    model_config = ConfigDict(frozen=True)

    rows: list[AuditEntry]
    next_cursor: str | None


class MyRecentPage(BaseModel):
    """Unified list envelope for ``GET /api/v1/audit/my-recent``.

    The `{items, next_cursor}` shape codified in
    ``docs/codebase/api-shape-conventions.md`` §2. It carries the same
    audit rows and forward-only cursor as :class:`AuditQueryResult`, but
    names the list field ``items`` (not ``rows``) so the reference list
    endpoints agree on one envelope. The sibling audit-query endpoints
    (``/query`` / ``/who-touched`` / ``/by-work-ref``) keep the
    :class:`AuditQueryResult` ``rows`` shape and are out of the §2
    reference set.
    """

    model_config = ConfigDict(frozen=True)

    items: list[AuditEntry]
    next_cursor: str | None = None
