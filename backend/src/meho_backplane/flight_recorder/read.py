# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Flight-recorder tenant-scoped trace read (#3215, operator read surface).

The read counterpart to :mod:`meho_backplane.flight_recorder.store`. The store
(#3212) owns persistence and explicitly ships *no* read surface; this module is
the single tenant-scoped read the operator plane shares between its two fronts:

* the REST route ``GET /api/v1/audit/{id}/trace`` (:mod:`meho_backplane.api.v1.audit`);
* the console trace pane in the audit drawer
  (:mod:`meho_backplane.ui.routes.audit`).

Both surfaces call :func:`load_trace` with the same ``(audit_id, tenant_id)``
pair, so the tenant boundary is enforced in **one** place. The function is
strictly read-only: it never re-processes, never un-redacts, and has no write
path. It renders exactly what the capture/redaction seams (#3213/#3214) already
stored -- bodies are redacted and capped at capture time, and the header-level
``redaction_uncertain`` flag (F5) is surfaced verbatim so an operator can see
when a trace was withheld from the agent handle.

Access posture (per ``docs/decisions/dispatch-flight-recorder.md``, F5): the
operator plane keeps **full** trace access -- including redaction-uncertain
traces -- independent of the agent gate. This module is the operator-plane read;
it applies no agent-side degrade, only the tenant scope.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.db.models import DispatchTrace, DispatchTraceSpan

__all__ = ["TraceSpanView", "TraceView", "load_trace"]


class TraceSpanView(BaseModel):
    """One ordered span of a dispatch trace, safe to serve to the operator.

    Mirrors :class:`~meho_backplane.db.models.DispatchTraceSpan` field-for-field
    -- the frequently-queried axes (:attr:`status`, :attr:`duration_ms`) are
    typed columns, and the redacted/capped span detail (method, URL, redacted
    headers, the redacted body + truncation marker, JSONFlux input/kept/output
    sizes, the result-handle id) rides the free-form :attr:`attributes` mapping.
    The read surface renders these as stored -- it never re-processes them.
    """

    model_config = ConfigDict(frozen=True)

    seq: int
    span_kind: str
    name: str
    started_at: datetime
    duration_ms: Decimal | None
    status: str | None
    attributes: dict[str, Any]


class TraceView(BaseModel):
    """A dispatch trace header plus its ordered spans, tenant-scoped.

    Returned by :func:`load_trace` when a trace exists for the ``(audit_id,
    tenant_id)`` pair. :attr:`redaction_uncertain` is the F5 degrade flag read
    straight off the header: ``True`` means the capture/redaction seam could not
    prove the trace fully redacted, so it was withheld from the agent handle and
    is readable on the operator plane alone. The operator surface surfaces the
    flag rather than acting on it -- an operator sees everything, and sees which
    traces the agent could not.
    """

    model_config = ConfigDict(frozen=True)

    trace_id: uuid.UUID
    audit_id: uuid.UUID
    tenant_id: uuid.UUID
    created_at: datetime
    expires_at: datetime
    redaction_uncertain: bool
    spans: list[TraceSpanView]


async def load_trace(
    session: AsyncSession,
    *,
    audit_id: uuid.UUID,
    tenant_id: uuid.UUID,
) -> TraceView | None:
    """Load the trace for ``(audit_id, tenant_id)``; ``None`` when none exists.

    Two tenant-scoped reads: the header (unique on ``audit_id``, filtered on
    ``tenant_id`` as defence in depth even though a trace is only ever written
    for the owning tenant), then its spans ordered by ``seq``. Returning
    ``None`` for a missing trace is distinct from a missing *audit row*: the
    caller decides the surface semantics (the REST route 404s only when the
    audit row is absent/cross-tenant, and renders an empty-trace state when the
    audit row exists but carries no trace).

    The ``tenant_id`` filter on the header is the load-bearing isolation guard:
    a trace is never returned for a tenant that does not own it, regardless of
    how the caller resolved ``audit_id``.
    """
    header = (
        await session.execute(
            select(DispatchTrace).where(
                DispatchTrace.audit_id == audit_id,
                DispatchTrace.tenant_id == tenant_id,
            )
        )
    ).scalar_one_or_none()
    if header is None:
        return None

    span_rows = (
        (
            await session.execute(
                select(DispatchTraceSpan)
                .where(DispatchTraceSpan.trace_id == header.id)
                .order_by(DispatchTraceSpan.seq.asc())
            )
        )
        .scalars()
        .all()
    )

    return TraceView(
        trace_id=header.id,
        audit_id=header.audit_id,
        tenant_id=header.tenant_id,
        created_at=header.created_at,
        expires_at=header.expires_at,
        redaction_uncertain=header.redaction_uncertain,
        spans=[
            TraceSpanView(
                seq=span.seq,
                span_kind=span.span_kind,
                name=span.name,
                started_at=span.started_at,
                duration_ms=span.duration_ms,
                status=span.status,
                attributes=dict(span.attributes),
            )
            for span in span_rows
        ],
    )
