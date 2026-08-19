# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Prometheus instrumentation for the backplane.

The default ``prometheus_client`` registry already auto-registers the
process collector (``process_resident_memory_bytes``,
``process_open_fds``, ``process_cpu_seconds_total``, …) and the GC
collector (``python_gc_objects_collected_total``, …). On top of those
this module defines the request-facing application metrics, all
observed at the :class:`~meho_backplane.middleware.RequestContextMiddleware`
seam or by the background schedulers:

* ``http_requests_total`` (Counter) — requests served, labelled by
  ``method`` / ``path`` / ``status``.
* ``http_request_duration_seconds`` (Histogram) — request handling
  latency in seconds, same label set, so p50/p90/p99 latency is
  answerable directly from Prometheus (``histogram_quantile``) instead
  of SQL over ``audit_log.duration_ms``.
* ``topology_refresh_total`` (Counter) — scheduled topology-refresh
  attempts, by ``outcome``.
* ``advisory_lock_busy_total`` (Counter) — background-loop ticks
  skipped because a PG advisory lock was already held, by ``subsystem``.

Further application counters — the ``broadcast_*_total`` family — live
next to the code they measure in :mod:`meho_backplane.broadcast`.

Cardinality contract
--------------------
The ``path`` label on ``http_requests_total`` and
``http_request_duration_seconds`` is bounded. The middleware labels by
the matched FastAPI route template (``/items/{id}`` — never the literal
``/items/42`` / ``/items/43`` / …), and collapses every request that
matched no route to a single constant value
(:data:`meho_backplane.middleware.UNMATCHED_ROUTE_LABEL`,
``path="__unmatched__"``). That fold is what stops an unauthenticated
404 scan spraying distinct URLs from exploding label cardinality on the
unauthenticated ``/metrics`` endpoint. The literal request path is
never used as a metric label; it is recorded only on the structured
``request_completed`` / ``request_failed`` log line — not a bounded
label set — where it stays available for forensics.

The exposition format intentionally pins the legacy
``text/plain; version=0.0.4; charset=utf-8`` content type via
:data:`prometheus_client.CONTENT_TYPE_PLAIN_0_0_4`. ``CONTENT_TYPE_LATEST``
in ``prometheus_client>=0.21`` advertises ``version=1.0.0``; while
modern Prometheus servers accept both, version 0.0.4 is the format
supported by every Prometheus deployment in the wild and is what
Goal #11's acceptance criterion specifies.
"""

from __future__ import annotations

from prometheus_client import (
    CONTENT_TYPE_PLAIN_0_0_4,
    REGISTRY,
    Counter,
    Histogram,
    generate_latest,
)

#: Counter for the total number of HTTP requests served by the
#: backplane, partitioned by ``method``, ``path``, and ``status``.
#:
#: Module-level instantiation is intentional and matches the
#: ``prometheus_client`` library contract — the underlying
#: ``CollectorRegistry`` complains if a metric with the same name is
#: registered twice in the same process, so this object must be a
#: singleton for the application's lifetime.
HTTP_REQUESTS_TOTAL: Counter = Counter(
    "http_requests_total",
    "Total HTTP requests served by the backplane.",
    labelnames=("method", "path", "status"),
)

#: Histogram of HTTP request handling latency in **seconds**, labelled by
#: ``method`` / ``path`` / ``status`` — the same label set and the same
#: middleware seam as :data:`HTTP_REQUESTS_TOTAL`
#: (:class:`meho_backplane.middleware.RequestContextMiddleware`). Lets
#: p50/p90/p99 request latency be read from Prometheus directly via
#: ``histogram_quantile`` instead of SQL over ``audit_log.duration_ms``
#: (#2886). No explicit ``buckets`` argument, so it inherits
#: ``prometheus_client``'s default latency buckets (5 ms … 10 s plus the
#: implicit ``+Inf``) — the canonical range for web request-duration
#: seconds. The ``path`` label is bounded exactly as the counter's
#: (matched route template, else the
#: :data:`meho_backplane.middleware.UNMATCHED_ROUTE_LABEL` constant), so
#: the histogram's per-bucket series cannot be fanned out by a 404 scan.
#: Same module-level-singleton rationale as :data:`HTTP_REQUESTS_TOTAL`.
HTTP_REQUEST_DURATION_SECONDS: Histogram = Histogram(
    "http_request_duration_seconds",
    "HTTP request handling latency in seconds.",
    labelnames=("method", "path", "status"),
)

#: Counter for scheduled topology-refresh attempts, partitioned by
#: ``outcome`` (``ok`` / ``error`` / ``skipped_locked``). G9.1-T3
#: (#450): the background scheduler increments this per (tenant, target)
#: iteration so a stuck connector or a permanently-contended advisory
#: lock surfaces on the ``/metrics`` scrape rather than only in logs.
#: Same module-level-singleton rationale as ``HTTP_REQUESTS_TOTAL``.
TOPOLOGY_REFRESH_TOTAL: Counter = Counter(
    "topology_refresh_total",
    "Scheduled topology-refresh attempts by outcome.",
    labelnames=("outcome",),
)

#: Counter for background-loop ticks skipped because the subsystem's
#: process-wide PG advisory lock was already held, partitioned by
#: ``subsystem`` (``sensor_runner`` / ``scheduler`` / ``event_drain`` /
#: ``agent_run_reaper`` / ``gateway_deadman``). #3010: a lock-miss tick
#: used to be a silent ``return 0`` — a stranded lock (the
#: cross-connection unlock leak) starved the sensor runner to ~35-50 %
#: cadence with no signal anywhere. Incremented by
#: :func:`meho_backplane.db.advisory.advisory_lock` so a sustained
#: non-zero rate on a single-replica deploy is directly alertable.
#: Same module-level-singleton rationale as ``HTTP_REQUESTS_TOTAL``.
ADVISORY_LOCK_BUSY_TOTAL: Counter = Counter(
    "advisory_lock_busy_total",
    "Background-loop ticks skipped because the advisory lock was held.",
    labelnames=("subsystem",),
)


def render_metrics() -> tuple[bytes, str]:
    """Render the default registry as Prometheus exposition bytes.

    Returns:
        ``(body, content_type)`` — ``body`` is the UTF-8 encoded
        Prometheus text exposition; ``content_type`` is the legacy
        ``text/plain; version=0.0.4; charset=utf-8`` MIME type. The
        FastAPI ``/metrics`` route wraps this into a
        :class:`fastapi.Response`.
    """
    return generate_latest(REGISTRY), CONTENT_TYPE_PLAIN_0_0_4
