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
* ``background_loop_last_tick_timestamp_seconds`` (Gauge) — Unix
  timestamp of each lifespan background loop's last completed tick, by
  ``loop``; its companion ``background_loop_interval_seconds`` (Gauge)
  publishes each loop's cadence so a staleness alert can threshold at
  ``N x interval`` per loop (#2888).

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

import time

from prometheus_client import (
    CONTENT_TYPE_PLAIN_0_0_4,
    REGISTRY,
    Counter,
    Gauge,
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

#: ``loop`` label vocabulary for the two background-loop liveness gauges
#: below — one value per lifespan-owned loop started in
#: :func:`meho_backplane.main._start_background_tasks`:
#: ``topology_scheduler``, ``memory_expiry``, ``topology_history``,
#: ``announcement_retention``, ``evidence_retention``, ``grant_expiry``,
#: ``approval_expiry``, ``scheduler``, ``sensor_runner``,
#: ``sensor_watchdog``, ``agent_run_reaper``, ``event_drain``,
#: ``gateway_deadman``. A loop that never calls :func:`note_loop_tick` is
#: invisible to the staleness alert — exactly the silent-stall class this
#: family exists to catch — so any new lifespan loop must both stamp here
#: and be added to this list.

#: Unix timestamp (seconds) of the last completed tick per lifespan
#: background loop, labelled by ``loop``. Stamped by :func:`note_loop_tick`
#: at the end of every completed iteration, including no-op / heartbeat
#: ticks — the same "a tick that did no work still proves the loop alive"
#: semantics the sensor-runner watchdog uses (``checks/watchdog.py``). A
#: wedged or dead loop stops advancing its stamp while healthy loops move
#: on, so ``time() - background_loop_last_tick_timestamp_seconds`` is the
#: staleness the ``MehoBackgroundLoopStalled`` alert trips on. The stamp is
#: per process and scraped per pod, so a single wedged replica surfaces
#: without a persisted fleet-level write masking it behind healthy
#: siblings. Same module-level-singleton rationale as
#: ``HTTP_REQUESTS_TOTAL``.
BACKGROUND_LOOP_LAST_TICK: Gauge = Gauge(
    "background_loop_last_tick_timestamp_seconds",
    "Unix timestamp of the last completed tick, per lifespan background loop.",
    labelnames=("loop",),
)

#: Configured tick interval (seconds) of each background loop, labelled by
#: ``loop`` and re-published by :func:`note_loop_tick` every tick so it
#: tracks a live settings change. It exists so the staleness alert can
#: threshold *per loop* at ``N x this loop's interval`` without the chart
#: enumerating loop names or baking in intervals that would drift from the
#: deployment's settings: the alert is one rule,
#: ``(time() - last_tick) > (N * interval)``, matched element-wise on the
#: ``loop`` label. Same module-level-singleton rationale as
#: ``HTTP_REQUESTS_TOTAL``.
BACKGROUND_LOOP_INTERVAL_SECONDS: Gauge = Gauge(
    "background_loop_interval_seconds",
    "Configured tick interval in seconds, per lifespan background loop.",
    labelnames=("loop",),
)


def note_loop_tick(loop: str, interval_seconds: float, *, now: float | None = None) -> None:
    """Stamp a completed background-loop tick and (re)publish its interval.

    Call at the end of every completed iteration of a lifespan-owned
    background loop — including no-op / heartbeat iterations — so a wedged
    loop's :data:`BACKGROUND_LOOP_LAST_TICK` stamp goes stale while healthy
    loops keep advancing theirs. ``interval_seconds`` is the loop's current
    sleep cadence; it feeds :data:`BACKGROUND_LOOP_INTERVAL_SECONDS` so the
    staleness alert can threshold at ``N x interval`` per loop. Setting a
    gauge is an in-memory dict write, so this never blocks and never raises
    on its own — safe on the hot path of a loop that must not die. ``now``
    (Unix seconds) is injectable for deterministic tests; it defaults to
    the wall clock.
    """
    stamp = now if now is not None else time.time()
    BACKGROUND_LOOP_LAST_TICK.labels(loop=loop).set(stamp)
    BACKGROUND_LOOP_INTERVAL_SECONDS.labels(loop=loop).set(interval_seconds)


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
