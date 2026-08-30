# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Curated read ops exposed by :class:`TempoConnector` (#2903).

The trace-read core an RDC operator needs to triage the tracing half of an
LGTM stack through the same dispatch -> policy-gate -> audit seam every other
connector uses, without reaching for the Grafana datasource proxy or raw
``curl`` against ``:3200``:

* ``tempo.search`` -- ``GET /api/search``; a TraceQL (``q``) or tag-based
  search that returns matching traces.
* ``tempo.trace`` -- ``GET /api/traces/{trace_id}``; fetch one full trace by
  its id.
* ``tempo.search_tags`` -- ``GET /api/v2/search/tags``; the attribute (tag)
  names known to Tempo, by scope.
* ``tempo.search_tag_values`` -- ``GET /api/v2/search/tag/{tag}/values``; the
  values a given tag takes.
* ``tempo.metrics_query_range`` -- ``GET /api/metrics/query_range``; a TraceQL
  metrics query over a time range (RED-style series from traces).
* ``tempo.get`` -- a GET passthrough to any other read endpoint under ``/api``
  (gated by
  :func:`~meho_backplane.connectors.tempo.read_only.assert_tempo_read_only`).

Every op is ``safety_level="safe"`` + ``requires_approval=False`` and carries a
``read-only`` tag -- Tempo has no operator-facing write API (ingest is an OTLP
push from collectors, not a query surface), so the connector registers no
write/mutating op.

Multi-tenancy: every op accepts an optional ``tenant`` selector that the
handler renders into the per-call ``X-Scope-OrgID`` header. It is required only
when the target's Tempo is multi-tenant; a single-tenant / monolithic Tempo
needs none. The dataclass + tuple shape mirrors the loki (#2235) sibling so the
registration walk reads identically.

Endpoint + parameter facts are pinned to the Tempo HTTP API reference
(https://grafana.com/docs/tempo/latest/api_docs/, Tempo 2.x): ``start``/``end``
are unix epoch **seconds**; the v2 tags/tag-values endpoints wrap their result
under ``{"scopes":[...]}`` / ``{"tagValues":[...]}`` with a trailing
``metrics`` block; search returns ``{"traces":[...],"metrics":{...}}``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

__all__ = ["TEMPO_OPS", "TEMPO_WHEN_TO_USE_BY_GROUP", "TempoOp"]


@dataclass(frozen=True)
class TempoOp:
    """Metadata for one Tempo op the connector registers at startup.

    Fields mirror the keyword arguments
    :func:`~meho_backplane.operations.typed_register.register_typed_operation`
    accepts so the registrar can splat the dataclass into the helper.
    ``handler_attr`` is the async-handler attribute name on
    :class:`~meho_backplane.connectors.tempo.connector.TempoConnector`.
    """

    op_id: str
    handler_attr: str
    summary: str
    description: str
    parameter_schema: dict[str, Any]
    response_schema: dict[str, Any] | None
    group_key: str | None
    tags: tuple[str, ...]
    safety_level: Literal["safe", "caution", "dangerous", "destructive"]
    requires_approval: bool
    llm_instructions: dict[str, Any] | None


#: Curated ``when_to_use`` blurbs per group. ``register_typed_operation``
#: requires a non-empty string whenever ``group_key`` is set; the registrar
#: looks each op's ``group_key`` up here.
TEMPO_WHEN_TO_USE_BY_GROUP: dict[str, str] = {
    "tempo-search": (
        "Use to find and fetch distributed traces in Grafana Tempo: a TraceQL "
        "or tag search to locate traces (tempo.search), a fetch of one full "
        "trace by id (tempo.trace), or a GET of any other read endpoint under "
        "/api (tempo.get). The right group for 'find the trace(s) for this "
        "request / error / slow call' or 'show me trace <id>'. Read-only. Pass "
        "'tenant' when the Tempo is multi-tenant (X-Scope-OrgID)."
    ),
    "tempo-metadata": (
        "Use to discover which attributes (tags) exist in Tempo before writing "
        "a TraceQL query: list tag names by scope (tempo.search_tags) or the "
        "values a tag takes (tempo.search_tag_values). The right group for "
        "building or debugging a selector like '{ .service.name = \"api\" }', "
        "or 'which services / attributes report to this Tempo?'. Read-only. "
        "Pass 'tenant' on a multi-tenant Tempo."
    ),
    "tempo-metrics": (
        "Use to compute TraceQL metrics (RED-style rates, error ratios, "
        "quantiles) directly from trace data over a time range "
        "(tempo.metrics_query_range) — e.g. 'the request rate / p99 latency / "
        "error rate for this service, from traces'. Requires the target's "
        "metrics-generator local-blocks processor (the op errors otherwise). "
        "Read-only. Pass 'tenant' on a multi-tenant Tempo."
    ),
}


# ---------------------------------------------------------------------------
# Shared parameter-schema fragments
# ---------------------------------------------------------------------------

#: The per-call multi-tenancy selector rendered into ``X-Scope-OrgID``.
_TENANT_PROPERTY: dict[str, Any] = {
    "type": "string",
    "minLength": 1,
    "description": (
        "Tempo tenant id, rendered into the per-call X-Scope-OrgID header. "
        "Required when the target's Tempo is multi-tenant; omit for a "
        "single-tenant or monolithic Tempo."
    ),
}

#: A start/end time bound. Tempo's query API expects unix epoch **seconds**.
_TIME_BOUND_SECONDS: dict[str, Any] = {
    "type": ["integer", "string"],
    "description": (
        "A time bound as a unix epoch timestamp in seconds "
        "(e.g. 1720000000). Bounds the blocks Tempo searches."
    ),
}

#: The ``limit`` cap shared by search + metadata ops.
_LIMIT_PROPERTY: dict[str, Any] = {
    "type": "integer",
    "minimum": 1,
    "description": "Maximum number of results to return.",
}

#: An optional TraceQL query that narrows a v2 tags / tag-values enumeration.
_TAG_FILTER_QUERY: dict[str, Any] = {
    "type": "string",
    "minLength": 1,
    "description": (
        "Optional TraceQL query to filter the returned tag names/values "
        "(e.g. '{ resource.service.name = \"api\" }'). Supports && and ||."
    ),
}

#: Early-termination threshold shared by the v2 metadata ops.
_MAX_STALE_VALUES: dict[str, Any] = {
    "type": "integer",
    "minimum": 0,
    "description": (
        "Search-termination threshold: stop after this many consecutive "
        "already-seen values. 0 (default) disables early termination."
    ),
}

#: The search envelope: matched traces plus a search-metrics summary.
_SEARCH_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "traces": {"type": ["array", "null"]},
        "metrics": {"type": "object"},
    },
    "additionalProperties": True,
}

#: The v2 tags envelope: scoped tag-name lists plus a metrics block.
_TAGS_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "scopes": {"type": ["array", "null"]},
        "metrics": {"type": "object"},
    },
    "additionalProperties": True,
}

#: The v2 tag-values envelope: typed value objects plus a metrics block.
_TAG_VALUES_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "tagValues": {"type": ["array", "null"]},
        "metrics": {"type": "object"},
    },
    "additionalProperties": True,
}

#: A TraceQL metrics result — a Prometheus-like time-series envelope.
_METRICS_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": True,
}


# ---------------------------------------------------------------------------
# tempo.search
# ---------------------------------------------------------------------------

_SEARCH = TempoOp(
    op_id="tempo.search",
    handler_attr="search",
    summary="Search Grafana Tempo for traces via TraceQL or tag filters.",
    description=(
        "Searches for traces via GET /api/search. Pass a TraceQL query in 'q' "
        "(e.g. '{ .service.name = \"api\" && status = error }') or use the "
        "tag-based filters ('tags' in logfmt, 'minDuration'/'maxDuration'). "
        "'start'/'end' (unix epoch seconds) bound the search window, 'limit' "
        "caps the trace count (Tempo default 20) and 'spss' caps spans per "
        "span-set (default 3). Returns matching traces with their root "
        "service/name and matched span-sets. Pass 'tenant' on a multi-tenant "
        "Tempo. safety_level=safe, read-only."
    ),
    parameter_schema={
        "type": "object",
        "properties": {
            "q": {
                "type": "string",
                "minLength": 1,
                "description": (
                    "A TraceQL query, e.g. '{ .service.name = \"api\" }'. "
                    "Prefer this over the tag filters for anything non-trivial."
                ),
            },
            "tags": {
                "type": "string",
                "minLength": 1,
                "description": (
                    "logfmt span/process attributes to filter on, matched as a "
                    "case-insensitive substring (e.g. 'service.name=api "
                    "http.status_code=500'). The pre-TraceQL search form."
                ),
            },
            "minDuration": {
                "type": "string",
                "description": "Only traces at least this long (Go duration, e.g. '100ms').",
            },
            "maxDuration": {
                "type": "string",
                "description": "Only traces at most this long (Go duration, e.g. '10s').",
            },
            "start": _TIME_BOUND_SECONDS,
            "end": _TIME_BOUND_SECONDS,
            "limit": _LIMIT_PROPERTY,
            "spss": {
                "type": "integer",
                "minimum": 1,
                "description": "Max spans per span-set to return (Tempo default 3).",
            },
            "tenant": _TENANT_PROPERTY,
        },
        "additionalProperties": False,
    },
    response_schema=_SEARCH_RESPONSE_SCHEMA,
    group_key="tempo-search",
    tags=("read-only", "tempo", "traceql", "traces", "search"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions={
        "when_to_use": (
            "The entry point for trace triage: find the traces matching a "
            "TraceQL query or tag filter. Follow with tempo.trace to fetch a "
            "full trace by the traceID a hit returns."
        ),
        "parameter_hints": {
            "q": "A TraceQL query (preferred).",
            "tags": "logfmt attribute filter (the older tag-search form).",
            "start": "Window start, unix epoch seconds.",
            "end": "Window end, unix epoch seconds.",
            "tenant": "Tenant id for X-Scope-OrgID; required on multi-tenant Tempo.",
        },
        "output_shape": (
            "{traces:[{traceID, rootServiceName, rootTraceName, "
            "startTimeUnixNano, durationMs, spanSets:[...]}], metrics:{...}}. "
            "Take traceID from a hit and pass it to tempo.trace. Page by "
            "lowering 'limit' or advancing 'end' back in time."
        ),
    },
)


# ---------------------------------------------------------------------------
# tempo.trace
# ---------------------------------------------------------------------------

_TRACE = TempoOp(
    op_id="tempo.trace",
    handler_attr="trace",
    summary="Fetch one full trace from Grafana Tempo by its trace id.",
    description=(
        "Fetches a single trace via GET /api/traces/{trace_id}, returning the "
        "full OpenTelemetry trace (all spans across all services). Optional "
        "'start'/'end' (unix epoch seconds) narrow the blocks searched, which "
        "speeds the lookup when the trace's time is known. Use after "
        "tempo.search to drill into a specific traceID. Pass 'tenant' on a "
        "multi-tenant Tempo. safety_level=safe, read-only."
    ),
    parameter_schema={
        "type": "object",
        "properties": {
            "trace_id": {
                "type": "string",
                "minLength": 1,
                "pattern": "^[0-9A-Fa-f]+$",
                "description": "The hex trace id to fetch (from a tempo.search hit).",
            },
            "start": _TIME_BOUND_SECONDS,
            "end": _TIME_BOUND_SECONDS,
            "tenant": _TENANT_PROPERTY,
        },
        "required": ["trace_id"],
        "additionalProperties": False,
    },
    response_schema={"type": "object", "additionalProperties": True},
    group_key="tempo-search",
    tags=("read-only", "tempo", "traces"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions={
        "when_to_use": (
            "Call with a traceID (from tempo.search) to fetch the complete "
            "trace. Supply start/end when known to bound the block scan."
        ),
        "parameter_hints": {
            "trace_id": "A hex trace id from a tempo.search result.",
            "tenant": "Tenant id for X-Scope-OrgID; required on multi-tenant Tempo.",
        },
        "output_shape": (
            "An OpenTelemetry trace as JSON (batches / resourceSpans of spans). "
            "Large — the result is returned as a JSONFlux handle for drill-in."
        ),
    },
)


# ---------------------------------------------------------------------------
# tempo.search_tags
# ---------------------------------------------------------------------------

_SEARCH_TAGS = TempoOp(
    op_id="tempo.search_tags",
    handler_attr="search_tags",
    summary="List the attribute (tag) names known to Tempo, by scope.",
    description=(
        "Lists the tag names Tempo knows about via GET /api/v2/search/tags, "
        "grouped by scope (resource / span / intrinsic / event / link / "
        "instrumentation). Optionally narrow by 'scope', a TraceQL 'q' filter, "
        "and a 'start'/'end' window. The entry point for building a TraceQL "
        "selector: which attribute keys exist. Pass 'tenant' on a multi-tenant "
        "Tempo. safety_level=safe, read-only."
    ),
    parameter_schema={
        "type": "object",
        "properties": {
            "scope": {
                "type": "string",
                "enum": [
                    "resource",
                    "span",
                    "intrinsic",
                    "event",
                    "link",
                    "instrumentation",
                ],
                "description": "Restrict to one attribute scope (default: all scopes).",
            },
            "q": _TAG_FILTER_QUERY,
            "start": _TIME_BOUND_SECONDS,
            "end": _TIME_BOUND_SECONDS,
            "limit": _LIMIT_PROPERTY,
            "maxStaleValues": _MAX_STALE_VALUES,
            "tenant": _TENANT_PROPERTY,
        },
        "additionalProperties": False,
    },
    response_schema=_TAGS_RESPONSE_SCHEMA,
    group_key="tempo-metadata",
    tags=("read-only", "tempo", "tags"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions={
        "when_to_use": (
            "Call first when building a TraceQL query and you don't yet know "
            "which attribute keys exist. Follow with tempo.search_tag_values to "
            "see a tag's values."
        ),
        "parameter_hints": {
            "scope": "Narrow to resource/span/intrinsic/... to shorten the list.",
            "tenant": "Tenant id for X-Scope-OrgID; required on multi-tenant Tempo.",
        },
        "output_shape": (
            "{scopes:[{name:'resource', tags:['service.name', ...]}, ...], metrics:{...}}."
        ),
    },
)


# ---------------------------------------------------------------------------
# tempo.search_tag_values
# ---------------------------------------------------------------------------

_SEARCH_TAG_VALUES = TempoOp(
    op_id="tempo.search_tag_values",
    handler_attr="search_tag_values",
    summary="List the values a given Tempo attribute (tag) takes.",
    description=(
        "Lists the values of one tag via GET /api/v2/search/tag/{tag}/values, "
        "each with its value type. Optionally narrow by a TraceQL 'q' filter "
        "and a 'start'/'end' window. Use after tempo.search_tags to enumerate a "
        "tag's values (e.g. every 'service.name'). The tag is the fully-scoped "
        "TraceQL attribute name (e.g. '.service.name', 'resource.k8s.pod.name'). "
        "Pass 'tenant' on a multi-tenant Tempo. safety_level=safe, read-only."
    ),
    parameter_schema={
        "type": "object",
        "properties": {
            "tag": {
                "type": "string",
                "minLength": 1,
                "pattern": "\\S",
                "description": (
                    "The scoped TraceQL attribute name whose values to list, "
                    "e.g. '.service.name' or 'resource.k8s.namespace.name'."
                ),
            },
            "q": _TAG_FILTER_QUERY,
            "start": _TIME_BOUND_SECONDS,
            "end": _TIME_BOUND_SECONDS,
            "limit": _LIMIT_PROPERTY,
            "maxStaleValues": _MAX_STALE_VALUES,
            "tenant": _TENANT_PROPERTY,
        },
        "required": ["tag"],
        "additionalProperties": False,
    },
    response_schema=_TAG_VALUES_RESPONSE_SCHEMA,
    group_key="tempo-metadata",
    tags=("read-only", "tempo", "tags"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions={
        "when_to_use": (
            "Call after tempo.search_tags to list the concrete values of a tag, "
            "so you can pin an exact TraceQL matcher like "
            "'{ .service.name = \"api\" }'."
        ),
        "parameter_hints": {
            "tag": "The scoped attribute name (from tempo.search_tags).",
            "tenant": "Tenant id for X-Scope-OrgID; required on multi-tenant Tempo.",
        },
        "output_shape": "{tagValues:[{type:'string', value:'api'}, ...], metrics:{...}}.",
    },
)


# ---------------------------------------------------------------------------
# tempo.metrics_query_range
# ---------------------------------------------------------------------------

_METRICS_QUERY_RANGE = TempoOp(
    op_id="tempo.metrics_query_range",
    handler_attr="metrics_query_range",
    summary="Run a TraceQL metrics query over a time range (RED-style series).",
    description=(
        "Runs a TraceQL metrics query over a time range via "
        "GET /api/metrics/query_range — RED-style rates/quantiles computed from "
        "trace data (e.g. "
        "'{ .service.name = \"api\" } | rate()'). 'start'/'end' bound the "
        "window (or 'since' for a relative lookback, default 1h), 'step' sets "
        "the series resolution, 'exemplars' caps returned exemplars. Requires "
        "the metrics-generator local-blocks processor enabled on the target. "
        "Pass 'tenant' on a multi-tenant Tempo. safety_level=safe, read-only."
    ),
    parameter_schema={
        "type": "object",
        "properties": {
            "q": {
                "type": "string",
                "minLength": 1,
                "pattern": "\\S",
                "description": (
                    "A TraceQL metrics query, e.g. '{ .service.name = \"api\" } | rate()'."
                ),
            },
            "start": _TIME_BOUND_SECONDS,
            "end": _TIME_BOUND_SECONDS,
            "since": {
                "type": "string",
                "description": "Relative lookback ('15m', '1h') used when 'start' is omitted.",
            },
            "step": {
                "type": "string",
                "description": "Series resolution as a duration, e.g. '15s'.",
            },
            "exemplars": {
                "type": "integer",
                "minimum": 0,
                "description": "Maximum number of exemplars to return.",
            },
            "tenant": _TENANT_PROPERTY,
        },
        "required": ["q"],
        "additionalProperties": False,
    },
    response_schema=_METRICS_RESPONSE_SCHEMA,
    group_key="tempo-metrics",
    tags=("read-only", "tempo", "traceql", "metrics"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions={
        "when_to_use": (
            "Use for a time series of a trace-derived metric (request rate, "
            "error rate, latency quantile) across a window. Requires the "
            "target's metrics-generator local-blocks processor."
        ),
        "parameter_hints": {
            "q": "A TraceQL metrics query (must include a metrics function like rate()).",
            "since": "Relative lookback ('1h') when start is omitted.",
            "step": "Series resolution ('15s').",
            "tenant": "Tenant id for X-Scope-OrgID; required on multi-tenant Tempo.",
        },
        "output_shape": "A Prometheus-like time-series result computed from traces.",
    },
)


# ---------------------------------------------------------------------------
# tempo.get (read-only passthrough)
# ---------------------------------------------------------------------------

_GET = TempoOp(
    op_id="tempo.get",
    handler_attr="get_passthrough",
    summary="GET any other read-only Tempo endpoint under /api.",
    description=(
        "Issues a GET against an arbitrary Tempo endpoint under /api — the "
        "escape hatch for JSON read endpoints without a curated op (e.g. "
        "/api/v2/traces/{id}, the v1 /api/search/tags). The path is gated: "
        "only GET, only under /api, so the passthrough can never mutate Tempo. "
        "Optional 'params' become query parameters. Pass 'tenant' on a "
        "multi-tenant Tempo. safety_level=safe, read-only."
    ),
    parameter_schema={
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "minLength": 1,
                "pattern": "^/api(/.*)?$",
                "description": (
                    "The read endpoint path under /api, e.g. "
                    "'/api/v2/traces/<id>'. Must start with /api."
                ),
            },
            "params": {
                "type": "object",
                "description": "Optional query parameters for the request.",
                "additionalProperties": True,
            },
            "tenant": _TENANT_PROPERTY,
        },
        "required": ["path"],
        "additionalProperties": False,
    },
    response_schema=None,
    group_key="tempo-search",
    tags=("read-only", "tempo", "passthrough"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions={
        "when_to_use": (
            "Use only when a needed read endpoint has no curated op. Prefer the "
            "curated ops (search / trace / search_tags / search_tag_values / "
            "metrics_query_range) when they fit. The path must be under /api."
        ),
        "parameter_hints": {
            "path": "A read path under /api.",
            "params": "A dict of query params.",
            "tenant": "Tenant id for X-Scope-OrgID; required on multi-tenant Tempo.",
        },
        "output_shape": "The endpoint's raw JSON body.",
    },
)


#: The ops :class:`TempoConnector` registers at lifespan startup. Ordered
#: search -> metadata -> metrics -> passthrough to match the operator's typical
#: drill path (find traces, then discover tags, then metrics).
TEMPO_OPS: tuple[TempoOp, ...] = (
    _SEARCH,
    _TRACE,
    _SEARCH_TAGS,
    _SEARCH_TAG_VALUES,
    _METRICS_QUERY_RANGE,
    _GET,
)
