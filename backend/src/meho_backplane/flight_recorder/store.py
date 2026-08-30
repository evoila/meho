# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Flight-recorder internal persistence API (#3212, F6).

The minimal, internal write API for the trace store. The capture seam (#3214)
produces the spans (redacted + capped by the #3213 engine) and calls
:func:`record_trace` to persist them; this module owns only the persistence,
the retention-deadline stamp, and the F7 best-effort contract. There is no
read surface here -- the operator + agent read paths are sibling Tasks.

F7 invariant (best-effort, cannot fail or slow a dispatch): :func:`record_trace`
opens its **own** session, decoupled from the dispatch's transaction, and
**swallows every error**, returning ``None`` instead of raising. It follows the
fail-open discipline the result-handle spill already proves
(:mod:`meho_backplane.connectors.result_handle_store` -- "a reduce must never
fail because the spill backend is unreachable"); a trace write must never fail
because the trace store is unreachable.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal

import structlog

from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import DispatchTrace, DispatchTraceSpan
from meho_backplane.flight_recorder.config import compute_expires_at, resolve_retention_days

__all__ = ["SpanInput", "record_trace"]

_log = structlog.get_logger(__name__)


@dataclass(frozen=True, slots=True)
class SpanInput:
    """One captured span to persist under a trace (the seam's input shape).

    The seam is responsible for redaction (#3213) and caps (#3214) *before*
    building a :class:`SpanInput`; this API stores the values as given. The
    frequently-queried axes (:attr:`status`, :attr:`duration_ms`) are typed
    columns; everything else -- method, URL, redacted headers, the
    redacted/capped body, JSONFlux input/kept/output sizes, the result-handle
    id -- goes in the free-form :attr:`attributes` mapping.
    """

    #: One of ``vendor_call`` / ``composite_step`` / ``jsonflux_reduction`` /
    #: ``typed`` / ``opaque`` (the decision's enumerated span kinds).
    span_kind: str
    #: Human label (``"GET /rest/vm"``, ``"jsonflux.reduce"``, a sub-step name).
    name: str
    #: Span start time (UTC).
    started_at: datetime
    #: Span duration in milliseconds; ``None`` when not measured.
    duration_ms: Decimal | None = None
    #: HTTP status code (as text) or ``"ok"`` / ``"error"``; ``None`` if N/A.
    status: str | None = None
    #: Redacted, capped span detail. Inner shape owned by the seam.
    attributes: Mapping[str, object] = field(default_factory=dict)


async def record_trace(
    *,
    audit_id: uuid.UUID,
    tenant_id: uuid.UUID,
    spans: Sequence[SpanInput],
    redaction_uncertain: bool = False,
    now: datetime | None = None,
) -> uuid.UUID | None:
    """Persist one trace header + its ordered spans; return the new trace id.

    Resolves the tenant's retention window (F4), stamps
    ``expires_at = created_at + retention_days`` on the header, and writes the
    header plus ``spans`` (ordered by their position in the sequence -> ``seq``
    ``0..n-1``) in a single transaction on a dedicated session.

    F7: best-effort. Any error (retention resolution, DB write, serialization)
    is logged and swallowed, and the function returns ``None`` -- it never
    raises into the caller's dispatch path.

    Parameters
    ----------
    audit_id:
        The dispatch's ``audit_log.id`` -- the soft-FK the trace is referenced
        by. The ``audit_log`` row itself is never modified.
    tenant_id:
        Owning tenant; drives the retention-window resolution.
    spans:
        Ordered spans to persist. May be empty (a header-only trace is valid).
    redaction_uncertain:
        F5 degrade flag: ``True`` marks the trace withheld from the agent
        read handle (operator-only). Set by the capture/redaction seam.
    now:
        Capture time; defaults to ``datetime.now(UTC)``. Injectable for tests.
    """
    try:
        created_at = now if now is not None else datetime.now(UTC)
        retention_days = await resolve_retention_days(tenant_id)
        expires_at = compute_expires_at(created_at, retention_days)
        trace_id = uuid.uuid4()
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session:
            session.add(
                DispatchTrace(
                    id=trace_id,
                    audit_id=audit_id,
                    tenant_id=tenant_id,
                    created_at=created_at,
                    expires_at=expires_at,
                    redaction_uncertain=redaction_uncertain,
                )
            )
            for seq, span in enumerate(spans):
                session.add(
                    DispatchTraceSpan(
                        id=uuid.uuid4(),
                        trace_id=trace_id,
                        seq=seq,
                        span_kind=span.span_kind,
                        name=span.name,
                        started_at=span.started_at,
                        duration_ms=span.duration_ms,
                        status=span.status,
                        attributes=dict(span.attributes),
                    )
                )
            await session.commit()
        return trace_id
    except Exception:
        # F7: capture is best-effort and can never fail or slow a dispatch.
        _log.warning(
            "flight_recorder_record_trace_failed",
            audit_id=str(audit_id),
            tenant_id=str(tenant_id),
            exc_info=True,
        )
        return None
