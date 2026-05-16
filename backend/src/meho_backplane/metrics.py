# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Prometheus instrumentation for the backplane.

The default ``prometheus_client`` registry already auto-registers the
process collector (``process_resident_memory_bytes``,
``process_open_fds``, ``process_cpu_seconds_total``, …) and the GC
collector (``python_gc_objects_collected_total``, …). On top of those
we expose a single application metric in v0.1: ``http_requests_total``,
labelled by method, path, and HTTP status.

Path cardinality is bounded by the FastAPI router — every request is
matched to a registered route template before the middleware records
the metric, so a flood of distinct ``/foo/bar/<random>`` URLs from a
hostile client cannot explode label cardinality (FastAPI returns 404
without ever populating ``request.scope["route"].path``; the middleware
falls back to the literal request path for unmatched routes, which is
the documented Prometheus pattern for 404 buckets).

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
