# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Prometheus Infrastructure Handler Mixin.

Handles CPU, memory, disk, and network metric queries.
All methods return summary statistics via data reduction module.

PromQL conventions:
- container_cpu_usage_seconds_total: counter, needs rate()
- container_memory_working_set_bytes: gauge, direct query (NOT usage_bytes)
- node_cpu_seconds_total: counter, needs rate()
- node_memory_*: gauge, direct query
- ALWAYS use container!="" filter to exclude cgroup-level aggregates (Pitfall 3)
"""

from typing import Any

from meho_app.modules.connectors.observability.data_reduction import summarize_time_series
from meho_app.modules.connectors.observability.time_range import TimeRange


class InfrastructureHandlerMixin:
    """Mixin for Prometheus infrastructure metric handlers."""

    # These will be provided by PrometheusConnector (base class)
    async def _query_range(self, query: str, time_range: TimeRange) -> list: ...

    async def _query_instant(self, query: str) -> list: ...

    # ==========================================================================
    # CPU Metrics
    # ==========================================================================

    async def _get_pod_cpu(self, params: dict[str, Any]) -> dict:
        """Get CPU usage per pod in a namespace."""
        namespace = params["namespace"]
        time_range = TimeRange.from_relative(params.get("time_range", "1h"))

        query = (
            f"sum(rate(container_cpu_usage_seconds_total"
            f'{{namespace="{namespace}",container!="",pod!=""}}[5m])) by (pod)'
        )
        result = await self._query_range(query, time_range)
        return summarize_time_series(result, "pod", "cpu_cores")

    async def _get_namespace_cpu(self, params: dict[str, Any]) -> dict:
        """Get total CPU usage for a namespace."""
        namespace = params["namespace"]
        time_range = TimeRange.from_relative(params.get("time_range", "1h"))

        query = (
            f"sum(rate(container_cpu_usage_seconds_total"
            f'{{namespace="{namespace}",container!=""}}[5m]))'
        )
        result = await self._query_range(query, time_range)
        return summarize_time_series(result, "namespace", "cpu_cores")

    async def _get_node_cpu(self, params: dict[str, Any]) -> dict:
        """Get CPU usage per node (1 - idle ratio)."""
        time_range = TimeRange.from_relative(params.get("time_range", "1h"))

        query = '1 - avg(rate(node_cpu_seconds_total{mode="idle"}[5m])) by (instance)'
        result = await self._query_range(query, time_range)
        return summarize_time_series(result, "instance", "cpu_usage_ratio")

    # ==========================================================================
    # Memory Metrics
    # ==========================================================================

    async def _get_pod_memory(self, params: dict[str, Any]) -> dict:
        """Get memory usage (working set) per pod in a namespace."""
        namespace = params["namespace"]
        time_range = TimeRange.from_relative(params.get("time_range", "1h"))

        # Use working_set_bytes, NOT usage_bytes (which includes inactive cache)
        query = (
            f"sum(container_memory_working_set_bytes"
            f'{{namespace="{namespace}",container!="",pod!=""}}) by (pod)'
        )
        result = await self._query_range(query, time_range)
        return summarize_time_series(result, "pod", "memory_bytes")

    async def _get_namespace_memory(self, params: dict[str, Any]) -> dict:
        """Get total memory usage (working set) for a namespace."""
        namespace = params["namespace"]
        time_range = TimeRange.from_relative(params.get("time_range", "1h"))

        query = f'sum(container_memory_working_set_bytes{{namespace="{namespace}",container!=""}})'
        result = await self._query_range(query, time_range)
        return summarize_time_series(result, "namespace", "memory_bytes")

    async def _get_node_memory(self, params: dict[str, Any]) -> dict:
        """Get memory usage per node (total - available)."""
        time_range = TimeRange.from_relative(params.get("time_range", "1h"))

        query = "(node_memory_MemTotal_bytes - node_memory_MemAvailable_bytes)"
        # We need grouping by instance for multi-node
        # node_memory_* metrics already have instance label
        result = await self._query_range(query, time_range)
        return summarize_time_series(result, "instance", "memory_used_bytes")

    # ==========================================================================
    # Disk & Network Metrics
    # ==========================================================================

    async def _get_disk_usage(self, params: dict[str, Any]) -> dict:
        """Get root filesystem disk usage ratio per node."""
        time_range = TimeRange.from_relative(params.get("time_range", "1h"))

        query = (
            '1 - (node_filesystem_avail_bytes{mountpoint="/",fstype!="tmpfs"}'
            ' / node_filesystem_size_bytes{mountpoint="/",fstype!="tmpfs"})'
        )
        result = await self._query_range(query, time_range)
        return summarize_time_series(result, "instance", "disk_usage_ratio")

    async def _get_network_io(self, params: dict[str, Any]) -> dict:
        """Get network receive and transmit rates per node."""
        time_range = TimeRange.from_relative(params.get("time_range", "1h"))

        # Exclude loopback and virtual interfaces
        device_filter = 'device!~"lo|veth.*|docker.*|br.*"'

        rx_query = (
            f"sum(rate(node_network_receive_bytes_total{{{device_filter}}}[5m])) by (instance)"
        )
        tx_query = (
            f"sum(rate(node_network_transmit_bytes_total{{{device_filter}}}[5m])) by (instance)"
        )

        rx_result = await self._query_range(rx_query, time_range)
        tx_result = await self._query_range(tx_query, time_range)

        rx_summary = summarize_time_series(rx_result, "instance", "receive_bytes_per_sec")
        tx_summary = summarize_time_series(tx_result, "instance", "transmit_bytes_per_sec")

        # Combine rx and tx summaries by instance
        tx_by_instance = {
            item["instance"]: item["transmit_bytes_per_sec"] for item in tx_summary.get("items", [])
        }

        for item in rx_summary.get("items", []):
            instance = item["instance"]
            if instance in tx_by_instance:
                item["transmit_bytes_per_sec"] = tx_by_instance[instance]

        return {
            "items": rx_summary.get("items", []),
            "total_count": rx_summary.get("total_count", 0),
            "showing": rx_summary.get("showing", 0),
            "metrics": ["receive_bytes_per_sec", "transmit_bytes_per_sec"],
        }
