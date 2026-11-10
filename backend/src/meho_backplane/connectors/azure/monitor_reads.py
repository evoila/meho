# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Body of ``azure.vm.metric.list``: the hourly CPU and free memory of one VM.

The body behind a thin method of
:class:`~meho_backplane.connectors.azure.monitor.MonitorOps`. The operation
table and the guidance for the AI are in :mod:`.monitor`; the reading of the
metric answer and the numbers are in :mod:`.monitor_rows`; the many-VM summary
is in :mod:`.monitor_summary`.

It reads the VM first (its region, size and power state; a wrong ID answers
not found there), then the memory of its size, then the metrics from Resource
Manager in one call. The rows cover every hour of the window. An hour without a
sample has ``None``, never zero.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any, cast
from urllib.parse import quote

from meho_backplane.auth.operator import Operator
from meho_backplane.connectors.adapters.http import json_payload_or_empty
from meho_backplane.connectors.azure import monitor_calls
from meho_backplane.connectors.azure._http import send
from meho_backplane.connectors.azure._paths import (
    METRICS_API_VERSION,
    METRICS_PATH,
    VIRTUAL_MACHINE_PATH,
)
from meho_backplane.connectors.azure.compute_reads import vm_show_impl
from meho_backplane.connectors.azure.monitor_calls import (
    INTERVAL,
    REGION,
    Ctx,
    list_window,
    memory_by_size,
    vm_ref,
)
from meho_backplane.connectors.azure.monitor_rows import (
    CPU_METRIC,
    MEMORY_METRIC,
    HourlyMetrics,
    cpu_block,
    failed_text,
    hour_time,
    memory_block,
    parse_metrics,
    round_value,
    time_text,
    used_percent,
)
from meho_backplane.connectors.azure.session import AzureTargetLike, resolve_target_config

if TYPE_CHECKING:
    from meho_backplane.connectors.azure.connector import AzureConnector

__all__ = ["vm_metric_list_impl"]

_NO_MEMORY_SIZE = "The memory size of this VM size is unknown, so memory use is not worked out."


def _hour_rows(
    parsed: HourlyMetrics, start: datetime, hours: int, memory_gb: float | None
) -> list[dict[str, Any]]:
    known = memory_gb is not None and memory_gb > 0
    size = cast("float", memory_gb)
    rows: list[dict[str, Any]] = []
    for index in range(hours):
        cpu = parsed.cpu.get(index, {})
        free = parsed.memory.get(index, {})
        rows.append(
            {
                "time": hour_time(start, index),
                "cpu_average_percent": round_value(cpu.get("average")),
                "cpu_maximum_percent": round_value(cpu.get("maximum")),
                "memory_available_bytes_average": free.get("average"),
                "memory_available_bytes_minimum": free.get("minimum"),
                "memory_available_bytes_maximum": free.get("maximum"),
                "memory_used_percent_average": (
                    round_value(used_percent(free["average"], size))
                    if known and "average" in free
                    else None
                ),
                "memory_used_percent_peak": (
                    round_value(used_percent(free["minimum"], size))
                    if known and "minimum" in free
                    else None
                ),
            }
        )
    return rows


async def vm_metric_list_impl(
    connector: AzureConnector,
    operator: Operator,
    target: AzureTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``azure.vm.metric.list``: the hourly CPU and free memory of one VM."""
    subscription_id = resolve_target_config(target).subscription_id
    group, name = vm_ref(params.get("vm_id"), subscription_id, "vm_id")
    start, end = list_window(params, monitor_calls.utcnow())
    hours = int((end - start).total_seconds() // 3600)
    vm = await vm_show_impl(connector, operator, target, {"resource_group": group, "name": name})
    location = vm.get("location")
    sizes: dict[str, float] = {}
    memory_note: str | None = None
    if isinstance(location, str) and REGION.fullmatch(location.lower()):
        sizes, memory_note = await memory_by_size(
            Ctx(connector, operator, target), location.lower()
        )
    memory_gb = monitor_calls.memory_gb(sizes, vm.get("size"))
    if memory_gb is None and memory_note is None:
        memory_note = _NO_MEMORY_SIZE

    resource_uri = VIRTUAL_MACHINE_PATH.format(
        subscription_id=subscription_id,
        resource_group=quote(group, safe=""),
        vm_name=quote(name, safe=""),
    ).lstrip("/")
    response = await send(
        connector,
        target,
        "GET",
        METRICS_PATH.format(resource_uri=resource_uri),
        operator=operator,
        params={
            "api-version": METRICS_API_VERSION,
            "metricnames": f"{CPU_METRIC},{MEMORY_METRIC}",
            "timespan": f"{time_text(start)}/{time_text(end)}",
            "interval": INTERVAL,
            "aggregation": "Average,Minimum,Maximum",
        },
    )
    parsed = parse_metrics(json_payload_or_empty(response).get("value"), start, hours)
    cpu = cpu_block(parsed.cpu, hours)
    memory = memory_block(parsed.memory, memory_gb, hours)
    return {
        "value": _hour_rows(parsed, start, hours, memory_gb),
        "vm_id": vm.get("id"),
        "name": vm.get("name"),
        "resource_group": vm.get("resource_group"),
        "location": location,
        "size": vm.get("size"),
        "power_state": vm.get("power_state"),
        "window_from": time_text(start),
        "window_to": time_text(end),
        "interval": INTERVAL,
        "samples_expected": hours,
        "samples_found": cpu.samples_found,
        "coverage_percent": cpu.coverage_percent,
        "memory_samples_found": memory.samples_found,
        "memory_gb": memory_gb,
        "note": " ".join(t for t in (memory_note, failed_text(parsed)) if t) or None,
    }
