# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Request/response models for the audit-query REST surface (G8.1-T2).

The single non-trivial model is :class:`AuditQueryRequest` — the POST
body for ``/api/v1/audit/query``. It mirrors
:class:`~meho_backplane.audit_query.AuditQueryFilters` from the T1
substrate but accepts ``since`` / ``until`` as strings so operators can
pass shorthand (``"24h"`` / ``"7d"``) instead of ISO-8601. The router
converts them via :func:`~meho_backplane.audit_query.parse_duration`
before dispatch.

The response models (:class:`AuditEntry` / :class:`AuditQueryResult`)
are re-exported from the substrate without modification so OpenAPI
surfaces them under the audit-query tag without duplicating the schema.

:class:`AuditReplayResult` is the 200 body for the per-session replay
route (G8.2-T4) — a ``ReplayNode`` forest plus the echoed
``session_id`` / ``tenant_id`` and the session's ``row_count``. It is
defined here (not re-exported from the substrate) because it is a
REST-surface envelope around the substrate's :class:`ReplayNode`, the
same layering :class:`AuditQueryRequest` already follows.

Tenant scoping by construction
==============================

:class:`AuditQueryRequest` deliberately has **no** ``tenant_id`` field
and sets ``extra="forbid"`` (G0.9-T2 / #729) so a client that puts
``tenant_id`` in the body fails 422 ``extra_forbidden`` instead of
having the value silently dropped. The router never reads from the
body for the tenant boundary — it injects ``operator.tenant_id`` from
the JWT into the T1 handler call. The substrate's mandatory
keyword-only ``tenant_id`` argument enforces the invariant on its side.
The fail-loud posture matches every other public v1 request schema.
"""

from __future__ import annotations

import uuid

from pydantic import BaseModel, ConfigDict, Field

from meho_backplane.audit_query import AuditEntry, AuditQueryResult, ReplayNode
from meho_backplane.flight_recorder import TraceSpanView, TraceView

__all__ = [
    "AuditEntry",
    "AuditQueryRequest",
    "AuditQueryResult",
    "AuditReplayResult",
    "AuditTraceResult",
    "ReplayNode",
    "TraceSpanView",
    "TraceView",
]


class AuditQueryRequest(BaseModel):
    """POST body for ``/api/v1/audit/query``.

    Mirrors :class:`~meho_backplane.audit_query.AuditQueryFilters`
    except ``since`` / ``until`` are strings parsed by
    :func:`~meho_backplane.audit_query.parse_duration` at the router
    layer. All other fields pass through to the substrate filter
    object unchanged. The empty body shape ``{}`` is the no-filter
    case — every field defaults to None or the substrate-side default.

    ``extra="forbid"`` rejects unknown fields with 422
    ``extra_forbidden`` so a typo or a client passing ``tenant_id``
    in the body fails loud at the framework boundary — the route
    always uses ``operator.tenant_id`` from the JWT, never the body.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    target: str | None = Field(default=None, max_length=256)
    principal: str | None = Field(default=None, max_length=256)
    op_id: str | None = Field(default=None, max_length=256)
    op_class: str | None = Field(default=None, max_length=64)
    result_status: str | None = Field(default=None, max_length=16)
    since: str | None = Field(default=None, max_length=32)
    until: str | None = Field(default=None, max_length=32)
    audit_id: uuid.UUID | None = None
    parent_audit_id: uuid.UUID | None = None
    agent_session_id: uuid.UUID | None = None
    work_ref: str | None = Field(default=None, max_length=256)
    limit: int = Field(default=100, ge=1, le=1000)
    cursor: str | None = Field(default=None, max_length=512)


class AuditReplayResult(BaseModel):
    """200 body for ``GET /api/v1/audit/sessions/{session_id}/replay``.

    Wraps the substrate's :class:`ReplayNode` forest with the echoed
    request identity. ``tenant_id`` is always the operator's tenant
    (lifted from the JWT by the route), never client-supplied, so the
    echo is a confirmation of the boundary the query ran under — not a
    value the caller chose.

    ``row_count`` is the count of *anchor* rows in the session — rows
    whose ``agent_session_id`` equals ``session_id`` within the
    operator's tenant. It is the same number the route's count-first
    413 guard evaluates, so a session that returns 200 reports a
    ``row_count`` identical to what its over-cap sibling would report
    at 413. NULL-session lineage children pulled into ``root`` by the
    replay closure (a composite ``dispatch_child`` whose own
    ``agent_session_id`` is NULL but whose ``parent_audit_id`` links
    into the session) are present in the tree but are not counted —
    "session rows" are defined by the ``agent_session_id`` anchor, not
    by tree membership.

    An unknown session id, or one belonging to another tenant, yields
    ``root=[]`` / ``row_count=0`` — never a 404 — so a foreign session
    is indistinguishable from an empty one and existence never leaks
    across tenants (the same non-leakage posture
    ``GET /show/{audit_id}`` takes).

    ``excluded_null_session_count`` (#2700) is the tenant-scoped tally
    of ``method=MCP`` audit rows whose ``agent_session_id`` is NULL —
    calls from clients that never negotiated a session via the
    ``initialize`` handshake. Such rows can never anchor or be walked by
    *any* session replay, so a ``row_count=0`` response with a non-zero
    ``excluded_null_session_count`` tells an investigator the empty
    forest is not the same as an empty history: there is un-negotiated
    MCP traffic this forensic surface structurally cannot see. It is a
    tenant-wide count, independent of ``session_id`` (the un-negotiated
    rows belong to no session), and mirrors the honest
    ``when_negotiated`` capture label the deploy's ``/status`` reports.

    Frozen like the substrate models it carries.
    """

    model_config = ConfigDict(frozen=True)

    root: list[ReplayNode]
    session_id: uuid.UUID
    tenant_id: uuid.UUID
    row_count: int
    excluded_null_session_count: int


class AuditTraceResult(BaseModel):
    """200 body for ``GET /api/v1/audit/{audit_id}/trace`` (#3215).

    The operator read surface of the dispatch flight recorder
    (``docs/decisions/dispatch-flight-recorder.md``). Wraps the tenant-scoped
    :class:`~meho_backplane.flight_recorder.TraceView` with the echoed
    ``audit_id`` and a ``trace_present`` discriminator, the same REST-envelope
    layering :class:`AuditReplayResult` follows over the substrate's
    :class:`ReplayNode`.

    Two-level absence semantics -- distinct on purpose:

    * The **audit row** being missing or in another tenant is a 404 from the
      route, matching ``GET /show/{audit_id}``: existence never leaks across
      the tenant boundary.
    * The audit row existing but carrying **no captured trace** is *not* a 404 --
      it is a 200 with ``trace_present=False`` and ``trace=None``. Capture is a
      per-tenant/per-target opt-in and best-effort (F1/F7), so "no trace" is a
      normal state an operator must be able to observe, not an error. The
      console pane renders a "no trace captured" empty state for this case.

    ``trace.redaction_uncertain`` surfaces the F5 degrade flag so an operator
    (and a paired add-on reading over this route with its operator principal)
    can see when a trace was withheld from the agent handle. The operator plane
    keeps **full** access regardless of that flag.

    Frozen like the models it carries.
    """

    model_config = ConfigDict(frozen=True)

    audit_id: uuid.UUID
    trace_present: bool
    trace: TraceView | None = None
