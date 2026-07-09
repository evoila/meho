# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Curated read ops exposed by :class:`LokiConnector` (#2235).

The read core an RDC operator needs to triage the logs half of an LGTM stack
through the same dispatch -> policy-gate -> audit seam every other connector
uses, without reaching for ``logcli`` or raw ``curl`` against ``:3100``:

* ``loki.query`` -- ``GET /loki/api/v1/query``; a LogQL instant query at a
  single point in time.
* ``loki.query_range`` -- ``GET /loki/api/v1/query_range``; a LogQL query over
  a time range (the ``logcli query`` default).
* ``loki.labels`` -- ``GET /loki/api/v1/labels``; the known label names in a
  time span.
* ``loki.label_values`` -- ``GET /loki/api/v1/label/{name}/values``; the values
  a given label takes.
* ``loki.series`` -- ``GET /loki/api/v1/series``; the streams (unique label
  sets) matching one or more selectors.
* ``loki.get`` -- a GET passthrough to any other read endpoint under
  ``/loki/api/v1`` (gated by
  :func:`~meho_backplane.connectors.loki.read_only.assert_loki_read_only`).

Every op is ``safety_level="safe"`` + ``requires_approval=False`` and carries a
``read-only`` tag -- the connector registers no write/mutating op (``push`` /
``delete`` are blocked outright).

Multi-tenancy: every op accepts an optional ``tenant`` selector that the handler
renders into the per-call ``X-Scope-OrgID`` header. It is required only when the
target's Loki has ``auth_enabled`` (multi-tenant); a single-tenant / auth-disabled
Loki needs none. The dataclass + tuple shape mirrors the argocd (#1391) and
bind9 (#367) siblings so the registration walk reads identically.

Endpoint + parameter facts are pinned to the Loki HTTP API reference
(https://grafana.com/docs/loki/latest/reference/loki-http-api/): the query
endpoints wrap their result under ``{"status":"success","data":{...}}``; the
metadata endpoints (``labels`` / ``label/<name>/values`` / ``series``) return
``{"status":"success","data":[...]}``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

__all__ = ["LOKI_OPS", "LOKI_WHEN_TO_USE_BY_GROUP", "LokiOp"]


@dataclass(frozen=True)
class LokiOp:
    """Metadata for one Loki op the connector registers at startup.

    Fields mirror the keyword arguments
    :func:`~meho_backplane.operations.typed_register.register_typed_operation`
    accepts so the registrar can splat the dataclass into the helper.
    ``handler_attr`` is the async-handler attribute name on
    :class:`~meho_backplane.connectors.loki.connector.LokiConnector`.
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
#: requires a non-empty string whenever ``group_key`` is set; the registrar
#: looks each op's ``group_key`` up here.
LOKI_WHEN_TO_USE_BY_GROUP: dict[str, str] = {
    "loki-query": (
        "Use to run LogQL against Grafana Loki: an instant query at one point "
        "in time (loki.query), a query over a time range (loki.query_range, the "
        "usual choice for 'show me the logs / count between T1 and T2'), or a "
        "raw GET against any other read endpoint under /loki/api/v1 "
        "(loki.get). The right group when the question is 'what did this service "
        "log?' or 'how many errors in the last hour?'. Read-only — no push, no "
        "delete. Pass 'tenant' when the Loki is multi-tenant (auth_enabled)."
    ),
    "loki-metadata": (
        "Use to discover what labels and streams exist in Loki before writing a "
        "LogQL selector: list label names (loki.labels), list the values a label "
        "takes (loki.label_values), or list the streams (unique label sets) that "
        "match a selector (loki.series). The right group when building or "
        "debugging a '{app=\"...\"}' matcher, or answering 'which namespaces / "
        "pods report to this Loki?'. Read-only. Pass 'tenant' when the Loki is "
        "multi-tenant (auth_enabled)."
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
        "Loki tenant id, rendered into the per-call X-Scope-OrgID header. "
        "Required when the target's Loki has auth_enabled (multi-tenant); omit "
        "for a single-tenant or auth-disabled Loki. Pipe-separate ids "
        "('a|b') to query across tenants."
    ),
}

#: A LogQL query string (required by the two query ops).
_QUERY_PROPERTY: dict[str, Any] = {
    "type": "string",
    "minLength": 1,
    "pattern": "\\S",
    "description": 'The LogQL query, e.g. \'{app="api"} |= "error"\'.',
}

#: A start/end time bound. Loki accepts RFC3339 or a Unix timestamp (seconds or
#: nanoseconds), so both a string and an integer are allowed.
_TIME_BOUND: dict[str, Any] = {
    "type": ["string", "integer"],
    "description": (
        "A time bound as RFC3339 ('2026-07-09T00:00:00Z') or a Unix timestamp "
        "(seconds or nanoseconds)."
    ),
}

#: The ``direction`` enum shared by the two query ops.
_DIRECTION_PROPERTY: dict[str, Any] = {
    "type": "string",
    "enum": ["forward", "backward"],
    "description": "Sort order for returned entries (default 'backward').",
}

#: The ``limit`` cap shared by the two query ops.
_LIMIT_PROPERTY: dict[str, Any] = {
    "type": "integer",
    "minimum": 1,
    "description": "Max entries to return (Loki default 100).",
}

#: The success envelope the query endpoints return.
_QUERY_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "status": {"type": "string"},
        "data": {"type": "object"},
    },
    "additionalProperties": True,
}

#: The success envelope the metadata endpoints return (data is a string array).
_METADATA_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "status": {"type": "string"},
        "data": {"type": ["array", "null"]},
    },
    "additionalProperties": True,
}


# ---------------------------------------------------------------------------
# loki.query
# ---------------------------------------------------------------------------

_QUERY = LokiOp(
    op_id="loki.query",
    handler_attr="query",
    summary="Run a LogQL instant query against Loki at a single point in time.",
    description=(
        "Runs a LogQL instant query via GET /loki/api/v1/query — a query "
        "evaluated at a single instant (the 'time' param, default now). Use for "
        "a metric query that yields one vector (e.g. "
        "'sum(rate({app=\"api\"}[5m]))') or a quick log peek. For 'show me the "
        "logs between T1 and T2' prefer loki.query_range. Pass 'tenant' on a "
        "multi-tenant Loki. safety_level=safe, read-only."
    ),
    parameter_schema={
        "type": "object",
        "properties": {
            "query": _QUERY_PROPERTY,
            "time": _TIME_BOUND,
            "limit": _LIMIT_PROPERTY,
            "direction": _DIRECTION_PROPERTY,
            "tenant": _TENANT_PROPERTY,
        },
        "required": ["query"],
        "additionalProperties": False,
    },
    response_schema=_QUERY_RESPONSE_SCHEMA,
    group_key="loki-query",
    tags=("read-only", "loki", "logql", "logs"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions={
        "when_to_use": (
            "Call for an instant LogQL evaluation at one point in time — a "
            "single-vector metric query or a quick peek. Use loki.query_range "
            "for anything spanning a time window."
        ),
        "parameter_hints": {
            "query": "A LogQL expression.",
            "time": "Evaluation instant (RFC3339 or Unix ts); omit for now.",
            "tenant": "Tenant id for X-Scope-OrgID; required on multi-tenant Loki.",
        },
        "output_shape": (
            "{status:'success', data:{resultType, result:[...]}}. resultType is "
            "'streams' for a log query or 'vector'/'matrix' for a metric query."
        ),
    },
)


# ---------------------------------------------------------------------------
# loki.query_range
# ---------------------------------------------------------------------------

_QUERY_RANGE = LokiOp(
    op_id="loki.query_range",
    handler_attr="query_range",
    summary="Run a LogQL query against Loki over a time range.",
    description=(
        "Runs a LogQL query over a time range via GET /loki/api/v1/query_range "
        "— the everyday 'show me the logs / the error rate between start and "
        "end' op (what 'logcli query' issues). 'start'/'end' bound the window "
        "(Loki defaults to the last hour); 'step' sets the resolution for a "
        "metric query; 'direction' and 'limit' page log results. Pass 'tenant' "
        "on a multi-tenant Loki. safety_level=safe, read-only."
    ),
    parameter_schema={
        "type": "object",
        "properties": {
            "query": _QUERY_PROPERTY,
            "start": _TIME_BOUND,
            "end": _TIME_BOUND,
            "since": {
                "type": "string",
                "description": (
                    "A duration ('1h', '15m') used as the lookback when 'start' is omitted."
                ),
            },
            "step": {
                "type": "string",
                "description": (
                    "Query resolution step for metric queries (a duration like "
                    "'30s' or a float of seconds)."
                ),
            },
            "interval": {
                "type": "string",
                "description": "Return entries at most this far apart (a duration).",
            },
            "limit": _LIMIT_PROPERTY,
            "direction": _DIRECTION_PROPERTY,
            "tenant": _TENANT_PROPERTY,
        },
        "required": ["query"],
        "additionalProperties": False,
    },
    response_schema=_QUERY_RESPONSE_SCHEMA,
    group_key="loki-query",
    tags=("read-only", "loki", "logql", "logs"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions={
        "when_to_use": (
            "The default log-triage op: fetch logs or a metric series across a "
            "time window. Set start/end (or 'since' for a relative lookback) and "
            "'limit' to bound the result."
        ),
        "parameter_hints": {
            "query": "A LogQL expression.",
            "start": "Window start (RFC3339 or Unix ts).",
            "end": "Window end (RFC3339 or Unix ts); omit for now.",
            "since": "Relative lookback ('1h') when start is omitted.",
            "tenant": "Tenant id for X-Scope-OrgID; required on multi-tenant Loki.",
        },
        "output_shape": (
            "{status:'success', data:{resultType, result:[...]}}. For a log "
            "query each result entry carries 'stream' (labels) and 'values' "
            "([[ts, line], ...])."
        ),
        "pagination_hint": {
            "params": {
                "limit": "Cap on returned entries.",
                "start": "Advance the window start to page forward in time.",
                "end": "Advance the window end to page backward in time.",
            },
            "example_next_call": {
                "op_id": "loki.query_range",
                "params": {"query": '{app="api"}', "start": "<last-ts>", "limit": 100},
            },
        },
    },
)


# ---------------------------------------------------------------------------
# loki.labels
# ---------------------------------------------------------------------------

_LABELS = LokiOp(
    op_id="loki.labels",
    handler_attr="labels",
    summary="List the known label names in Loki within a time span.",
    description=(
        "Lists the label names Loki knows about via GET /loki/api/v1/labels, "
        "optionally bounded by 'start'/'end' (or 'since') and narrowed by a "
        "LogQL stream selector 'query'. The entry point for building a "
        "selector: which label keys exist. Pass 'tenant' on a multi-tenant "
        "Loki. safety_level=safe, read-only."
    ),
    parameter_schema={
        "type": "object",
        "properties": {
            "start": _TIME_BOUND,
            "end": _TIME_BOUND,
            "since": {
                "type": "string",
                "description": "Relative lookback ('1h') used when 'start' is omitted.",
            },
            "query": {
                "type": "string",
                "minLength": 1,
                "description": "Optional LogQL stream selector to narrow the labels.",
            },
            "tenant": _TENANT_PROPERTY,
        },
        "additionalProperties": False,
    },
    response_schema=_METADATA_RESPONSE_SCHEMA,
    group_key="loki-metadata",
    tags=("read-only", "loki", "labels"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions={
        "when_to_use": (
            "Call first when building a LogQL selector and you don't yet know "
            "which label keys exist. Follow with loki.label_values to see a "
            "label's values."
        ),
        "parameter_hints": {
            "tenant": "Tenant id for X-Scope-OrgID; required on multi-tenant Loki.",
        },
        "output_shape": "{status:'success', data:['app', 'namespace', ...]}.",
    },
)


# ---------------------------------------------------------------------------
# loki.label_values
# ---------------------------------------------------------------------------

_LABEL_VALUES = LokiOp(
    op_id="loki.label_values",
    handler_attr="label_values",
    summary="List the values a given label takes in Loki.",
    description=(
        "Lists the values of one label via "
        "GET /loki/api/v1/label/{name}/values, optionally bounded by "
        "'start'/'end' (or 'since') and narrowed by a LogQL stream selector "
        "'query'. Use after loki.labels to enumerate a label's values (e.g. all "
        "'namespace' values). Pass 'tenant' on a multi-tenant Loki. "
        "safety_level=safe, read-only."
    ),
    parameter_schema={
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "minLength": 1,
                "pattern": "\\S",
                "description": "The label name whose values to list, e.g. 'namespace'.",
            },
            "start": _TIME_BOUND,
            "end": _TIME_BOUND,
            "since": {
                "type": "string",
                "description": "Relative lookback ('1h') used when 'start' is omitted.",
            },
            "query": {
                "type": "string",
                "minLength": 1,
                "description": "Optional LogQL stream selector to narrow the values.",
            },
            "tenant": _TENANT_PROPERTY,
        },
        "required": ["name"],
        "additionalProperties": False,
    },
    response_schema=_METADATA_RESPONSE_SCHEMA,
    group_key="loki-metadata",
    tags=("read-only", "loki", "labels"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions={
        "when_to_use": (
            "Call after loki.labels to list the concrete values of a label, so "
            "you can pin an exact matcher like '{namespace=\"prod\"}'."
        ),
        "parameter_hints": {
            "name": "The label key (from loki.labels).",
            "tenant": "Tenant id for X-Scope-OrgID; required on multi-tenant Loki.",
        },
        "output_shape": "{status:'success', data:['prod', 'staging', ...]}.",
    },
)


# ---------------------------------------------------------------------------
# loki.series
# ---------------------------------------------------------------------------

_SERIES = LokiOp(
    op_id="loki.series",
    handler_attr="series",
    summary="List the streams (unique label sets) matching selectors in Loki.",
    description=(
        "Lists the streams — unique label sets — matching one or more LogQL "
        "selectors via GET /loki/api/v1/series. Each 'match' selector "
        "(e.g. '{app=\"api\"}') is sent as a repeated 'match[]' query param. Use "
        "to see exactly which streams a selector resolves to before running a "
        "query. Pass 'tenant' on a multi-tenant Loki. safety_level=safe, "
        "read-only."
    ),
    parameter_schema={
        "type": "object",
        "properties": {
            "match": {
                "type": "array",
                "items": {"type": "string", "minLength": 1},
                "minItems": 1,
                "description": (
                    "One or more LogQL stream selectors; each is sent as a "
                    "repeated 'match[]' query param."
                ),
            },
            "start": _TIME_BOUND,
            "end": _TIME_BOUND,
            "since": {
                "type": "string",
                "description": "Relative lookback ('1h') used when 'start' is omitted.",
            },
            "tenant": _TENANT_PROPERTY,
        },
        "required": ["match"],
        "additionalProperties": False,
    },
    response_schema={
        "type": "object",
        "properties": {
            "status": {"type": "string"},
            "data": {"type": ["array", "null"]},
        },
        "additionalProperties": True,
    },
    group_key="loki-metadata",
    tags=("read-only", "loki", "series"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions={
        "when_to_use": (
            "Call to resolve which concrete streams a selector matches — useful "
            "to confirm a matcher is neither empty nor overly broad before "
            "querying."
        ),
        "parameter_hints": {
            "match": "A list of LogQL selectors (at least one).",
            "tenant": "Tenant id for X-Scope-OrgID; required on multi-tenant Loki.",
        },
        "output_shape": (
            "{status:'success', data:[{label: value, ...}, ...]} — one object per matched stream."
        ),
    },
)


# ---------------------------------------------------------------------------
# loki.get (read-only passthrough)
# ---------------------------------------------------------------------------

_GET = LokiOp(
    op_id="loki.get",
    handler_attr="get_passthrough",
    summary="GET any other read-only Loki endpoint under /loki/api/v1.",
    description=(
        "Issues a GET against an arbitrary Loki endpoint under /loki/api/v1 — "
        "the escape hatch for read endpoints without a curated op (e.g. "
        "/loki/api/v1/index/stats, /loki/api/v1/patterns). The path is gated: "
        "only GET, only under /loki/api/v1, and the /push and /delete* surfaces "
        "are refused outright, so the passthrough can never mutate Loki. "
        "Optional 'params' become query parameters. Pass 'tenant' on a "
        "multi-tenant Loki. safety_level=safe, read-only."
    ),
    parameter_schema={
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "minLength": 1,
                "pattern": "^/loki/api/v1(/.*)?$",
                "description": (
                    "The read endpoint path under /loki/api/v1, e.g. "
                    "'/loki/api/v1/index/stats'. /push and /delete* are rejected."
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
    group_key="loki-query",
    tags=("read-only", "loki", "passthrough"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions={
        "when_to_use": (
            "Use only when a needed read endpoint has no curated op. Prefer the "
            "curated ops (query/query_range/labels/label_values/series) when they "
            "fit. The path must be under /loki/api/v1 and cannot be push/delete."
        ),
        "parameter_hints": {
            "path": "A read path under /loki/api/v1.",
            "params": "A dict of query params.",
            "tenant": "Tenant id for X-Scope-OrgID; required on multi-tenant Loki.",
        },
        "output_shape": "The endpoint's raw JSON body.",
    },
)


#: The ops :class:`LokiConnector` registers at lifespan startup. Ordered
#: query -> metadata -> passthrough to match the operator's typical drill path.
LOKI_OPS: tuple[LokiOp, ...] = (
    _QUERY,
    _QUERY_RANGE,
    _LABELS,
    _LABEL_VALUES,
    _SERIES,
    _GET,
)
