# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Fail-open publish-on-write to the per-tenant Valkey stream (G6.1-T3).

Constructs one ``XADD meho:feed:{tenant_id}`` per audited operation,
trimmed to :data:`BROADCAST_MAXLEN` via ``MAXLEN ~`` (approximate).
Two Prometheus counters surface the publish path on ``/metrics``:

* :data:`BROADCAST_EVENTS_PUBLISHED_TOTAL` — labelled by ``op_class``
  + ``result_status`` so operators can see the broadcast surface
  partitioned by sensitivity class and outcome.
* :data:`BROADCAST_PUBLISH_ERRORS_TOTAL` — unlabelled, counts every
  fail-open swallow (Valkey unreachable, ``XADD`` rejected, redis-py
  client teardown race). Operators alert off this counter; the
  cardinality is intentionally coarse.

Fail-open semantics
===================

A publish failure NEVER propagates back to the audit-middleware or
MCP handler call site. Reasons:

* The audit row is the canonical record; the broadcast feed is the
  real-time view. Returning a 500 to the operator because the broadcast
  side-channel didn't reach Slack would be worse than the operator just
  missing one event in the feed.
* Valkey unreachability is operationally common (rolling restart of
  the broadcast subchart, brief network blips); making it a request-path
  failure would multiply the operational cost of every Valkey wobble
  by the chassis's QPS.

The trade-off is **at-most-once** publish semantics — events can be
dropped, never duplicated. Subscribers that need at-least-once
ordering query :class:`~meho_backplane.db.models.AuditLog` by
``audit_id`` (the field on every :class:`BroadcastEvent`) to reconcile
gaps. G8's audit-query API (#334) is the upstream path for this.

References
----------

* Valkey ``XADD``: https://valkey.io/commands/xadd/
* Decision #3 (PII defaults the publisher relies on the upstream
  :func:`~meho_backplane.broadcast.events.redact_payload` to enforce):
  ``docs/planning/v0.2-decisions.md``.
"""

from __future__ import annotations

from typing import Final
from uuid import UUID

import structlog
from prometheus_client import Counter

from meho_backplane.broadcast.client import get_broadcast_client
from meho_backplane.broadcast.events import BroadcastEvent

__all__ = [
    "BROADCAST_EVENTS_PUBLISHED_TOTAL",
    "BROADCAST_MAXLEN",
    "BROADCAST_PUBLISH_ERRORS_TOTAL",
    "publish_event",
]


#: Approximate ceiling on entries kept per ``meho:feed:{tenant_id}``
#: stream. Heuristic per the G6.1-T3 task body: 10k entries ≈ 24h at
#: moderate load. The actual retention window is operator-tunable via
#: :attr:`~meho_backplane.settings.Settings.broadcast_retention_hours`;
#: a future refactor (post-T6) may derive this constant from that
#: setting and an events-per-hour estimate. v0.2 ships the static
#: ceiling — keeps the publish hot path one ``XADD`` with constant
#: kwargs.
BROADCAST_MAXLEN: Final[int] = 10000


#: Per-event publish success counter. The two labels partition the
#: feed so operators can graph "broadcast volume by op class" without
#: fanning into per-tenant cardinality. ``op_class`` matches the
#: ``BroadcastEvent.op_class`` taxonomy from
#: :func:`~meho_backplane.broadcast.events.classify_op`;
#: ``result_status`` matches the ``"ok"`` / ``"error"`` / ``"denied"``
#: trichotomy the handlers produce.
BROADCAST_EVENTS_PUBLISHED_TOTAL: Counter = Counter(
    "broadcast_events_published_total",
    "BroadcastEvents successfully XADD'd to the per-tenant Valkey stream.",
    labelnames=("op_class", "result_status"),
)

#: Per-failure counter for the fail-open swallow path. Unlabelled by
#: design: the failure modes (Valkey unreachable, XADD-rejected, redis-py
#: teardown race) all reduce to "the broadcast feed missed one event",
#: and an alert on a sustained nonzero rate is the only operational
#: signal that matters. Cardinality is intentionally coarse — adding
#: ``error_class`` as a label would multiply series by every redis-py
#: exception subclass the worker happens to see.
BROADCAST_PUBLISH_ERRORS_TOTAL: Counter = Counter(
    "broadcast_publish_errors_total",
    "BroadcastEvent publishes that failed (Valkey unreachable / XADD error / teardown race).",
)


_log = structlog.get_logger(__name__)


def _stream_key(tenant_id: UUID) -> str:
    """Build the per-tenant Valkey Streams key.

    Centralised here so a future tenancy-isolation tightening (e.g.
    a per-environment prefix) lands in one place rather than at every
    publisher call site.
    """
    return f"meho:feed:{tenant_id}"


async def publish_event(event: BroadcastEvent) -> None:
    """Fail-open publish to the per-tenant Valkey stream.

    Exactly one ``XADD`` per call:

    * ``key`` — ``meho:feed:{event.tenant_id}`` (per-tenant isolation).
    * ``fields`` — single ``event`` field carrying the JSON-serialised
      :class:`BroadcastEvent`. Single field rather than splitting the
      event into one Streams field per :class:`BroadcastEvent` attribute
      because the wire shape (one JSON blob) round-trips identically
      through SSE (T4), the MCP resource (T6), and any future Slack
      mirror (G6.2) — every consumer deserialises back to a
      :class:`BroadcastEvent`.
    * ``maxlen=BROADCAST_MAXLEN`` + ``approximate=True`` — the ``MAXLEN ~``
      form per the Valkey docs. Best-effort retention is acceptable for
      the broadcast feed; strict ``MAXLEN`` would force a slow per-call
      O(n) trim and the broadcast subscribers are already designed
      around at-most-once delivery (see :doc:`module docstring`).

    Failure handling: any exception from :func:`get_broadcast_client`
    or :meth:`Redis.xadd` is logged at warning level with the
    exception class only (no error message — redis-py exceptions can
    embed URL substrings that would leak the broadcast endpoint to
    log shippers), the error counter is incremented, and the call
    returns silently. The caller (AuditMiddleware on the HTTP path,
    MCP handlers on the JSON-RPC path) MUST NOT depend on this
    succeeding — the audit row is the canonical record.

    Parameters
    ----------
    event:
        The frozen :class:`BroadcastEvent`. Its ``payload`` field MUST
        already be the redacted view per
        :func:`~meho_backplane.broadcast.events.redact_payload`; this
        publisher does not re-redact, doesn't inspect the payload, and
        doesn't enforce the PII contract. See the events module
        docstring for why the contract is enforced upstream.
    """
    try:
        client = get_broadcast_client()
        await client.xadd(
            _stream_key(event.tenant_id),
            {"event": event.model_dump_json()},
            maxlen=BROADCAST_MAXLEN,
            approximate=True,
        )
    except Exception as exc:
        _log.warning(
            "broadcast_publish_failed",
            error_class=type(exc).__name__,
            tenant_id=str(event.tenant_id),
            op_id=event.op_id,
        )
        BROADCAST_PUBLISH_ERRORS_TOTAL.inc()
        return
    BROADCAST_EVENTS_PUBLISHED_TOTAL.labels(
        op_class=event.op_class,
        result_status=event.result_status,
    ).inc()
