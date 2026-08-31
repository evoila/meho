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
  touching the ``audit_log`` row;
* the **agent** read surface (:mod:`meho_backplane.flight_recorder.agent_read`,
  #3216, F5) -- the per-tenant-gated, redaction-uncertainty-degrading mint that
  exposes a captured trace to an agent as a reduced ``ResultHandle`` read back
  through the existing ``result_query`` core (no new tool). Its per-tenant gate
  lives in :mod:`meho_backplane.flight_recorder.config`
  (:func:`should_expose_to_agent`).

Explicitly **not** in this package (sibling Task under #3207): the
fail-closed redaction engine (#3213), which lives in
:mod:`meho_backplane.redaction.flight_recorder`. Both trace read surfaces do
live here -- the **operator** read (:mod:`meho_backplane.flight_recorder.read`,
#3215; the tenant-scoped read the REST route and the console pane both share)
and the **agent** read (:mod:`meho_backplane.flight_recorder.agent_read`,
#3216; the gated, degrading mint in the bullet above) -- alongside the capture
seam (:mod:`meho_backplane.flight_recorder.capture`, #3214).
"""

from meho_backplane.flight_recorder.agent_read import (
    AGENT_TRACE_HANDLE_EXTRA_KEY,
    attach_agent_trace_handle,
    materialize_agent_trace_handle,
)
from meho_backplane.flight_recorder.config import (
    compute_expires_at,
    reset_flight_recorder_config_cache_for_testing,
    resolve_retention_days,
    should_capture,
    should_expose_to_agent,
)
from meho_backplane.flight_recorder.read import TraceSpanView, TraceView, load_trace
from meho_backplane.flight_recorder.store import SpanInput, record_trace

__all__ = [
    "AGENT_TRACE_HANDLE_EXTRA_KEY",
    "SpanInput",
    "TraceSpanView",
    "TraceView",
    "attach_agent_trace_handle",
    "compute_expires_at",
    "load_trace",
    "materialize_agent_trace_handle",
    "record_trace",
    "reset_flight_recorder_config_cache_for_testing",
    "resolve_retention_days",
    "should_capture",
    "should_expose_to_agent",
]
