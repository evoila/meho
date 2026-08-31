# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Typed-connector spans -- the typed handler seam + transport enrichers (#3217, F8).

The span-production layer for **typed** connectors, the F8 analog of the
generic-connector ``vendor_call`` seam in
:mod:`meho_backplane.flight_recorder.capture`. Per the decision of record
``docs/decisions/dispatch-flight-recorder.md`` (F8, the operator override
"all of them in v1"), every typed connector family emits real spans, not the
transitional ``opaque`` fallback.

Two entry points, one scope
---------------------------
Both ride the **same** per-dispatch capture scope + op context that
:mod:`.capture` binds on the dispatcher's contextvars -- no parallel state:

* :func:`typed_dispatch_span` -- the **shared seam**. The typed-dispatch
  branch (:func:`meho_backplane.operations._branches.dispatch_typed`) opens
  it around **every** typed handler invocation, so every typed family --
  class-based and the builtin ``product.*`` families alike -- emits a real
  (non-``opaque``) ``typed`` span through the one seam with no per-connector
  edit: operation id, target, duration, and outcome. It is metadata-only (no
  ``params``, no vendor payload), so it carries no secret and never sets
  redaction-uncertainty. Keyed on ``impl_id`` (F8), so both implementations
  of a dual-impl product are distinguished.
* :func:`typed_span_start` + :func:`record_typed_call` -- the **transport
  enricher** seam. A transport with request/response-shaped detail records
  its own richer span under the same trace. REST-backed typed connectors are
  already covered by the shared httpx ``vendor_call`` seam; the SSH families
  (bind9 / holodeck / pfsense / rke2 / windows_dns) record per-command spans
  from the shared :meth:`SshConnector._run_command` seam. The direct-SDK
  families (hvac / kubernetes / pymongo / postgres and the googleapiclient-
  and REST-backed gcloud) are covered by the shared ``typed`` span at the
  op granularity, the natural unit for a one-op-one-SDK-interaction handler.

Redaction + F7
--------------
Any body handed to :func:`record_typed_call` is capped (F3) and passed
through the **same** fail-closed engine the vendor-call seam uses
(:func:`meho_backplane.redaction.flight_recorder.redact_span`): a body it
cannot prove secret-free -- a plain-text SSH command or its output, a
secret-family op's body -- is omitted and the trace degrades to operator-only
(F5). Every function is best-effort (F7): the disabled path is one contextvar
read, every exception is logged and swallowed, and a handler's own exception
always propagates unchanged (the shared seam records the failure outcome,
then re-raises).
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import structlog

from meho_backplane.flight_recorder._capture_util import (
    cap_body,
    elapsed_ms,
    now_utc,
    request_id,
)
from meho_backplane.flight_recorder.capture import (
    _active_capture_var,
    _op_context_var,
    _OpContext,
)
from meho_backplane.flight_recorder.store import SpanInput
from meho_backplane.redaction.flight_recorder import redact_span

__all__ = [
    "TypedSpanStart",
    "record_typed_call",
    "typed_dispatch_span",
    "typed_span_start",
]

_log = structlog.get_logger(__name__)


@dataclass(frozen=True, slots=True)
class TypedSpanStart:
    """A cheap start marker for one typed span.

    Returned by :func:`typed_span_start` **only** when capture is active for
    the current dispatch, so a typed handler / SSH-transport seam pays for
    nothing (one contextvar read) when the recorder is off. Carries the span
    label plus the wall-clock + monotonic references for the duration.
    """

    started_at: datetime
    monotonic: float
    name: str


def typed_span_start(name: str) -> TypedSpanStart | None:
    """Return a typed-span start marker iff capture is active; else ``None``.

    The one-contextvar-read gate every typed seam opens *before* the SDK /
    vendor interaction, so the disabled path stays free (F7).
    """
    if _active_capture_var.get() is None:
        return None
    return TypedSpanStart(started_at=now_utc(), monotonic=time.monotonic(), name=name)


def record_typed_call(
    start: TypedSpanStart | None,
    *,
    kind: str = "typed",
    status: str | None = "ok",
    request_body: Any = None,
    response_body: Any = None,
    request_content_type: str | None = None,
    response_content_type: str | None = None,
    extra: dict[str, Any] | None = None,
) -> None:
    """Capture one typed-handler / typed-transport interaction as a span (F8, F7).

    *start* is the marker from :func:`typed_span_start` (``None`` -> no-op).
    The span records the op context (``op_id`` / ``connector_id`` /
    ``impl_id`` / ``product``), keyed on the implementation not the product.
    Any body is capped + redacted (see module docstring); a bodiless call is
    metadata-only and never sets redaction-uncertainty. ``kind`` is
    ``"typed"`` (instrumented) or ``"opaque"`` (transitional fallback). Any
    error is logged and swallowed (F7).
    """
    scope = _active_capture_var.get()
    if start is None or scope is None:
        return
    try:
        op = _op_context_var.get()
        attributes = _typed_base_attributes(op, extra)
        uncertain = False
        if request_body is not None or response_body is not None:
            uncertain = _record_typed_bodies(
                attributes,
                op,
                request_body=request_body,
                response_body=response_body,
                request_content_type=request_content_type,
                response_content_type=response_content_type,
            )
        rid = request_id()
        if rid is not None:
            attributes["request_id"] = rid
        scope.add(
            SpanInput(
                span_kind=kind,
                name=start.name,
                started_at=start.started_at,
                duration_ms=elapsed_ms(start.monotonic),
                status=status,
                attributes=attributes,
            )
        )
        if uncertain:
            scope.redaction_uncertain = True
    except Exception:
        _log.warning("flight_recorder_typed_span_failed", exc_info=True)


def _typed_base_attributes(op: _OpContext | None, extra: dict[str, Any] | None) -> dict[str, Any]:
    """The op-context axes every typed span carries, plus any caller *extra*."""
    attributes: dict[str, Any] = {
        "op_id": op.op_id if op else None,
        "connector_id": op.connector_id if op else None,
        "impl_id": op.impl_id if op else None,
        "product": op.product if op else None,
    }
    if extra:
        attributes.update(extra)
    return attributes


def _record_typed_bodies(
    attributes: dict[str, Any],
    op: _OpContext | None,
    *,
    request_body: Any,
    response_body: Any,
    request_content_type: str | None,
    response_content_type: str | None,
) -> bool:
    """Cap + redact a typed span's bodies onto *attributes*; return uncertainty.

    Reuses the fail-closed engine (:func:`redact_span`) exactly as the
    vendor-call seam does, so a typed body is held to the identical redaction
    contract. Returns the OR of the two bodies' redaction-uncertainty (the F5
    operator-only degrade signal).
    """
    req_body, req_truncated = cap_body(request_body)
    resp_body, resp_truncated = cap_body(response_body)
    redaction = redact_span(
        op_id=op.op_id if op else None,
        connector_id=op.connector_id if op else None,
        tags=op.tags if op else (),
        request_headers=None,
        response_headers=None,
        request_body=req_body,
        response_body=resp_body,
        request_content_type=request_content_type,
        response_content_type=response_content_type,
        request_truncated=req_truncated,
        response_truncated=resp_truncated,
        body_paths=op.body_paths if op else (),
        delete_shaped_patterns=op.delete_shaped_patterns if op else None,
    )
    attributes["request_body"] = redaction.request_body
    attributes["response_body"] = redaction.response_body
    attributes["body_recorded"] = redaction.body_recorded
    if req_truncated or resp_truncated:
        attributes["truncated"] = True
    if redaction.reasons:
        attributes["redaction_reasons"] = list(redaction.reasons)
    return redaction.uncertain


@contextmanager
def typed_dispatch_span(*, target: Any = None) -> Iterator[None]:
    """Bracket one typed-handler dispatch as a ``typed`` span (F8, F7).

    The shared typed-dispatch seam. Opened by
    :func:`meho_backplane.operations._branches.dispatch_typed` around every
    typed handler invocation, so every typed family emits a real
    (non-``opaque``) span -- op id, target, duration, outcome -- with no
    per-connector edit. Metadata-only, so it never records a secret and never
    sets redaction-uncertainty; transports add their own richer spans.

    Cheap no-op when capture is off (**one** contextvar read -- the disabled
    path never reads the op context or builds the span name). Best-effort (F7):
    a recorder failure is swallowed, and the handler's own exception always
    propagates unchanged -- the span records the outcome
    (``error:<ExceptionClass>``), then re-raises.
    """
    # Gate on the single active-capture read first, so the disabled path costs
    # exactly one contextvar read -- the op-context read + name build happen
    # only when a trace is actually being recorded.
    if _active_capture_var.get() is None:
        yield
        return
    start = TypedSpanStart(
        started_at=now_utc(), monotonic=time.monotonic(), name=_typed_dispatch_span_name()
    )
    status = "ok"
    try:
        yield
    except BaseException as exc:
        status = f"error:{type(exc).__name__}"
        raise
    finally:
        # ``record_typed_call`` already swallows its own errors; this guard
        # covers a monkeypatched/raising recorder so a forced failure in the
        # ``finally`` can never propagate into (and fail) the dispatch (F7).
        try:
            record_typed_call(
                start, kind="typed", status=status, extra=_typed_dispatch_extra(target)
            )
        except Exception:
            _log.warning("flight_recorder_typed_dispatch_span_failed", exc_info=True)


def _typed_dispatch_span_name() -> str:
    """The shared typed-dispatch span label: the op id (or ``"typed"``)."""
    op = _op_context_var.get()
    if op is not None and op.op_id:
        return op.op_id
    return "typed"


def _typed_dispatch_extra(target: Any) -> dict[str, Any] | None:
    """The non-secret ``target`` label for the shared typed-dispatch span."""
    name = _safe_target_name(target)
    return {"target": name} if name else None


def _safe_target_name(target: Any) -> str | None:
    """Read ``target.name`` defensively -- a name access must never raise (F7)."""
    try:
        name = getattr(target, "name", None)
    except Exception:
        return None
    return name if isinstance(name, str) else None
