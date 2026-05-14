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

* ``principal_name`` — the chassis HTTP audit middleware
  (``meho_backplane.audit``) and the MCP audit writer
  (``meho_backplane.mcp.audit``) do not capture the operator's ``name`` JWT
  claim at write time. The :class:`~meho_backplane.broadcast.events.BroadcastEvent`
  side has a nullable ``principal_name`` field, but it is populated only on
  the MCP path (``mcp/handlers.py``), and the audit row itself never carries
  it. Always None in v0.2; a future small task on the write path closes this.
* ``parent_audit_id`` — composite-operation lineage column landed by
  G0.6-T7 (#398, OPEN). Until that task ships the column + populates it
  from the dispatcher's recursion, this field stays None and the filter
  on ``parent_audit_id`` raises :class:`UnsupportedFilterError`.
* ``agent_session_id`` — referenced in the Task body but absent from any
  current or planned schema work. Always None until a Goal proposes the
  column. Filter raises :class:`UnsupportedFilterError`.
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
    * ``principal_name`` / ``parent_audit_id`` / ``agent_session_id`` /
      ``broadcast_event_id`` — v0.2 placeholders, always None
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
    broadcast_event_id: uuid.UUID | None


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
