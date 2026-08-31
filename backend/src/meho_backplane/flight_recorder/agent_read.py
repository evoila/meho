# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Flight-recorder **agent** read surface (#3216, F5).

Implements F5 of the decision of record
``docs/decisions/dispatch-flight-recorder.md`` -- the operator override that
makes a captured trace readable by an agent, but *only* through the existing
narrow-waist result-handle idiom, per-tenant gated, with a per-trace
redaction-uncertainty degrade.

The access path (design choice, grounded in the decision + postulates 5/6)
==========================================================================

An agent obtains a trace handle **exactly the way it obtains any result
handle**: as a :class:`~meho_backplane.connectors.schemas.ResultHandle` the
backplane surfaces on the dispatch response envelope, then pages it via the
**unchanged** ``result_query`` meta-tool
(:func:`meho_backplane.operations.result_query.read_result_window`). There is
**no new tool** on the agent surface, **no vendor-specific name**, and **no
raw payload** in agent context -- the agent pages the ordered spans exactly as
it pages any set-shaped result (postulates 5 and 6 intact;
``docs/codebase/mcp.md`` and the surface conformance test are untouched).

This module owns the **mint**: :func:`materialize_agent_trace_handle` takes a
persisted trace (written by :func:`meho_backplane.flight_recorder.store.record_trace`,
which the capture seam #3214 calls), applies the per-tenant agent gate (F5,
:func:`meho_backplane.flight_recorder.config.should_expose_to_agent`) and the
redaction-uncertainty degrade, and -- only when both permit -- spills the
ordered spans into the same Valkey read-back store the JSONFlux reducer uses
(:class:`~meho_backplane.connectors.result_handle_store.ResultHandleStore`),
scoped to ``(tenant_id, operator_sub)`` with a bounded TTL, returning the
``ResultHandle`` the agent pages.

The mint's **live trigger** -- the recorder attaching the returned handle to
the dispatch response envelope after ``record_trace`` -- belongs to the
capture seam (#3214), which owns the recorder's best-effort path (F7). This
module does **not** import capture: the boundary is the same sibling-task seam
:func:`record_trace` already documents. F7 decouples the trace write from the
dispatch result path, so the handle is produced on the recorder path (where
``record_trace`` runs), never by slowing the synchronous dispatch return.

The degrade is the load-bearing security property
=================================================

The operator's F5 condition (*"as long as there are no secrets in there"*) is
discharged by the composition of the #3213 fail-closed redaction (span bodies
are redacted + capped at capture) **plus** this degrade: any trace whose
:attr:`~meho_backplane.db.models.DispatchTrace.redaction_uncertain` flag is set
is withheld from the agent handle **entirely** -- checked *before* any span is
loaded or spilled, so a doubtful (potentially secret-bearing) trace can never
reach an agent-visible handle. The operator plane still reads it. The default
on doubt is *less* agent exposure, never more.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, Final

import msgspec
import structlog
from sqlalchemy import select

from meho_backplane.auth.operator import Operator
from meho_backplane.connectors.result_handle_store import (
    ResultHandleStore,
    get_result_handle_store,
)
from meho_backplane.connectors.schemas import (
    FetchMore,
    FetchMoreDrillIn,
    FetchMoreNativePagination,
    OperationResult,
    ResultHandle,
)
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import DispatchTrace, DispatchTraceSpan
from meho_backplane.flight_recorder.config import should_expose_to_agent
from meho_backplane.settings import get_settings

__all__ = [
    "AGENT_TRACE_HANDLE_EXTRA_KEY",
    "attach_agent_trace_handle",
    "materialize_agent_trace_handle",
]

_log = structlog.get_logger(__name__)

#: Key under which the recorder's live trigger surfaces the agent trace handle
#: on the dispatch response envelope (:attr:`OperationResult.extras`). A dict --
#: the JSON-serialized :class:`ResultHandle` -- so the value stays wire-safe in
#: the free-form ``extras`` bag; the agent pages it via ``result_query`` exactly
#: as it pages the sibling data handle. Not a new tool, not a new typed field.
AGENT_TRACE_HANDLE_EXTRA_KEY: Final[str] = "flight_recorder_trace_handle"

#: TTL for a spilled trace handle. Mirrors the production JsonFluxReducer's
#: default ``ttl_seconds`` (``main.py`` installs ``JsonFluxReducer()`` via
#: ``set_default_reducer``), so a trace handle expires on the **same** server-
#: enforced schedule as any reduced result -- no TTL drift from
#: ResultHandleStore semantics.
_TRACE_HANDLE_TTL_SECONDS: Final[int] = 3600

#: Inline-sample upper bound (rows), mirroring the reducer's default
#: ``sample_size``. The sample is *also* byte-bounded (see
#: :func:`_bounded_sample`) -- a span body can be up to F3's 64 KB cap, so a
#: count-only bound could ship a heavy inline preview into agent context.
_SAMPLE_SIZE: Final[int] = 5

#: op_id label stamped on the spilled payload's metadata (store logs only).
#: NOT a tool name and NOT a vendor identifier -- it never appears in
#: ``tools/list`` or on the agent surface.
_TRACE_OP_ID: Final[str] = "flight_recorder.trace"

#: The read-back meta-tool an agent pages the handle with. The existing
#: working-surface tool -- named here only for the self-documenting
#: ``fetch_more`` envelope, exactly as the reducer names it.
_RESULT_QUERY_TOOL: Final[str] = "result_query"

#: Static JSON Schema of one materialized span row. The trace is a set of
#: these; ``result_query`` pages them.
_SPAN_ROW_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "seq": {"type": "integer"},
        "span_kind": {"type": "string"},
        "name": {"type": "string"},
        "started_at": {"type": ["string", "null"]},
        "duration_ms": {"type": ["string", "null"]},
        "status": {"type": ["string", "null"]},
        "attributes": {"type": "object"},
    },
}


def _span_row(span: DispatchTraceSpan) -> dict[str, Any]:
    """Materialize one persisted span as a JSON-safe row.

    ``attributes`` is already redacted + capped (the #3213 engine scrubbed it
    at capture time); this read path stores it verbatim. ``duration_ms`` (a
    ``Decimal``) is serialized as a string to stay JSON-safe and lossless.
    """
    return {
        "seq": span.seq,
        "span_kind": span.span_kind,
        "name": span.name,
        "started_at": span.started_at.isoformat() if span.started_at is not None else None,
        "duration_ms": None if span.duration_ms is None else str(span.duration_ms),
        "status": span.status,
        "attributes": dict(span.attributes) if span.attributes else {},
    }


def _bounded_sample(rows: list[dict[str, Any]], *, byte_budget: int) -> list[dict[str, Any]]:
    """Inline preview bounded by BOTH a row count and a serialized-byte budget.

    Mirrors the JSONFlux reducer's postulate-6 discipline: the inline sample is
    sized to a byte budget, not only a count. A trace span body is capped at
    F3's 64 KB, so a count-only bound could ship hundreds of KB of preview into
    agent context; this keeps the sample small and pushes the full ordered set
    to ``result_query``. The first row always ships (a non-empty trace never
    yields an empty preview), then rows accrue until the next would exceed the
    budget.
    """
    sample: list[dict[str, Any]] = []
    used = 0
    for row in rows[:_SAMPLE_SIZE]:
        encoded = len(msgspec.json.encode(row))
        if sample and used + encoded > byte_budget:
            break
        sample.append(row)
        used += encoded
    return sample


def _build_handle(
    *,
    handle_id: uuid.UUID,
    audit_id: uuid.UUID,
    sample_rows: list[dict[str, Any]],
    total_rows: int,
    stored_rows: int,
    minted_at: datetime,
) -> ResultHandle:
    """Assemble the ``ResultHandle`` the agent pages, mirroring the reducer.

    The drill-in branch is ``available=True`` naming ``result_query`` and a
    first-page ``example_call``, exactly as the JSONFlux reducer does for any
    spilled set; native pagination is ``available=False`` (a trace has none).
    """
    return ResultHandle(
        handle_id=handle_id,
        summary_md=(
            f"Flight-recorder trace: {total_rows} span(s) captured for dispatch "
            f"{audit_id}. Bodies redacted and capped at capture; page the full "
            "ordered set via `result_query`."
        ),
        schema_=_SPAN_ROW_SCHEMA,
        total_rows=total_rows,
        sample_rows=tuple(sample_rows),
        ttl_seconds=_TRACE_HANDLE_TTL_SECONDS,
        fetch_more=FetchMore(
            drill_in=FetchMoreDrillIn(
                available=True,
                rationale=(
                    f"Full trace ({stored_rows} of {total_rows} span(s)) is spilled; "
                    f"page it with {_RESULT_QUERY_TOOL}(handle_id={handle_id})."
                ),
                mcp_tool=_RESULT_QUERY_TOOL,
                example_call={
                    "tool": _RESULT_QUERY_TOOL,
                    "args": {"handle_id": str(handle_id), "offset": 0, "limit": 50},
                },
                expires_at=minted_at + timedelta(seconds=_TRACE_HANDLE_TTL_SECONDS),
            ),
            native_pagination=FetchMoreNativePagination(
                available=False,
                rationale=(
                    "A flight-recorder trace has no native pagination; page it "
                    "via the result handle."
                ),
            ),
        ),
    )


async def _load_agent_trace_rows(
    *, tenant_id: uuid.UUID, audit_id: uuid.UUID
) -> list[dict[str, Any]] | None:
    """Load the ordered, agent-visible span rows for one trace, or ``None``.

    Returns ``None`` (withhold) when no trace exists for
    ``(audit_id, tenant_id)`` -- the load is **tenant-scoped**, so another
    tenant's trace never matches and a cross-tenant read is a miss, not a leak
    -- or when the trace is **redaction-uncertain**. The uncertainty degrade
    (F5) is checked *before* any span row is materialized, so a doubtful
    (potentially secret-bearing) trace never has its spans read here at all.
    """
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
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
        # F5 redaction-uncertainty degrade: withhold ENTIRELY, BEFORE any span
        # is loaded. The load-bearing property -- a doubtful trace can never
        # reach an agent-visible handle. Operator plane keeps access.
        if header.redaction_uncertain:
            return None
        spans = list(
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
    return [_span_row(span) for span in spans]


async def materialize_agent_trace_handle(
    *,
    operator: Operator,
    audit_id: uuid.UUID,
    store: ResultHandleStore | None = None,
    now: datetime | None = None,
) -> ResultHandle | None:
    """Mint the agent-readable ``ResultHandle`` for a captured trace (F5).

    Returns the handle the agent pages via ``result_query``, or ``None`` when
    the trace is withheld from the agent surface. ``None`` -- never a raised
    error -- for every withhold/failure case, so the operator plane is
    unaffected and the safe default on doubt is *less* agent exposure:

    * the operator has no tenant (it can never own a spilled handle);
    * the per-tenant agent gate is off (F5,
      :func:`should_expose_to_agent`) -- checked first, so a gated-off tenant's
      spans are never even loaded;
    * no trace exists for ``(audit_id, operator.tenant_id)`` -- which also
      makes a cross-tenant read a miss (the load is tenant-scoped);
    * the trace is **redaction-uncertain** -- the F5 degrade: withheld
      entirely, checked *before* any span is loaded or spilled;
    * the spill was skipped or failed (empty trace, store unreachable) -- the
      store is fail-open, and the operator plane still has the trace.

    On success the ordered spans are spilled to the read-back store keyed by
    ``(tenant_id, operator_sub)`` with a bounded TTL -- the exact scoping
    :func:`~meho_backplane.operations.result_query.read_result_window` enforces
    on the read side, so a handle minted for one operator in one tenant is
    unreadable by any other operator or tenant.

    Parameters
    ----------
    operator:
        The reading agent's identity. ``tenant_id`` + ``sub`` key the spill
        (and, on the read side, the fetch); the arguments carry no tenant.
    audit_id:
        The dispatch's ``audit_log.id`` -- the soft-FK the trace hangs off.
    store:
        Injectable read-back store (tests); defaults to the process singleton,
        the same store the JSONFlux reducer spills into.
    now:
        Mint time; defaults to ``datetime.now(UTC)``. Injectable for tests.
    """
    minted_at = now if now is not None else datetime.now(UTC)
    try:
        tenant_id = operator.tenant_id
        if tenant_id is None:
            return None

        # F5 per-tenant gate FIRST: a gated-off tenant's trace is never loaded.
        if not await should_expose_to_agent(tenant_id=tenant_id):
            return None

        rows = await _load_agent_trace_rows(tenant_id=tenant_id, audit_id=audit_id)
        if rows is None:
            return None
        total_rows = len(rows)

        handle_id = uuid.uuid4()
        resolved_store = store if store is not None else get_result_handle_store()
        settings = get_settings()
        max_rows = settings.result_handle_max_spill_rows
        stored = await resolved_store.spill(
            tenant_id=tenant_id,
            operator_sub=operator.sub,
            handle_id=handle_id,
            op_id=_TRACE_OP_ID,
            rows=rows,
            total_rows=total_rows,
            ttl_seconds=_TRACE_HANDLE_TTL_SECONDS,
            max_rows=max_rows,
        )
        if not stored:
            # Fail-open: empty trace or unreachable store -> no agent-readable
            # handle. The operator plane still reads the trace.
            return None

        return _build_handle(
            handle_id=handle_id,
            audit_id=audit_id,
            sample_rows=_bounded_sample(rows, byte_budget=settings.jsonflux_sample_byte_budget),
            total_rows=total_rows,
            stored_rows=min(total_rows, max_rows),
            minted_at=minted_at,
        )
    except Exception:
        # Best-effort, doubt-reduces-exposure: any unexpected error withholds
        # the trace from the agent (returns None) rather than surfacing it or
        # failing the read. Mirrors record_trace's F7 fail-open discipline.
        _log.warning(
            "flight_recorder_agent_trace_mint_failed",
            audit_id=str(audit_id),
            tenant_id=None if operator.tenant_id is None else str(operator.tenant_id),
            exc_info=True,
        )
        return None


async def attach_agent_trace_handle(
    result: OperationResult,
    *,
    operator: Operator,
    audit_id: uuid.UUID,
) -> OperationResult:
    """The recorder's **live trigger** (F5): surface a trace handle on the response.

    Called by the dispatcher for the **owning root dispatch** after the capture
    seam has persisted the trace via ``record_trace`` (best-effort). Mints the
    agent-readable handle (:func:`materialize_agent_trace_handle`, which applies
    the per-tenant gate and the redaction-uncertainty degrade) and, when one is
    minted, attaches its JSON form to :attr:`OperationResult.extras` under
    :data:`AGENT_TRACE_HANDLE_EXTRA_KEY`. The agent pages it via the unchanged
    ``result_query`` meta-tool -- no new tool, no raw payload.

    **Strictly inside the F7 swallow-everything discipline.** This runs after
    the dispatch result is computed and the audit row committed; it returns the
    **original, unmodified** ``result`` on every no-handle and every failure
    path, so a trigger failure can never fail, block, or alter a dispatch. When
    the tenant gate is off (or capture produced nothing), the mint returns
    ``None`` after a single cache-aware gate check and nothing is spilled --
    the disabled path stays cheap.
    """
    try:
        handle = await materialize_agent_trace_handle(operator=operator, audit_id=audit_id)
        if handle is None:
            return result
        extras = dict(result.extras)
        extras[AGENT_TRACE_HANDLE_EXTRA_KEY] = handle.model_dump(mode="json")
        return result.model_copy(update={"extras": extras})
    except Exception:
        # F7: a trigger failure never touches the dispatch result.
        _log.warning(
            "flight_recorder_agent_trace_attach_failed",
            audit_id=str(audit_id),
            exc_info=True,
        )
        return result
