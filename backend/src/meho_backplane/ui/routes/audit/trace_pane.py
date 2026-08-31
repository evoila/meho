# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Projection for the audit drawer's flight-recorder trace pane (#3215).

The console trace pane (a new section in ``audit/_drawer.html`` after Lineage)
renders the operator read surface of the dispatch flight recorder
(``docs/decisions/dispatch-flight-recorder.md``): the ordered, already
redacted+capped spans a governed dispatch produced. This module turns a
:class:`~meho_backplane.flight_recorder.TraceView` into a flat, render-ready
context dict.

Why a projection (not the raw view) is handed to the template: the drawer
templating runs under Jinja ``StrictUndefined``, so every key the template
reads must exist. :func:`project_trace` normalises each span into a fixed key
set (present, ``None`` when absent) drawn from the attribute contract the
capture seam writes (``vendor_call`` / ``composite_step`` / ``jsonflux_reduction``
/ ``typed`` / ``opaque``), plus an ``other_attributes`` catch-all so a span kind
whose exact shape is not yet frozen (typed spans, #3217) still renders every
stored, already-redacted datum rather than silently dropping it.

Read-only and non-mutating: it never re-processes or un-redacts a body. The
redacted body value (a structure, ``None``, or a fixed ``[MEHO-…]`` omission
marker) is passed through verbatim; the template escapes it at render time.
"""

from __future__ import annotations

from typing import Any, Final

from meho_backplane.flight_recorder import TraceSpanView, TraceView

__all__ = ["project_trace"]

#: DaisyUI badge variant per enumerated span kind. An unknown kind (a future
#: span kind landing before this map is updated) falls back to ``badge-ghost``
#: so it still renders, just uncoloured.
_SPAN_KIND_BADGES: Final[dict[str, str]] = {
    "vendor_call": "badge-primary",
    "composite_step": "badge-secondary",
    "jsonflux_reduction": "badge-accent",
    "typed": "badge-info",
    "opaque": "badge-ghost",
}

#: Attribute keys surfaced as first-class fields in the projected span. Anything
#: outside this set is collected into ``other_attributes`` so no stored datum is
#: hidden from an operator (e.g. typed-span metadata, #3217).
_SURFACED_ATTR_KEYS: Final[frozenset[str]] = frozenset(
    {
        "method",
        "url",
        "connector_id",
        "op_id",
        "source_kind",
        "request_id",
        "request_headers",
        "response_headers",
        "request_body",
        "response_body",
        "body_recorded",
        "truncated",
        "redaction_reasons",
        "input_rows",
        "total_rows",
        "kept_fields",
        "kept_field_count",
        "output_bytes",
        "handle",
        "collapsed",
        "collapsed_count",
    }
)


def _span_status_tone(status: str | None) -> str:
    """Map a span status to a DaisyUI badge tone.

    ``status`` is an HTTP code as text (``"200"`` / ``"503"``) or ``"ok"`` /
    ``"error"`` for non-HTTP spans. 2xx / ``ok`` are success; 4xx / 5xx /
    ``error`` are error; anything else (or absent) is neutral.
    """
    if not status:
        return "badge-ghost"
    if status == "ok" or status.startswith("2"):
        return "badge-success"
    if status == "error" or status.startswith(("4", "5")):
        return "badge-error"
    return "badge-ghost"


def _project_span(span: TraceSpanView) -> dict[str, Any]:
    """Normalise one span into the fixed key set the template reads."""
    attrs = span.attributes
    return {
        "seq": span.seq,
        "span_kind": span.span_kind,
        "kind_badge": _SPAN_KIND_BADGES.get(span.span_kind, "badge-ghost"),
        "name": span.name,
        "status": span.status,
        "status_tone": _span_status_tone(span.status),
        "duration_ms": span.duration_ms,
        "started_at": span.started_at.isoformat(),
        # vendor_call / composite_step identity
        "method": attrs.get("method"),
        "url": attrs.get("url"),
        "connector_id": attrs.get("connector_id"),
        "op_id": attrs.get("op_id"),
        "source_kind": attrs.get("source_kind"),
        "request_id": attrs.get("request_id"),
        # redacted headers/bodies (rendered as-stored; template escapes them)
        "request_headers": attrs.get("request_headers") or {},
        "response_headers": attrs.get("response_headers") or {},
        "request_body": attrs.get("request_body"),
        "response_body": attrs.get("response_body"),
        "request_body_present": attrs.get("request_body") is not None,
        "response_body_present": attrs.get("response_body") is not None,
        "body_recorded": bool(attrs.get("body_recorded", True)),
        "truncated": bool(attrs.get("truncated")),
        "redaction_reasons": attrs.get("redaction_reasons") or [],
        # jsonflux_reduction metadata
        "input_rows": attrs.get("input_rows"),
        "total_rows": attrs.get("total_rows"),
        "kept_fields": attrs.get("kept_fields") or [],
        "kept_field_count": attrs.get("kept_field_count"),
        "output_bytes": attrs.get("output_bytes"),
        "handle": attrs.get("handle"),
        # overflow-collapse groups (F3)
        "collapsed": bool(attrs.get("collapsed")),
        "collapsed_count": attrs.get("collapsed_count"),
        # everything else stored on the span, so nothing is hidden
        "other_attributes": {k: v for k, v in attrs.items() if k not in _SURFACED_ATTR_KEYS},
    }


def project_trace(trace: TraceView | None) -> dict[str, Any]:
    """Turn a :class:`TraceView` (or its absence) into the drawer pane context.

    Always returns a dict with a ``present`` discriminator so the template can
    branch without hitting ``StrictUndefined``. When ``trace`` is ``None`` (the
    audit row exists but no trace was captured — a normal best-effort/opt-in
    state, not an error) the pane renders its empty state.

    ``redaction_uncertain`` is surfaced verbatim (F5): the operator plane sees
    every trace, and sees which ones were withheld from the agent handle.
    """
    if trace is None:
        return {"present": False, "spans": []}
    return {
        "present": True,
        "redaction_uncertain": trace.redaction_uncertain,
        "trace_id": str(trace.trace_id),
        "created_at": trace.created_at.isoformat(),
        "expires_at": trace.expires_at.isoformat(),
        "span_count": len(trace.spans),
        "spans": [_project_span(span) for span in trace.spans],
    }
