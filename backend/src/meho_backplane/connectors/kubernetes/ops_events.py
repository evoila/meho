# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Observability ops -- ``k8s.event.list``.

G3.2-T4 (#324) of Initiative #320. The "what's recently happened?"
read-only op layered on top of the T1 / T2 / T5 base substrate:

* ``k8s.event.list [--namespace X] [--field-selector ...] [--limit N]`` --
  ``CoreV1Api.list_namespaced_event``. Returns recent events ordered
  most-recent-first (descending ``last_seen``). Default ``limit=50``;
  ``--field-selector`` is forwarded verbatim to the K8s API so the
  operator can apply server-side filters like
  ``type=Warning,involvedObject.kind=Pod`` without the connector
  re-encoding the syntax.

The event surface lives in its own module (rather than alongside the
configmap ops in :mod:`ops_config`) for two reasons:

1. **File-size hygiene.** The code-quality gate caps files at 600
   lines; the combined configmap+event schemas + llm_instructions +
   helpers crossed that threshold.
2. **Operator mental model.** Events are observability, configmaps
   are configuration -- the parent Initiative's
   :data:`~meho_backplane.connectors.kubernetes.ops_events.K8S_EVENT_LIST_LLM_INSTRUCTIONS`
   "when to use" prose for events is closer to ``k8s.logs`` than to
   ``k8s.configmap.list``; future grouping in
   :func:`~meho_backplane.operations.search_operations` benefits
   from the separation.

Row-shape helpers (:func:`event_row`, :func:`sort_event_rows_recent_first`)
are pure functions over :mod:`kubernetes_asyncio.client.models`
instances so the unit tests pin the wire shape against synthetic
fixtures without booting an event loop.

References
----------
* Parent task: G3.2-T4 (#324).
* Parent Initiative: G3.2 (#320), kubernetes-asyncio typed connector.
* k8s Event API: https://kubernetes.io/docs/reference/kubernetes-api/cluster-resources/event-v1/
* ``kubernetes_asyncio.CoreV1Api``:
  https://github.com/tomplus/kubernetes_asyncio/blob/master/kubernetes_asyncio/docs/CoreV1Api.md
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from meho_backplane.connectors.kubernetes.ops import KubernetesOp

if TYPE_CHECKING:
    from kubernetes_asyncio.client.models import CoreV1Event

__all__ = [
    "DEFAULT_EVENT_LIMIT",
    "EVENT_OPS",
    "K8S_EVENT_LIST_LLM_INSTRUCTIONS",
    "K8S_EVENT_LIST_PARAMETER_SCHEMA",
    "K8S_EVENT_LIST_RESPONSE_SCHEMA",
    "MAX_EVENT_LIMIT",
    "event_last_seen_seconds_key",
    "event_row",
    "sort_event_rows_recent_first",
]


#: Default ``limit`` on ``k8s.event.list`` when the caller doesn't pick
#: one. Events accumulate quickly on a busy cluster (a single failing
#: pod easily produces 100+ ``BackOff`` events per hour) and the
#: operator's typical "what's happening?" question is satisfied by the
#: 50 most-recent rows.
DEFAULT_EVENT_LIMIT = 50

#: Hard ceiling on ``--limit``. The K8s API itself supports paginated
#: list with no upper bound on ``limit``, but 500 is the ergonomic cap
#: -- above that the operator almost certainly wants a server-side
#: field-selector filter, not "more events". Mirrors the same
#: "5000 tail / pick --since instead" discipline in ``k8s.logs``.
MAX_EVENT_LIMIT = 500


# ---------------------------------------------------------------------------
# Event row helpers
# ---------------------------------------------------------------------------


def _involved_object_row(obj: Any) -> dict[str, Any]:
    """Project an :class:`V1ObjectReference` into the event row's
    ``involved_object`` flat shape.

    Operators reading an event care about *what* the event is about --
    kind / name / namespace are enough for the "is this a pod issue?"
    triage question. The full ObjectReference also carries UID,
    resourceVersion, apiVersion, fieldPath; those are correctness
    details the dispatcher's structured ``unknown_op``/``connector_error``
    surface doesn't need.
    """
    if obj is None:
        return {"kind": None, "name": None, "namespace": None}
    return {
        "kind": obj.kind,
        "name": obj.name,
        "namespace": obj.namespace,
    }


def _event_source_string(event: CoreV1Event) -> str | None:
    """Coalesce the event's source into a single string operators recognise.

    Pre-1.27 K8s events use ``source.component`` (e.g. ``"kubelet"``)
    + ``source.host``; the 1.27+ EventSeries API uses
    ``reporting_component`` (e.g. ``"node-controller"``). Both surfaces
    are present on every ``CoreV1Event`` instance for backward compat;
    the wire shape forwards the first non-empty one so an operator
    looking at the row sees the same string ``kubectl describe`` shows.
    """
    source = event.source
    if source is not None and source.component:
        return str(source.component)
    if event.reporting_component:
        return str(event.reporting_component)
    return None


def _event_last_seen(event: CoreV1Event) -> datetime | None:
    """Best-effort "when did this event last fire?" timestamp.

    Pre-1.27 events use ``last_timestamp``; the 1.27+ EventSeries shape
    moves the timestamp to ``series.last_observed_time`` (recurring
    events) or ``event_time`` (single-shot events). The helper walks
    the most-recent-first preference order so an event from either API
    surface sorts correctly under ``sort_event_rows_recent_first``.
    """
    if event.last_timestamp is not None:
        result: datetime = event.last_timestamp
        return result
    series = event.series
    if series is not None and getattr(series, "last_observed_time", None) is not None:
        result = series.last_observed_time
        return result
    if event.event_time is not None:
        result = event.event_time
        return result
    return None


def _event_first_seen(event: CoreV1Event) -> datetime | None:
    """First-observed timestamp; falls back to event_time when first_timestamp
    is unset (the 1.27+ EventSeries shape).
    """
    if event.first_timestamp is not None:
        result: datetime = event.first_timestamp
        return result
    if event.event_time is not None:
        result = event.event_time
        return result
    return None


def _event_seconds_ago(ts: datetime | None, *, now: datetime | None = None) -> int | None:
    """Convert an event timestamp into "N seconds ago"."""
    if ts is None:
        return None
    reference = now if now is not None else datetime.now(UTC)
    delta = reference - ts
    return int(delta.total_seconds())


def event_row(
    event: CoreV1Event,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Project a :class:`CoreV1Event` into the wire dict shape."""
    metadata = event.metadata
    last_seen = _event_last_seen(event)
    first_seen = _event_first_seen(event)
    # ``count`` is None for the 1.27+ EventSeries singletons; coerce to
    # the operator-mental-model default of 1 (one observation) so the
    # row's type is stable.
    raw_count = event.count
    count: int = int(raw_count) if raw_count is not None else 1
    return {
        "name": metadata.name if metadata is not None else None,
        "namespace": metadata.namespace if metadata is not None else None,
        "type": event.type,
        "reason": event.reason,
        "message": event.message,
        "involved_object": _involved_object_row(event.involved_object),
        "source": _event_source_string(event),
        "count": count,
        "first_seen_seconds": _event_seconds_ago(first_seen, now=now),
        "last_seen_seconds": _event_seconds_ago(last_seen, now=now),
    }


def event_last_seen_seconds_key(row: dict[str, Any]) -> int:
    """Sort key: smaller ``last_seen_seconds`` (more recent) sorts first.

    Rows with ``last_seen_seconds=None`` (no usable timestamp on either
    API surface) sort to the end -- they're rare and the operator's
    "what's recent?" question is better answered by deterministically
    pushing the un-timestamped rows out of the top of the list.
    """
    raw = row.get("last_seen_seconds")
    if raw is None:
        # ``sys.maxsize`` keeps un-timestamped rows last while staying
        # int-typed (Python's ``<`` rejects ``int < None``).
        return sys.maxsize
    return int(raw)


def sort_event_rows_recent_first(
    rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Sort a row list most-recent-first by ``last_seen_seconds``.

    Stable sort -- rows with identical timestamps keep the API server's
    original ordering. v0.2's ``--limit`` truncation happens **after**
    the sort so the kept rows are deterministically the N most-recent,
    not "the first N the server returned".
    """
    return sorted(rows, key=event_last_seen_seconds_key)


# ---------------------------------------------------------------------------
# Op metadata -- schemas + llm_instructions + KubernetesOp row.
# ---------------------------------------------------------------------------


_NAMESPACE_PARAM_SCHEMA: dict[str, Any] = {
    "type": "string",
    "minLength": 1,
    "pattern": r"\S",
    "description": "Namespace to list within.",
}


K8S_EVENT_LIST_PARAMETER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "namespace": _NAMESPACE_PARAM_SCHEMA,
        "field_selector": {
            "type": "string",
            "minLength": 1,
            "pattern": r"\S",
            "description": (
                "K8s field selector forwarded server-side. "
                "Examples: 'type=Warning', "
                "'involvedObject.kind=Pod', "
                "'type=Warning,reason=BackOff'."
            ),
        },
        "limit": {
            "type": "integer",
            "minimum": 1,
            "maximum": MAX_EVENT_LIMIT,
            "default": DEFAULT_EVENT_LIMIT,
            "description": (
                f"Maximum rows to return (default {DEFAULT_EVENT_LIMIT}, "
                f"capped at {MAX_EVENT_LIMIT}). Rows are sorted "
                "most-recent-first; ``limit`` truncates the tail "
                "(the oldest events) after the sort."
            ),
        },
    },
    "required": ["namespace"],
    "additionalProperties": False,
}


K8S_EVENT_LIST_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "rows": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": ["string", "null"]},
                    "namespace": {"type": ["string", "null"]},
                    "type": {"type": ["string", "null"]},
                    "reason": {"type": ["string", "null"]},
                    "message": {"type": ["string", "null"]},
                    "involved_object": {
                        "type": "object",
                        "properties": {
                            "kind": {"type": ["string", "null"]},
                            "name": {"type": ["string", "null"]},
                            "namespace": {"type": ["string", "null"]},
                        },
                        "additionalProperties": False,
                    },
                    "source": {"type": ["string", "null"]},
                    "count": {"type": "integer", "minimum": 0},
                    "first_seen_seconds": {"type": ["integer", "null"]},
                    "last_seen_seconds": {"type": ["integer", "null"]},
                },
                "required": [
                    "name",
                    "namespace",
                    "type",
                    "reason",
                    "message",
                    "involved_object",
                    "source",
                    "count",
                    "first_seen_seconds",
                    "last_seen_seconds",
                ],
                "additionalProperties": False,
            },
        },
        "total": {"type": "integer"},
    },
    "required": ["rows", "total"],
    "additionalProperties": False,
}


K8S_EVENT_LIST_LLM_INSTRUCTIONS: dict[str, Any] = {
    "when_to_use": (
        "Call when the operator asks 'what's recently happened in "
        "<namespace>?', 'why did this pod restart?', or 'are there "
        "any warnings?'. Rows are sorted most-recent-first; pair with "
        "``field_selector='type=Warning'`` to scope the answer to "
        "operator-actionable signals."
    ),
    "parameter_hints": {
        "namespace": ("Required. The Kubernetes namespace whose events to list."),
        "field_selector": (
            "Optional. K8s field-selector string forwarded server-side "
            "(e.g. 'type=Warning', 'involvedObject.kind=Pod'). Multiple "
            "filters comma-separated."
        ),
        "limit": (
            f"Optional. Defaults to {DEFAULT_EVENT_LIMIT}, capped at "
            f"{MAX_EVENT_LIMIT}. Rows are sorted before truncation, so "
            "``limit`` always keeps the N most-recent events."
        ),
    },
    "output_shape": (
        "{'rows': [{name, namespace, type, reason, message, "
        "involved_object: {kind, name, namespace}, source, count, "
        "first_seen_seconds, last_seen_seconds}], 'total': <int>}. "
        "``type`` is 'Normal' or 'Warning'; ``count`` is how many "
        "times the event has fired; ``last_seen_seconds`` is how "
        "long ago the event was last observed."
    ),
}


EVENT_OPS: tuple[KubernetesOp, ...] = (
    KubernetesOp(
        op_id="k8s.event.list",
        handler_attr="k8s_event_list",
        summary="List recent Kubernetes events in a namespace, most-recent-first.",
        description=(
            "Calls ``CoreV1Api.list_namespaced_event(namespace, "
            "field_selector=..., limit=...)`` and projects each Event "
            "into {name, namespace, type, reason, message, "
            "involved_object: {kind, name, namespace}, source, count, "
            "first_seen_seconds, last_seen_seconds}. Rows are sorted "
            "most-recent-first by ``last_seen_seconds`` before "
            "``limit`` truncates the tail, so the kept rows are "
            "deterministically the N most-recent events. "
            "``field_selector`` is forwarded verbatim to the K8s API "
            "(e.g. 'type=Warning', 'involvedObject.kind=Pod'). "
            f"Default limit {DEFAULT_EVENT_LIMIT}, capped at "
            f"{MAX_EVENT_LIMIT}. Read-only."
        ),
        parameter_schema=K8S_EVENT_LIST_PARAMETER_SCHEMA,
        response_schema=K8S_EVENT_LIST_RESPONSE_SCHEMA,
        group_key="events",
        tags=("read-only", "events", "observability"),
        safety_level="safe",
        requires_approval=False,
        llm_instructions=K8S_EVENT_LIST_LLM_INSTRUCTIONS,
    ),
)
