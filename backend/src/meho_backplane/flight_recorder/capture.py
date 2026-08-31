# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Flight-recorder capture seam (#3214, capture seam + F3 caps + F7 invariant).

The span-production layer of the dispatch flight recorder, per the decision
of record ``docs/decisions/dispatch-flight-recorder.md`` (capture seam / F3 /
F7). It rides the **existing** single dispatch path -- no parallel execution
path -- and produces the ordered spans the store task (#3212) persists and the
redaction engine (#3213) scrubs.

What lives here
---------------
* A per-dispatch **capture scope** bound on a contextvar
  (:data:`_active_capture_var`). Opened **once** at the root dispatch (when
  :func:`meho_backplane.flight_recorder.should_capture` says so, F1) and
  shared by every nested composite child dispatch, so a composite's sub-step
  spans land under the **one** parent trace with no new machinery -- the child
  re-enters :func:`dispatch` in the same task and sees the same scope.
* A per-dispatch **op context** bound on a second contextvar
  (:data:`_op_context_var`), rebound for every dispatch (root **and** each
  child). A vendor-call span is redacted against the op context of the dispatch
  that *made* the call -- so a composite child that mints a session has its
  body hard-excluded by the child's op family, not the parent's.
* The three capture entry points the seams call:
  :func:`span_start` + :func:`record_vendor_call` (the shared httpx seam),
  :func:`record_jsonflux_reduction` (the JSONFlux reducer), and the
  ``composite_step`` marker emitted by :func:`begin_dispatch_capture` when a
  nested child dispatch is detected.
* The **F3 caps** enforced at capture time
  (:data:`~meho_backplane.flight_recorder._capture_util.MAX_SPAN_BODY_BYTES`,
  :data:`_MAX_TRACE_BYTES`, :data:`_MAX_SPANS`): oversize truncates, span
  overflow collapses into counted per-kind groups, and nothing ever errors a
  dispatch.

The F7 invariant
----------------
Capture is strictly best-effort and can **never fail, block, or materially
slow a dispatch**. Every public function swallows every exception (logging a
warning), the span-assembly work is cheap, the disabled path is a single
contextvar read, and persistence rides the store's own best-effort
:func:`meho_backplane.flight_recorder.record_trace` (which opens its own
session and never raises). This is the same fail-open discipline the
result-handle spill proves
(:mod:`meho_backplane.connectors.result_handle_store`).

Capture-before-redaction is absolute: no raw vendor header or body byte is
ever placed on a :class:`~meho_backplane.flight_recorder.store.SpanInput`.
Every artefact passes through
:func:`meho_backplane.redaction.flight_recorder.redact_span` first, and the
trace inherits the OR of every span's redaction-uncertainty verdict (F5
operator-only degrade).
"""

from __future__ import annotations

import time
import uuid
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Final

import structlog

from meho_backplane.flight_recorder._capture_util import (
    approx_bytes,
    approx_span_bytes,
    as_mapping,
    cap_body,
    coerce_uuid,
    elapsed_ms,
    header_value,
    now_utc,
    request_id,
    response_content,
    safe_url,
)
from meho_backplane.flight_recorder.config import should_capture
from meho_backplane.flight_recorder.store import SpanInput, record_trace
from meho_backplane.redaction.flight_recorder import redact_span

__all__ = [
    "CaptureHandle",
    "SpanStart",
    "begin_dispatch_capture",
    "end_dispatch_capture",
    "record_jsonflux_reduction",
    "record_vendor_call",
    "span_start",
]

_log = structlog.get_logger(__name__)

# --- F3 caps (fixed invariants, not tunable knobs) --------------------------
#: Per-trace cap. Once a trace's accumulated span bytes would exceed this,
#: further spans collapse into counted per-kind groups instead of persisting.
_MAX_TRACE_BYTES: Final[int] = 1024 * 1024
#: Soft per-dispatch span ceiling. Beyond it, spans collapse into counted
#: per-kind groups -- the long-poll idiom the decision names (F3).
_MAX_SPANS: Final[int] = 50
#: Upper bound on the JSONFlux ``kept_fields`` list recorded on a reduction
#: span, so a pathologically wide schema cannot blow the span byte budget.
_MAX_JSONFLUX_KEPT_FIELDS: Final[int] = 200


@dataclass(frozen=True, slots=True)
class SpanStart:
    """A cheap start marker for one vendor-call span.

    Returned by :func:`span_start` **only** when capture is active for the
    current dispatch, so the httpx seam pays for nothing (one contextvar read)
    when the recorder is off. Carries the wall-clock start (for
    :attr:`SpanInput.started_at`) and a monotonic reference (for the duration).
    """

    started_at: datetime
    monotonic: float


@dataclass(frozen=True, slots=True)
class _OpContext:
    """The redaction-relevant context of one dispatch.

    Rebound per dispatch so a vendor call is redacted against the op that made
    it (a composite child's session mint is excluded by the child's family,
    not the parent's).
    """

    op_id: str | None
    connector_id: str | None
    tags: tuple[str, ...]
    body_paths: tuple[str, ...]
    delete_shaped_patterns: tuple[str, ...] | None


@dataclass(slots=True)
class _CaptureScope:
    """The mutable per-trace accumulator, bound once at the root dispatch."""

    audit_id: uuid.UUID
    tenant_id: uuid.UUID
    target_id: uuid.UUID | None
    spans: list[SpanInput] = field(default_factory=list)
    total_bytes: int = 0
    redaction_uncertain: bool = False
    #: Per-kind count of spans dropped by the span-count / byte caps (F3).
    overflow: dict[str, int] = field(default_factory=dict)

    def add(self, span: SpanInput) -> None:
        """Append *span* under the F3 caps; overflow collapses, never errors."""
        if len(self.spans) >= _MAX_SPANS:
            self._collapse(span.span_kind)
            return
        approx = approx_span_bytes(span)
        # Always admit the first span (a single body is already <=64 KB, well
        # under the 1 MB trace cap); collapse only once the trace is full.
        if self.spans and self.total_bytes + approx > _MAX_TRACE_BYTES:
            self._collapse(span.span_kind)
            return
        self.spans.append(span)
        self.total_bytes += approx

    def _collapse(self, kind: str) -> None:
        self.overflow[kind] = self.overflow.get(kind, 0) + 1

    def finalize(self, now: datetime) -> None:
        """Append one counted collapse span per overflowed kind (F3)."""
        for kind, count in self.overflow.items():
            self.spans.append(
                SpanInput(
                    span_kind=kind,
                    name=f"{kind} (collapsed x{count})",
                    started_at=now,
                    attributes={"collapsed": True, "collapsed_count": count},
                )
            )
        self.overflow.clear()


@dataclass(frozen=True, slots=True)
class CaptureHandle:
    """Opaque token returned by :func:`begin_dispatch_capture`.

    Carries the contextvar reset tokens and whether *this* dispatch owns the
    scope (the root that must persist the trace on exit). Consumed exactly once
    by :func:`end_dispatch_capture`.
    """

    owns: bool
    scope: _CaptureScope | None
    op_token: Any
    cap_token: Any


#: The active per-trace accumulator for the current dispatch (and its nested
#: composite children, via contextvar propagation within the same task).
#: ``None`` = capture is off for this dispatch, so every seam is a no-op.
_active_capture_var: ContextVar[_CaptureScope | None] = ContextVar(
    "flight_recorder_active_capture", default=None
)
#: The redaction-relevant op context of the *current* dispatch. Rebound per
#: dispatch (root and each composite child) so vendor-call redaction uses the
#: op that actually made the call.
_op_context_var: ContextVar[_OpContext | None] = ContextVar(
    "flight_recorder_op_context", default=None
)

_INACTIVE_HANDLE: Final[CaptureHandle] = CaptureHandle(
    owns=False, scope=None, op_token=None, cap_token=None
)


# ---------------------------------------------------------------------------
# Dispatch-boundary lifecycle (called by the dispatcher)
# ---------------------------------------------------------------------------


async def begin_dispatch_capture(
    *,
    audit_id: uuid.UUID,
    operator: Any,
    target: Any,
    descriptor: Any,
    connector_id: str,
) -> CaptureHandle:
    """Open (or join) the capture scope for one dispatch. Never raises (F7).

    * **Root dispatch** (no active scope): consults :func:`should_capture`
      (F1 precedence + kill switch, fail-open) and, when enabled, binds a fresh
      :class:`_CaptureScope` + this op's context. The returned handle ``owns``
      the scope and persists it on exit.
    * **Nested composite child** (a scope is already active): reuses the parent
      scope, binds *this* child's op context for correct per-op redaction, and
      records a ``composite_step`` marker. It does **not** open a new trace.

    Any failure degrades to an inactive handle -- capture simply does not
    happen for this dispatch, and the dispatch is untouched.
    """
    try:
        existing = _active_capture_var.get()
        if existing is not None:
            # Nested composite child: join the parent trace, no new machinery.
            op_token = _op_context_var.set(_op_context_from(descriptor, connector_id))
            _record_composite_step(existing, descriptor, connector_id)
            return CaptureHandle(owns=False, scope=existing, op_token=op_token, cap_token=None)

        tenant_id = getattr(operator, "tenant_id", None)
        if tenant_id is None:
            return _INACTIVE_HANDLE
        target_id = coerce_uuid(getattr(target, "id", None))
        if not await should_capture(tenant_id=tenant_id, target_id=target_id):
            return _INACTIVE_HANDLE

        scope = _CaptureScope(audit_id=audit_id, tenant_id=tenant_id, target_id=target_id)
        cap_token = _active_capture_var.set(scope)
        op_token = _op_context_var.set(_op_context_from(descriptor, connector_id))
        return CaptureHandle(owns=True, scope=scope, op_token=op_token, cap_token=cap_token)
    except Exception:
        _log.warning("flight_recorder_begin_capture_failed", exc_info=True)
        return _INACTIVE_HANDLE


async def end_dispatch_capture(handle: CaptureHandle) -> None:
    """Close the capture scope opened by :func:`begin_dispatch_capture` (F7).

    Resets the per-dispatch op context always; for the owning root dispatch,
    also unbinds the scope and persists the trace on the store's best-effort
    path. Runs in the dispatcher's ``finally``, so it fires after the audit row
    has committed -- the trace attaches to a durable ``audit_log.id``. Never
    raises.
    """
    try:
        if handle.op_token is not None:
            _op_context_var.reset(handle.op_token)
        if handle.owns and handle.scope is not None:
            if handle.cap_token is not None:
                _active_capture_var.reset(handle.cap_token)
            await _persist(handle.scope)
    except Exception:
        _log.warning("flight_recorder_end_capture_failed", exc_info=True)


async def _persist(scope: _CaptureScope) -> None:
    """Finalize + write the trace, best-effort. Wrapped for F7 defense."""
    try:
        scope.finalize(now_utc())
        await record_trace(
            audit_id=scope.audit_id,
            tenant_id=scope.tenant_id,
            spans=scope.spans,
            redaction_uncertain=scope.redaction_uncertain,
        )
    except Exception:
        # ``record_trace`` already swallows its own errors; this guard covers
        # a monkeypatched/raising recorder so a forced failure still cannot
        # touch the (already-committed) dispatch result (F7).
        _log.warning("flight_recorder_persist_failed", exc_info=True)


# ---------------------------------------------------------------------------
# Vendor-call span (the shared httpx seam)
# ---------------------------------------------------------------------------


def span_start() -> SpanStart | None:
    """Return a start marker iff capture is active; else ``None`` (cheap)."""
    if _active_capture_var.get() is None:
        return None
    return SpanStart(started_at=now_utc(), monotonic=time.monotonic())


def record_vendor_call(
    start: SpanStart | None,
    *,
    method: str,
    request_headers: Any,
    response: Any,
    request_body: Any = None,
    request_content_type: str | None = None,
) -> None:
    """Capture one generic-connector vendor HTTP call as a span (F7).

    *start* is the marker from :func:`span_start` (``None`` -> capture is off,
    no-op). *response* is the completed :class:`httpx.Response`; the span is
    recorded regardless of status so an operator sees vendor 4xx/5xx bodies.
    The request URL is derived from ``response.request`` **inside** this
    guarded function so the call site never touches an attribute that could
    raise into the dispatch. Headers and bodies are **redacted at capture** --
    no raw byte reaches the :class:`SpanInput`. Any error is logged and
    swallowed.
    """
    scope = _active_capture_var.get()
    if start is None or scope is None:
        return
    try:
        redaction, url, truncated = _redact_vendor_call(
            method=method,
            request_headers=request_headers,
            response=response,
            request_body=request_body,
            request_content_type=request_content_type,
        )
        scope.add(
            SpanInput(
                span_kind="vendor_call",
                name=f"{method} {url}",
                started_at=start.started_at,
                duration_ms=elapsed_ms(start.monotonic),
                status=str(getattr(response, "status_code", "")) or None,
                attributes=_vendor_attributes(redaction, method, url, truncated),
            )
        )
        if redaction.uncertain:
            scope.redaction_uncertain = True
    except Exception:
        _log.warning("flight_recorder_vendor_span_failed", exc_info=True)


def _redact_vendor_call(
    *,
    method: str,
    request_headers: Any,
    response: Any,
    request_body: Any,
    request_content_type: str | None,
) -> tuple[Any, str, bool]:
    """Cap + redact one vendor call. Returns ``(redaction, safe_url, truncated)``."""
    op = _op_context_var.get()
    request_url = getattr(getattr(response, "request", None), "url", None)
    response_content_type = header_value(response, "content-type")
    req_body, req_truncated = cap_body(request_body)
    resp_body, resp_truncated = cap_body(response_content(response))
    redaction = redact_span(
        op_id=op.op_id if op else None,
        connector_id=op.connector_id if op else None,
        method=method,
        tags=op.tags if op else (),
        request_headers=as_mapping(request_headers),
        response_headers=as_mapping(getattr(response, "headers", None)),
        request_body=req_body,
        response_body=resp_body,
        request_content_type=request_content_type,
        response_content_type=response_content_type,
        request_truncated=req_truncated,
        response_truncated=resp_truncated,
        body_paths=op.body_paths if op else (),
        delete_shaped_patterns=op.delete_shaped_patterns if op else None,
    )
    return redaction, safe_url(request_url), req_truncated or resp_truncated


def _vendor_attributes(redaction: Any, method: str, url: str, truncated: bool) -> dict[str, Any]:
    op = _op_context_var.get()
    attributes: dict[str, Any] = {
        "connector_id": op.connector_id if op else None,
        "op_id": op.op_id if op else None,
        "method": method,
        "url": url,
        "request_headers": redaction.request_headers,
        "response_headers": redaction.response_headers,
        "request_body": redaction.request_body,
        "response_body": redaction.response_body,
        "body_recorded": redaction.body_recorded,
    }
    if truncated:
        attributes["truncated"] = True
    if redaction.reasons:
        attributes["redaction_reasons"] = list(redaction.reasons)
    rid = request_id()
    if rid is not None:
        attributes["request_id"] = rid
    return attributes


# ---------------------------------------------------------------------------
# JSONFlux reduction span (the reducer seam)
# ---------------------------------------------------------------------------


def record_jsonflux_reduction(
    *,
    op_id: str | None,
    input_rows: int,
    total_rows: int,
    kept_fields: list[str],
    summary: Any,
    handle_id: uuid.UUID,
) -> None:
    """Capture the JSONFlux reduction as a span (F7).

    Metadata only -- input row count -> kept fields -> output size -> handle id
    -- so it carries no vendor bytes and never sets redaction-uncertainty.
    No-op when capture is off. Any error is logged and swallowed.
    """
    scope = _active_capture_var.get()
    if scope is None:
        return
    try:
        attributes: dict[str, Any] = {
            "op_id": op_id,
            "input_rows": input_rows,
            "total_rows": total_rows,
            "kept_fields": list(kept_fields[:_MAX_JSONFLUX_KEPT_FIELDS]),
            "kept_field_count": len(kept_fields),
            "output_bytes": approx_bytes(summary),
            "handle": str(handle_id),
        }
        rid = request_id()
        if rid is not None:
            attributes["request_id"] = rid
        scope.add(
            SpanInput(
                span_kind="jsonflux_reduction",
                name="jsonflux.reduce",
                started_at=now_utc(),
                status="ok",
                attributes=attributes,
            )
        )
    except Exception:
        _log.warning("flight_recorder_jsonflux_span_failed", exc_info=True)


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _record_composite_step(scope: _CaptureScope, descriptor: Any, connector_id: str) -> None:
    """Mark a composite sub-step boundary under the parent trace."""
    try:
        attributes: dict[str, Any] = {
            "op_id": getattr(descriptor, "op_id", None),
            "connector_id": connector_id,
            "source_kind": getattr(descriptor, "source_kind", None),
        }
        rid = request_id()
        if rid is not None:
            attributes["request_id"] = rid
        scope.add(
            SpanInput(
                span_kind="composite_step",
                name=f"sub-step: {getattr(descriptor, 'op_id', '?')}",
                started_at=now_utc(),
                attributes=attributes,
            )
        )
    except Exception:
        _log.warning("flight_recorder_composite_span_failed", exc_info=True)


def _op_context_from(descriptor: Any, connector_id: str) -> _OpContext:
    """Build the per-dispatch op context for redaction."""
    tags_raw = getattr(descriptor, "tags", None) or ()
    tags = tuple(str(tag) for tag in tags_raw)
    try:
        from meho_backplane.settings import get_settings

        delete_shaped: tuple[str, ...] | None = tuple(
            get_settings().service_grant_delete_shaped_patterns
        )
    except Exception:
        delete_shaped = None
    return _OpContext(
        op_id=getattr(descriptor, "op_id", None),
        connector_id=connector_id,
        tags=tags,
        # Per-connector body-path config is a connector-authoring follow-up;
        # the credential-shape scrub + hard-excluded families still protect
        # every body until a connector declares its paths.
        body_paths=(),
        delete_shaped_patterns=delete_shaped,
    )
