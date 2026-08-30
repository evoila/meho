# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Dispatch flight recorder — storage, capture config, and retention (#3212).

The storage + config substrate of the dispatch flight recorder, per the
decision of record ``docs/decisions/dispatch-flight-recorder.md`` (F1/F4/F6).
This package owns:

* the capture-enablement + retention resolver
  (:mod:`meho_backplane.flight_recorder.config`) -- per-target > per-tenant >
  global default precedence, the global kill switch, and the retention-window
  math, all fail-open;
* the internal, minimal persistence API
  (:mod:`meho_backplane.flight_recorder.store`) -- ``record_trace`` writes a
  trace header + ordered spans, stamping the retention deadline; best-effort
  (F7), never raising into a dispatch;
* the retention reaper (:mod:`meho_backplane.flight_recorder.reaper`) -- the
  bounded ``expires_at < now()`` sweep that deletes expired traces, never
  touching the ``audit_log`` row.

Explicitly **not** in this package (sibling Tasks under #3207): span
production / the capture seam (#3214), the fail-closed redaction engine
(#3213), and the operator + agent read surfaces.
"""

from meho_backplane.flight_recorder.config import (
    compute_expires_at,
    reset_flight_recorder_config_cache_for_testing,
    resolve_retention_days,
    should_capture,
)
from meho_backplane.flight_recorder.store import SpanInput, record_trace

__all__ = [
    "SpanInput",
    "compute_expires_at",
    "record_trace",
    "reset_flight_recorder_config_cache_for_testing",
    "resolve_retention_days",
    "should_capture",
]
