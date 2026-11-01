# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``vmware.vsan.performance`` typed op -- vSAN latency, IOPS and throughput.

One vSAN call on the connector's own session (VI-JSON on vCenter 9.x, SOAP on
8.0, see :meth:`VmwareRestConnector._post_vsan`):
``VsanPerformanceManager.VsanPerfQueryPerf`` (object
``vsan-performance-manager``) with one ``VsanPerfQuerySpec``.

The spec names one vSAN entity as ``<entity-type>:<entity-id>``. The id is a
vSAN UUID (of the cluster, a host, a VM instance or a disk), or ``*`` for all
entities of that type in the cluster. Entity types and metric names: the
description of ``VsanPerfQueryPerf`` in the vendor VI/JSON file of vCenter 9.0.
vSAN keeps samples of five minutes for 90 days, and only while the vSAN
performance service is on.

The answer (``VsanPerfEntityMetricCSV[]``) holds the times of the samples as
one comma list (``sampleInfo``) and the values of each metric as another. The
op turns them into flat rows ``{entity, metric, time, value}``, so a long
answer rides a result handle and the agent can filter it with
``result_query``.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Final

import structlog

from meho_backplane.connectors.vmware_rest._mount import VsanMethod
from meho_backplane.connectors.vmware_rest.typed_ops import VmwareTypedOp
from meho_backplane.connectors.vmware_rest.typed_ops_vsan_common import (
    CLUSTER_PARAM_SCHEMA,
    VSAN_GROUP_KEY,
    VSAN_WHEN_TO_USE,
    as_float,
    as_text,
    cluster_ref,
    dict_items,
    text_items,
    vsan_call,
)

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors.vmware_rest.connector import VmwareRestConnector
    from meho_backplane.connectors.vmware_rest.session import VsphereTargetLike

__all__ = [
    "PERF_ENTITY_TYPES",
    "VMWARE_VSAN_PERFORMANCE_OP",
    "VSAN_PERF_QUERY_PERF",
    "build_perf_query_body",
    "vsan_performance_impl",
]

VSAN_PERF_QUERY_PERF: Final = VsanMethod(
    mo_type="VsanPerformanceManager",
    moid="vsan-performance-manager",
    method="VsanPerfQueryPerf",
)

#: The vSAN performance entity types an agent can ask for (vendor names).
PERF_ENTITY_TYPES: Final = (
    "cluster-domclient",
    "cluster-domcompmgr",
    "host-domclient",
    "host-domcompmgr",
    "virtual-machine",
    "virtual-disk",
    "vscsi",
    "disk-group",
    "cache-disk",
    "capacity-disk",
    "vsan-esa-disk-layer",
    "vsan-esa-disk-scsifw",
    "vsan-host-net",
    "vsan-vnic-net",
    "vsan-pnic-net",
    "vsan-cluster-capacity",
)
_DEFAULT_ENTITY_TYPE: Final = "cluster-domclient"
_ALL_ENTITIES: Final = "*"
_DEFAULT_MINUTES: Final = 60
_MAX_MINUTES: Final = 1440
_TIME_FORMAT: Final = "%Y-%m-%dT%H:%M:%SZ"

NO_SAMPLES_NOTE: Final = (
    "vSAN sent no samples. The vSAN performance service may be off, no entity of this "
    "type and id may exist in the cluster, or the time window may be too short."
)


def _log() -> Any:
    """Get the logger per call, so ``structlog.testing.capture_logs`` sees every event."""
    return structlog.get_logger(__name__)


def _window(params: dict[str, Any]) -> tuple[str, str]:
    """``(start, end)`` in UTC: *minutes* before *end_time* (default now)."""
    end_text = params.get("end_time")
    if isinstance(end_text, str) and end_text:
        end = datetime.fromisoformat(end_text)
        end = end.replace(tzinfo=UTC) if end.tzinfo is None else end.astimezone(UTC)
    else:
        end = datetime.now(UTC)
    start = end - timedelta(minutes=int(params.get("minutes", _DEFAULT_MINUTES)))
    return start.strftime(_TIME_FORMAT), end.strftime(_TIME_FORMAT)


def build_perf_query_body(
    cluster_moid: str,
    entity_ref_id: str,
    start: str,
    end: str,
    labels: list[str] | None,
) -> dict[str, Any]:
    """The ``VsanPerfQueryPerf`` body: one ``VsanPerfQuerySpec``, then the cluster.

    Keys follow the order the vendor types define; SOAP needs it.
    """
    spec: dict[str, Any] = {
        "_typeName": "VsanPerfQuerySpec",
        "entityRefId": entity_ref_id,
        "startTime": start,
        "endTime": end,
    }
    if labels:
        spec["labels"] = list(labels)
    return {"querySpecs": [spec], "cluster": cluster_ref(cluster_moid)}


def _csv(value: Any) -> list[str]:
    return [part.strip() for part in value.split(",")] if isinstance(value, str) and value else []


def _rows(entity_metric: dict[str, Any]) -> list[dict[str, Any]]:
    """Flat ``{entity, metric, time, value}`` rows of one ``VsanPerfEntityMetricCSV``."""
    entity = as_text(entity_metric.get("entityRefId"))
    times = _csv(entity_metric.get("sampleInfo"))
    rows: list[dict[str, Any]] = []
    for series in dict_items(entity_metric.get("value")):
        metric_id = series.get("metricId")
        metric = as_text(metric_id.get("label")) if isinstance(metric_id, dict) else None
        for time, value in zip(times, _csv(series.get("values")), strict=False):
            rows.append(
                {"entity": entity, "metric": metric, "time": time, "value": as_float(value)}
            )
    return rows


async def vsan_performance_impl(
    connector: VmwareRestConnector,
    operator: Operator,
    target: VsphereTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Implementation of ``vmware.vsan.performance``."""
    cluster_moid = params["cluster"]
    entity_type = params.get("entity_type") or _DEFAULT_ENTITY_TYPE
    entity_ref_id = f"{entity_type}:{params.get('entity_id') or _ALL_ENTITIES}"
    start, end = _window(params)
    labels = text_items(params.get("metrics")) or None
    answer = await vsan_call(
        connector,
        operator,
        target,
        VSAN_PERF_QUERY_PERF,
        build_perf_query_body(cluster_moid, entity_ref_id, start, end, labels),
        what="performance",
        empty_ok=True,
    )
    rows = [row for entity_metric in dict_items(answer) for row in _rows(entity_metric)]
    _log().info(
        "vmware_vsan_performance_read",
        target=target.name,
        cluster=cluster_moid,
        entity_type=entity_type,
        row_count=len(rows),
    )
    return {
        "cluster": cluster_moid,
        "entity": entity_ref_id,
        "start_time": start,
        "end_time": end,
        "rows": rows,
        "note": None if rows else NO_SAMPLES_NOTE,
    }


# ---------------------------------------------------------------------------
# Op metadata + schemas
# ---------------------------------------------------------------------------

_PARAMETER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "cluster": CLUSTER_PARAM_SCHEMA,
        "entity_type": {
            "type": "string",
            "enum": list(PERF_ENTITY_TYPES),
            "default": _DEFAULT_ENTITY_TYPE,
            "description": (
                "What to measure. cluster-domclient: the whole cluster as the VMs see it. "
                "host-domclient: one host. virtual-machine: one VM. disk-group, cache-disk, "
                "capacity-disk: OSA disks. vsan-esa-disk-layer: the ESA disks of a host. "
                "Default cluster-domclient."
            ),
        },
        "entity_id": {
            "type": "string",
            "minLength": 1,
            "default": _ALL_ENTITIES,
            "description": (
                "vSAN UUID of the entity: the cluster's vsan_cluster_uuid or a host's "
                "node_uuid (from vmware.vsan.cluster.info), a VM's instance UUID, or a disk "
                "UUID. Default '*': every entity of that type in the cluster."
            ),
        },
        "minutes": {
            "type": "integer",
            "minimum": 5,
            "maximum": _MAX_MINUTES,
            "default": _DEFAULT_MINUTES,
            "description": "Length of the time window in minutes, up to 1440. Default 60.",
        },
        "end_time": {
            "type": "string",
            "format": "date-time",
            "description": (
                "End of the time window, for example '2026-10-31T12:00:00Z'. Default now."
            ),
        },
        "metrics": {
            "type": "array",
            "items": {
                "type": "string",
                "minLength": 1,
                "description": "One metric name, for example 'latencyAvgRead'.",
            },
            "description": (
                "Metric names to return, for example ['iopsRead', 'latencyAvgRead']. Default: "
                "all metrics of the entity type."
            ),
        },
    },
    "required": ["cluster"],
    "additionalProperties": False,
    "examples": [
        {"cluster": "<cluster-id>"},
        {"cluster": "<cluster-id>", "entity_type": "host-domclient", "minutes": 120},
    ],
}

_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "cluster": {"type": "string", "description": "Managed object id of the cluster."},
        "entity": {
            "type": "string",
            "description": "The vSAN entity asked for, '<entity-type>:<entity-id>'.",
        },
        "start_time": {"type": "string", "description": "Start of the time window (UTC)."},
        "end_time": {"type": "string", "description": "End of the time window (UTC)."},
        "rows": {
            "type": "array",
            "description": "One row per entity, metric and sample time.",
            "items": {
                "type": "object",
                "properties": {
                    "entity": {
                        "type": ["string", "null"],
                        "description": "The entity, '<entity-type>:<uuid>'.",
                    },
                    "metric": {
                        "type": ["string", "null"],
                        "description": "Metric name, for example 'latencyAvgRead'.",
                    },
                    "time": {"type": "string", "description": "Time of the sample."},
                    "value": {
                        "type": ["number", "null"],
                        "description": (
                            "Value of the sample as vSAN sends it; null when vSAN has no value "
                            "for that time."
                        ),
                    },
                },
            },
        },
        "note": {
            "type": ["string", "null"],
            "description": "Plain text when vSAN sent no samples, else null.",
        },
    },
    "required": ["cluster", "entity", "start_time", "end_time", "rows", "note"],
}

VMWARE_VSAN_PERFORMANCE_OP: Final[VmwareTypedOp] = VmwareTypedOp(
    op_id="vmware.vsan.performance",
    handler_attr="vsan_performance",
    summary="vSAN latency, IOPS and throughput of a cluster, host, VM or disk over a time window.",
    description=(
        "Returns vSAN performance samples (latency, IOPS, throughput, congestion and more) "
        "for the cluster, a host, a VM or a disk, over a time window. One row per entity, "
        "metric and sample time."
    ),
    parameter_schema=_PARAMETER_SCHEMA,
    response_schema=_RESPONSE_SCHEMA,
    group_key=VSAN_GROUP_KEY,
    tags=("read-only", "vmware", "vcenter", "vsan", "performance", "latency"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions={
        "when_to_call": (
            "Call when vSAN is slow or busy: to see read and write latency, IOPS, throughput "
            "or congestion of the cluster, a host, a VM or a disk."
        ),
        "parameter_hints": {
            "cluster": "Cluster moid from vmware.cluster.list.",
            "entity_type": "cluster-domclient for the cluster, host-domclient for hosts.",
            "entity_id": "Leave out for all entities of the type; rows name each entity.",
            "minutes": "60 by default; samples are five minutes apart.",
            "metrics": "For example ['latencyAvgRead', 'latencyAvgWrite', 'iopsRead'].",
        },
        "output_shape": (
            "{cluster, entity, start_time, end_time, rows: [{entity, metric, time, value}], "
            "note}. Common metrics: iopsRead, iopsWrite, throughputRead, throughputWrite, "
            "latencyAvgRead, latencyAvgWrite, congestion, oio. Values are as vSAN sends "
            "them. A long list comes back as a result handle. note says why rows is empty. A "
            "failed vSAN call returns an error with vCenter's fault."
        ),
        "next_step": (
            "Use result_query to filter rows by metric or entity and find the highest values. "
            "For a host, map the UUID in entity to a host with vmware.vsan.cluster.info."
        ),
    },
)

TYPED_OPS: tuple[VmwareTypedOp, ...] = (VMWARE_VSAN_PERFORMANCE_OP,)
WHEN_TO_USE_BY_GROUP: dict[str, str] = {VSAN_GROUP_KEY: VSAN_WHEN_TO_USE}
