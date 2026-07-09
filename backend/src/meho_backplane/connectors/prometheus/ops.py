# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Typed read operations exposed by :class:`PrometheusConnector`.

Initiative #2228 / Task #2234. Eight read-only ops over the Prometheus
HTTP API ``/api/v1`` surface -- the query verbs, the metadata verbs, the
monitoring-state verbs, and a constrained GET passthrough. Every op is
``safety_level="safe"``, ``requires_approval=False``, carries a
``read-only`` tag, and reaches the wire through
:meth:`~meho_backplane.connectors.prometheus.connector.PrometheusConnector._api_get`,
which enforces the GET-only + ``/api/v1/`` path-allowlist gate by
construction (a non-GET method or off-allowlist path is rejected before
any upstream call).

The three PromQL-HTTP-compatible backends this connector serves
(Prometheus, Thanos Query, Grafana Mimir/Cortex) expose the same
``/api/v1`` verbs, so one op surface covers all three. Backend-specific
availability differences (Thanos Query has no scrape ``targets``; Mimir
mounts the API under a ``/prometheus`` path prefix) are handled at the
connector level -- the op metadata is backend-agnostic.

The dataclass + tuple shape mirrors the argocd (#1391) and bind9 (#367)
connectors so the registration walk in
:meth:`PrometheusConnector.register_operations` reads identically to
those siblings.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

__all__ = ["PROMETHEUS_OPS", "PROMETHEUS_WHEN_TO_USE_BY_GROUP", "PrometheusOp"]


@dataclass(frozen=True)
class PrometheusOp:
    """Metadata for one prometheus op the connector registers at startup.

    Fields mirror the keyword arguments
    :func:`~meho_backplane.operations.typed_register.register_typed_operation`
    accepts so the registrar can splat the dataclass into the helper
    without per-op boilerplate. ``handler_attr`` is the attribute name on
    :class:`~meho_backplane.connectors.prometheus.connector.PrometheusConnector`
    that exposes the async handler; the registrar resolves the bound
    method against the class at registration time so the dispatcher's
    :func:`~meho_backplane.operations._handler_resolve.import_handler`
    walk can recover the callable from the persisted
    ``module.ClassName.method`` dotted path.
    """

    op_id: str
    handler_attr: str
    summary: str
    description: str
    parameter_schema: dict[str, Any]
    response_schema: dict[str, Any] | None
    group_key: str | None
    tags: tuple[str, ...]
    safety_level: Literal["safe", "caution", "dangerous"]
    requires_approval: bool
    llm_instructions: dict[str, Any] | None


#: Curated ``when_to_use`` blurbs per group. ``register_typed_operation``
#: requires a non-empty string whenever ``group_key`` is set (G0.9-T4a
#: #731); the registrar looks each op's ``group_key`` up here.
PROMETHEUS_WHEN_TO_USE_BY_GROUP: dict[str, str] = {
    "query": (
        "Use for evaluating PromQL against the metrics tier. Call "
        "``prometheus.query`` for an instant vector/scalar at a single "
        "moment ('what is the current value of X?'); call "
        "``prometheus.query_range`` for a time series over a window "
        "('how did X trend over the last hour?', for charting or "
        "rate-of-change analysis). These are the first stop when an "
        "operator asks 'why is this metric high/stale/flapping?'."
    ),
    "metadata": (
        "Use to discover what series and labels exist before writing a "
        "PromQL query. Call ``prometheus.series`` to enumerate series "
        "matching a set of selectors (with their full label sets); call "
        "``prometheus.labels`` to list the label names present in the "
        "TSDB. Reach for these when the operator does not yet know the "
        "exact metric or label to query."
    ),
    "monitoring": (
        "Use to inspect the monitoring control plane rather than metric "
        "values. Call ``prometheus.targets`` to see scrape-target health "
        "(up/down, last scrape, error) -- the first place to look when a "
        "feed goes stale; call ``prometheus.rules`` to list recording + "
        "alerting rule groups and their evaluation state; call "
        "``prometheus.alerts`` to list currently active (pending/firing) "
        "alerts. Note Thanos Query exposes no scrape targets."
    ),
    "passthrough": (
        "Use only when a specific read endpoint under ``/api/v1/`` is "
        "needed that the curated ops do not cover (e.g. "
        "``/api/v1/status/tsdb``, ``/api/v1/metadata``, "
        "``/api/v1/query_exemplars``). ``prometheus.get`` issues a GET "
        "constrained to the ``/api/v1/`` allowlist -- it cannot reach "
        "admin, TSDB-delete, or ``/-/reload`` endpoints. Prefer a "
        "curated op when one fits; this is the escape hatch."
    ),
}


_INSTANT_QUERY_OP = PrometheusOp(
    op_id="prometheus.query",
    handler_attr="query",
    summary="Evaluate a PromQL expression at a single instant.",
    description=(
        "Runs ``GET /api/v1/query`` with the given PromQL ``query`` and "
        "optional evaluation ``time`` (RFC3339 or Unix timestamp). "
        "Returns Prometheus's standard result envelope "
        "(``{resultType, result}``) where ``resultType`` is one of "
        "``vector`` / ``scalar`` / ``string`` / ``matrix``. Serves "
        "Prometheus, Thanos Query, and Mimir/Cortex identically."
    ),
    parameter_schema={
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "PromQL expression."},
            "time": {
                "type": "string",
                "description": "Evaluation timestamp (RFC3339 or Unix). Defaults to server now.",
            },
            "timeout": {"type": "string", "description": "Evaluation timeout, e.g. '30s'."},
        },
        "required": ["query"],
        "additionalProperties": False,
    },
    response_schema=None,
    group_key="query",
    tags=("read-only", "query", "promql", "prometheus"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions={
        "when_to_use": (
            "Call to read the current value of a metric or the result of "
            "any instant PromQL expression."
        ),
        "output_shape": (
            "``{resultType, result}``. For an instant vector, ``result`` "
            "is a list of ``{metric: {...labels}, value: [ts, 'val']}``."
        ),
    },
)


_RANGE_QUERY_OP = PrometheusOp(
    op_id="prometheus.query_range",
    handler_attr="query_range",
    summary="Evaluate a PromQL expression over a time range.",
    description=(
        "Runs ``GET /api/v1/query_range`` with ``query``, ``start``, "
        "``end`` (RFC3339 or Unix timestamps) and ``step`` (duration or "
        "float seconds). Returns a ``matrix`` result -- one series of "
        "``[timestamp, value]`` pairs per matching label set. Use for "
        "trends, rates, and charting over a window."
    ),
    parameter_schema={
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "PromQL expression."},
            "start": {"type": "string", "description": "Range start (RFC3339 or Unix)."},
            "end": {"type": "string", "description": "Range end (RFC3339 or Unix)."},
            "step": {
                "type": "string",
                "description": "Resolution step: duration ('15s') or float seconds.",
            },
            "timeout": {"type": "string", "description": "Evaluation timeout, e.g. '30s'."},
        },
        "required": ["query", "start", "end", "step"],
        "additionalProperties": False,
    },
    response_schema=None,
    group_key="query",
    tags=("read-only", "query", "promql", "prometheus"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions={
        "when_to_use": (
            "Call to read how a metric evolved across a window -- trends, "
            "rates, deltas, anything you would chart."
        ),
        "output_shape": (
            "``{resultType: 'matrix', result: [{metric, values: [[ts, 'val'], ...]}]}``."
        ),
    },
)


_SERIES_OP = PrometheusOp(
    op_id="prometheus.series",
    handler_attr="series",
    summary="List series matching one or more label selectors.",
    description=(
        "Runs ``GET /api/v1/series`` with one or more ``match`` series "
        "selectors and an optional ``start`` / ``end`` window. Returns "
        "the full label set of every matching series -- the way to "
        "discover which series exist before querying them."
    ),
    parameter_schema={
        "type": "object",
        "properties": {
            "match": {
                "type": "array",
                "items": {"type": "string"},
                "minItems": 1,
                "description": "Series selectors, e.g. ['up', 'node_load1{job=\"node\"}'].",
            },
            "start": {"type": "string", "description": "Window start (RFC3339 or Unix)."},
            "end": {"type": "string", "description": "Window end (RFC3339 or Unix)."},
        },
        "required": ["match"],
        "additionalProperties": False,
    },
    response_schema=None,
    group_key="metadata",
    tags=("read-only", "metadata", "prometheus"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions={
        "when_to_use": "Call to enumerate the series (and their labels) that match a selector.",
        "output_shape": "A list of label-set dicts, one per matching series.",
    },
)


_LABELS_OP = PrometheusOp(
    op_id="prometheus.labels",
    handler_attr="labels",
    summary="List the label names present in the TSDB.",
    description=(
        "Runs ``GET /api/v1/labels`` with an optional ``match`` selector "
        "list and ``start`` / ``end`` window. Returns the list of label "
        "names -- useful for discovering the dimensions available for "
        "querying and grouping."
    ),
    parameter_schema={
        "type": "object",
        "properties": {
            "match": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Optional series selectors scoping the label-name set.",
            },
            "start": {"type": "string", "description": "Window start (RFC3339 or Unix)."},
            "end": {"type": "string", "description": "Window end (RFC3339 or Unix)."},
        },
        "additionalProperties": False,
    },
    response_schema=None,
    group_key="metadata",
    tags=("read-only", "metadata", "prometheus"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions={
        "when_to_use": "Call to discover which label names exist across the metrics tier.",
        "output_shape": "A list of label-name strings.",
    },
)


_TARGETS_OP = PrometheusOp(
    op_id="prometheus.targets",
    handler_attr="targets",
    summary="List scrape targets and their health.",
    description=(
        "Runs ``GET /api/v1/targets`` with an optional ``state`` filter "
        "(``active`` / ``dropped`` / ``any``). Returns ``activeTargets`` "
        "(each with ``health``, ``lastScrape``, ``lastError``, "
        "``scrapeUrl``) and ``droppedTargets``. The first place to look "
        "when a feed goes stale. Thanos Query exposes no scrape targets."
    ),
    parameter_schema={
        "type": "object",
        "properties": {
            "state": {
                "type": "string",
                "enum": ["active", "dropped", "any"],
                "description": "Which target set to return. Defaults to server default.",
            },
        },
        "additionalProperties": False,
    },
    response_schema=None,
    group_key="monitoring",
    tags=("read-only", "monitoring", "prometheus"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions={
        "when_to_use": "Call to check scrape-target health -- up/down, last scrape, last error.",
        "output_shape": "``{activeTargets: [...], droppedTargets: [...]}``.",
    },
)


_RULES_OP = PrometheusOp(
    op_id="prometheus.rules",
    handler_attr="rules",
    summary="List recording and alerting rule groups.",
    description=(
        "Runs ``GET /api/v1/rules`` with an optional ``type`` filter "
        "(``alert`` / ``record``). Returns rule ``groups`` with each "
        "rule's evaluation state, health, and (for alerting rules) the "
        "active alerts it currently holds."
    ),
    parameter_schema={
        "type": "object",
        "properties": {
            "type": {
                "type": "string",
                "enum": ["alert", "record"],
                "description": "Restrict to alerting or recording rules. Defaults to both.",
            },
        },
        "additionalProperties": False,
    },
    response_schema=None,
    group_key="monitoring",
    tags=("read-only", "monitoring", "prometheus"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions={
        "when_to_use": "Call to inspect recording/alerting rule groups and their evaluation state.",
        "output_shape": "``{groups: [{name, file, rules: [...]}]}``.",
    },
)


_ALERTS_OP = PrometheusOp(
    op_id="prometheus.alerts",
    handler_attr="alerts",
    summary="List currently active alerts.",
    description=(
        "Runs ``GET /api/v1/alerts``. Returns the alerts currently "
        "active (``pending`` or ``firing``) with their labels, "
        "annotations, state, and ``activeAt`` timestamp."
    ),
    parameter_schema={
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    },
    response_schema=None,
    group_key="monitoring",
    tags=("read-only", "monitoring", "prometheus"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions={
        "when_to_use": "Call to see which alerts are currently pending or firing.",
        "output_shape": "``{alerts: [{labels, annotations, state, activeAt, value}]}``.",
    },
)


_GET_PASSTHROUGH_OP = PrometheusOp(
    op_id="prometheus.get",
    handler_attr="raw_get",
    summary="Constrained GET passthrough to any /api/v1/ read endpoint.",
    description=(
        "Issues a raw ``GET`` against a caller-supplied ``path`` with "
        "optional ``query`` params. The path is enforced to start with "
        "``/api/v1/`` and the method is GET-only, so admin / "
        "TSDB-delete / ``/-/reload`` endpoints are unreachable by "
        "construction. Use for read endpoints under ``/api/v1/`` the "
        "curated ops do not cover."
    ),
    parameter_schema={
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "pattern": "^/api/v1/",
                "description": "Request path -- must start with '/api/v1/'.",
            },
            "query": {
                "type": "object",
                "additionalProperties": True,
                "description": "Optional query parameters.",
            },
        },
        "required": ["path"],
        "additionalProperties": False,
    },
    response_schema=None,
    group_key="passthrough",
    tags=("read-only", "passthrough", "prometheus"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions={
        "when_to_use": (
            "Call only when no curated op fits the read endpoint you need under /api/v1/."
        ),
        "output_shape": "The verbatim JSON body the endpoint returns.",
    },
)


#: The ops :class:`PrometheusConnector` registers at lifespan startup --
#: the full Task #2234 read surface (eight ops). Each follow-on change is
#: "append a :class:`PrometheusOp` here"; the registration walk in
#: :meth:`PrometheusConnector.register_operations` does not change.
PROMETHEUS_OPS: tuple[PrometheusOp, ...] = (
    _INSTANT_QUERY_OP,
    _RANGE_QUERY_OP,
    _SERIES_OP,
    _LABELS_OP,
    _TARGETS_OP,
    _RULES_OP,
    _ALERTS_OP,
    _GET_PASSTHROUGH_OP,
)
