# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
PerformanceManager Handlers - Advanced Performance Metrics

This module provides access to vSphere's PerformanceManager API for detailed
disk I/O, network throughput, and historical metrics that aren't available
through quickStats.

The LLM can request metrics for different time intervals:
- realtime: 20-second samples, last 1 hour
- 5min: 5-minute rollups, last 24 hours
- 1hour: 1-hour rollups, last 7 days
- 6hour: 6-hour aggregates
- 12hour: 12-hour aggregates
- 24hour: Daily rollups
- 7day: Weekly summary

All complexity of counter IDs, query specs, and result parsing is hidden
from the LLM - it just asks for metrics and gets structured data back.
"""

from typing import Any, TypedDict

from meho_app.core.otel import get_logger

logger = get_logger(__name__)


class IntervalConfig(TypedDict):
    """Type definition for interval configuration."""

    interval_id: int
    max_samples: int
    description: str


# Key metric groups and their counter names
# These are the most diagnostic-relevant metrics from the 300+ available
METRIC_GROUPS = {
    "cpu": [
        "cpu.usage.average",  # CPU usage as percentage
        "cpu.usagemhz.average",  # CPU usage in MHz
        "cpu.ready.summation",  # CPU ready time (contention indicator)
        "cpu.wait.summation",  # CPU wait time
        "cpu.costop.summation",  # Co-stop time (SMP VMs)
    ],
    "memory": [
        "mem.usage.average",  # Memory usage percentage
        "mem.active.average",  # Active memory KB
        "mem.consumed.average",  # Consumed memory KB
        "mem.swapped.average",  # Swapped memory KB
        "mem.vmmemctl.average",  # Balloon driver KB
        "mem.granted.average",  # Granted memory KB
    ],
    "disk": [
        "disk.read.average",  # Read rate KB/s
        "disk.write.average",  # Write rate KB/s
        "disk.numberRead.summation",  # Read IOPS
        "disk.numberWrite.summation",  # Write IOPS
        "disk.totalReadLatency.average",  # Read latency ms
        "disk.totalWriteLatency.average",  # Write latency ms
        "disk.maxTotalLatency.latest",  # Max latency ms
    ],
    "network": [
        "net.received.average",  # Network receive KB/s
        "net.transmitted.average",  # Network transmit KB/s
        "net.packetsRx.summation",  # Packets received
        "net.packetsTx.summation",  # Packets transmitted
        "net.droppedRx.summation",  # Dropped receive packets
        "net.droppedTx.summation",  # Dropped transmit packets
    ],
    "datastore": [
        "datastore.read.average",  # Datastore read KB/s
        "datastore.write.average",  # Datastore write KB/s
        "datastore.numberReadAveraged.average",  # Datastore read IOPS
        "datastore.numberWriteAveraged.average",  # Datastore write IOPS
        "datastore.totalReadLatency.average",  # Datastore read latency
        "datastore.totalWriteLatency.average",  # Datastore write latency
    ],
}

# Interval mapping to vSphere interval IDs
# vSphere uses specific interval IDs for historical data
INTERVAL_CONFIG: dict[str, IntervalConfig] = {
    "realtime": {
        "interval_id": 20,  # 20-second samples
        "max_samples": 180,  # ~1 hour of data
        "description": "Real-time (20-second samples, last ~1 hour)",
    },
    "5min": {
        "interval_id": 300,  # 5-minute rollups
        "max_samples": 288,  # 24 hours of data
        "description": "5-minute rollups (last 24 hours)",
    },
    "1hour": {
        "interval_id": 7200,  # 2-hour rollups (closest to 1hr)
        "max_samples": 84,  # 7 days of data
        "description": "Hourly rollups (last 7 days)",
    },
    "6hour": {
        "interval_id": 7200,
        "max_samples": 28,  # ~7 days, fewer samples
        "description": "6-hour aggregates",
    },
    "12hour": {
        "interval_id": 7200,
        "max_samples": 14,
        "description": "12-hour aggregates",
    },
    "24hour": {
        "interval_id": 86400,  # Daily rollups
        "max_samples": 30,  # 30 days
        "description": "Daily rollups (last 30 days)",
    },
    "7day": {
        "interval_id": 86400,
        "max_samples": 7,  # 7 days
        "description": "Weekly summary",
    },
}


class PerformanceHandlerMixin:
    """
    Mixin for PerformanceManager-based metrics.

    Provides detailed disk I/O, network throughput, and historical
    metrics that quickStats doesn't offer.
    """

    # These will be provided by VMwareConnector (base class)
    _content: Any

    # Helper methods (will be provided by VMwareConnector)
    def _find_vm(self, name: str) -> Any | None:
        return None

    def _find_host(self, name: str) -> Any | None:
        return None

    def _find_cluster(self, name: str) -> Any | None:
        return None

    def _find_datastore(self, name: str) -> Any | None:
        return None

    # =========================================================================
    # COUNTER DISCOVERY & CACHING
    # =========================================================================

    def _get_perf_manager(self) -> Any:
        """Get the PerformanceManager instance."""
        return self._content.perfManager

    def _check_perf_provider_summary(self, entity: Any) -> dict[str, Any]:
        """
        Check what performance intervals are supported for an entity.

        Uses QueryPerfProviderSummary to determine:
        - currentSupported: Whether real-time stats are available
        - summarySupported: Whether historical stats are available
        - refreshRate: The refresh rate for real-time stats (if supported)

        This is important because datastores typically don't support real-time
        metrics - they only have historical (rollup) data available.
        """
        perf_manager = self._get_perf_manager()
        try:
            summary = perf_manager.QueryPerfProviderSummary(entity=entity)
            return {
                "currentSupported": summary.currentSupported,  # Real-time
                "summarySupported": summary.summarySupported,  # Historical
                "refreshRate": summary.refreshRate if summary.currentSupported else None,
            }
        except Exception as e:
            logger.warning(f"QueryPerfProviderSummary failed: {e}")
            return {
                "currentSupported": False,
                "summarySupported": True,
                "refreshRate": None,
            }

    def _get_historical_intervals(self) -> list[dict[str, Any]]:
        """
        Get the configured historical intervals from the PerformanceManager.

        vSphere has configurable historical intervals - we need to query
        them dynamically rather than assuming hardcoded values.

        Returns list of intervals with:
        - key: The interval ID to use in QueryPerf
        - samplingPeriod: Seconds between samples
        - name: Human-readable name
        - length: How long data is kept (in seconds)
        """
        perf_manager = self._get_perf_manager()
        intervals = []

        try:
            for interval in perf_manager.historicalInterval:
                intervals.append(
                    {
                        "key": interval.key,  # This is the ID to use!
                        "samplingPeriod": interval.samplingPeriod,
                        "name": interval.name,
                        "length": interval.length,
                    }
                )
        except Exception as e:
            logger.warning(f"Failed to get historical intervals: {e}")

        return intervals

    def _get_best_interval_for_entity(
        self,
        entity: Any,
        requested_interval: str,
    ) -> tuple[str, IntervalConfig, str | None]:
        """
        Get the best available interval for an entity.

        If the requested interval isn't supported, falls back to the next
        best available option.

        Returns:
            tuple of (interval_name, interval_config, note_if_fallback)
        """
        provider_summary = self._check_perf_provider_summary(entity)

        # Check if this entity supports any performance metrics at all
        if not provider_summary["currentSupported"] and not provider_summary["summarySupported"]:
            logger.warning("Entity does not support any performance metrics")
            return (
                requested_interval,
                INTERVAL_CONFIG.get(requested_interval, INTERVAL_CONFIG["5min"]),
                "This entity does not support performance metrics collection.",
            )

        # If only historical is supported (typical for datastores)
        if not provider_summary["currentSupported"] and provider_summary["summarySupported"]:
            # Get actual historical intervals from vSphere
            historical_intervals = self._get_historical_intervals()

            if historical_intervals:
                # Use the first (shortest) historical interval
                shortest = historical_intervals[0]
                logger.info(
                    f"Real-time not supported for entity. "
                    f"Using historical interval: {shortest['name']} "
                    f"(key={shortest['key']}, sampling={shortest['samplingPeriod']}s)"
                )

                # Create a dynamic interval config based on actual vSphere config
                dynamic_config: IntervalConfig = {
                    "interval_id": shortest["key"],  # Use the KEY, not samplingPeriod!
                    "max_samples": 100,  # Reasonable default
                    "description": f"{shortest['name']} ({shortest['samplingPeriod']}s samples)",
                }

                note = (
                    f"Real-time metrics not available for this entity. "
                    f"Using historical interval: {shortest['name']}."
                )
                return "historical", dynamic_config, note
            else:
                # Fallback to static config if we can't get intervals
                logger.warning("Could not get historical intervals, using static config")
                return (
                    "5min",
                    INTERVAL_CONFIG["5min"],
                    "Real-time metrics not available. Using 5-minute historical data.",
                )

        # Real-time is supported
        if requested_interval == "realtime":
            return requested_interval, INTERVAL_CONFIG[requested_interval], None

        return requested_interval, INTERVAL_CONFIG[requested_interval], None

    def _build_counter_map(self) -> dict[str, int]:
        """
        Build a mapping of counter names to counter IDs.

        Counter names are in format: group.name.rollup
        e.g., "cpu.usage.average", "disk.read.average"
        """
        perf_manager = self._get_perf_manager()
        counter_map = {}

        for counter in perf_manager.perfCounter:
            # Build full counter name: group.name.rollup
            full_name = f"{counter.groupInfo.key}.{counter.nameInfo.key}.{counter.rollupType}"
            counter_map[full_name] = counter.key

        return counter_map

    def _get_counter_ids(self, metric_names: list[str]) -> dict[str, int]:
        """
        Get counter IDs for the requested metric names.
        Returns mapping of metric_name -> counter_id.
        """
        counter_map = self._build_counter_map()
        result = {}

        for name in metric_names:
            if name in counter_map:
                result[name] = counter_map[name]
            else:
                logger.warning(f"Counter not found: {name}")

        return result

    # =========================================================================
    # QUERY BUILDING
    # =========================================================================

    def _build_query_spec(
        self,
        entity: Any,
        counter_ids: list[int],
        interval_id: int,
        max_samples: int,
    ) -> Any:
        """Build a PerformanceManager QuerySpec."""
        from pyVmomi import vim

        metric_ids = [
            vim.PerformanceManager.MetricId(counterId=cid, instance="*") for cid in counter_ids
        ]

        query_spec = vim.PerformanceManager.QuerySpec(
            entity=entity,
            metricId=metric_ids,
            intervalId=interval_id,
            maxSample=max_samples,
        )

        return query_spec

    # =========================================================================
    # RESULT PARSING
    # =========================================================================

    def _parse_perf_results(  # NOSONAR (cognitive complexity)
        self,
        perf_data: list[Any],
        counter_map: dict[int, str],
    ) -> dict[str, Any]:
        """
        Parse PerformanceManager query results into structured data.

        Returns a dict with:
        - metrics: Dict of metric_name -> {values: [...], unit: str, avg: float, max: float, min: float}
        - timestamps: List of sample timestamps
        - sample_count: Number of samples
        """
        if not perf_data:
            return {"metrics": {}, "timestamps": [], "sample_count": 0}

        result: dict[str, Any] = {
            "metrics": {},
            "timestamps": [],
            "sample_count": 0,
        }

        # Process first entity's data (we query one entity at a time)
        entity_data = perf_data[0]

        # Extract timestamps
        if hasattr(entity_data, "sampleInfo") and entity_data.sampleInfo:
            result["timestamps"] = [
                info.timestamp.isoformat()
                if hasattr(info.timestamp, "isoformat")
                else str(info.timestamp)
                for info in entity_data.sampleInfo
            ]
            result["sample_count"] = len(entity_data.sampleInfo)

        # Process each metric's values
        if hasattr(entity_data, "value") and entity_data.value:
            for metric_value in entity_data.value:
                counter_id = metric_value.id.counterId
                metric_name = counter_map.get(counter_id, f"unknown_{counter_id}")
                instance = metric_value.id.instance or "aggregate"

                values = list(metric_value.value) if metric_value.value else []

                # Filter out -1 values (indicates no data)
                valid_values = [v for v in values if v >= 0]

                key = f"{metric_name}" if instance == "" else f"{metric_name}[{instance}]"

                result["metrics"][key] = {
                    "values": values,
                    "valid_values": valid_values,
                    "avg": sum(valid_values) / len(valid_values) if valid_values else None,
                    "max": max(valid_values) if valid_values else None,
                    "min": min(valid_values) if valid_values else None,
                    "latest": valid_values[-1] if valid_values else None,
                }

        return result

    def _summarize_metrics(self, parsed_data: dict[str, Any]) -> dict[str, Any]:
        """
        Create a diagnostic-friendly summary of metrics.

        Converts raw values to human-readable format with units.
        """
        metrics = parsed_data.get("metrics", {})
        summary: dict[str, dict[str, Any]] = {
            "cpu": {},
            "memory": {},
            "disk": {},
            "network": {},
            "datastore": {},
        }

        for metric_name, data in metrics.items():
            # Remove instance suffix for grouping
            base_name = metric_name.split("[")[0]

            # Determine category
            if base_name.startswith("cpu."):
                category = "cpu"
            elif base_name.startswith("mem."):
                category = "memory"
            elif base_name.startswith("disk."):
                category = "disk"
            elif base_name.startswith("net."):
                category = "network"
            elif base_name.startswith("datastore."):
                category = "datastore"
            else:
                continue

            # Simplify metric name for readability
            base_name.split(".")[-2] if "." in base_name else base_name

            summary[category][metric_name] = {
                "avg": data.get("avg"),
                "max": data.get("max"),
                "min": data.get("min"),
                "latest": data.get("latest"),
            }

        return summary

    # =========================================================================
    # MAIN QUERY METHODS
    # =========================================================================

    async def _get_detailed_vm_performance(self, params: dict[str, Any]) -> dict:
        """
        Get detailed VM performance metrics including disk I/O and network.

        Parameters:
            vm_name: Name of the VM
            interval: Time interval (realtime, 5min, 1hour, 6hour, 12hour, 24hour, 7day)
            metrics: Optional list of metric groups to include (cpu, memory, disk, network)
                     Defaults to all groups.

        Returns comprehensive performance data that quickStats doesn't provide:
        - Disk read/write rates (KB/s)
        - Disk IOPS
        - Disk latency (ms)
        - Network receive/transmit rates (KB/s)
        - Network packet counts
        - Dropped packets (network health indicator)
        - CPU ready time (contention indicator)
        - Memory balloon/swap activity
        """

        vm_name = params.get("vm_name")
        if not vm_name:
            raise ValueError("vm_name is required")

        vm = self._find_vm(vm_name)
        if not vm:
            raise ValueError(f"VM not found: {vm_name}")

        interval = params.get("interval", "realtime")
        if interval not in INTERVAL_CONFIG:
            raise ValueError(f"Invalid interval: {interval}. Valid: {list(INTERVAL_CONFIG.keys())}")

        interval_cfg = INTERVAL_CONFIG[interval]

        # Determine which metric groups to query
        requested_groups = params.get("metrics", ["cpu", "memory", "disk", "network"])
        if isinstance(requested_groups, str):
            requested_groups = [requested_groups]

        # Collect metric names
        metric_names = []
        for group in requested_groups:
            if group in METRIC_GROUPS:
                metric_names.extend(METRIC_GROUPS[group])

        if not metric_names:
            raise ValueError(f"No valid metric groups. Valid: {list(METRIC_GROUPS.keys())}")

        # Get counter IDs
        counter_ids = self._get_counter_ids(metric_names)
        if not counter_ids:
            return {
                "vm_name": vm_name,
                "interval": interval,
                "interval_description": interval_cfg["description"],
                "error": "No performance counters available. PerformanceManager may not be enabled.",
                "quick_stats": self._get_quick_stats_fallback(vm),
            }

        # Build reverse map for parsing
        reverse_counter_map = {v: k for k, v in counter_ids.items()}

        # Build and execute query
        perf_manager = self._get_perf_manager()
        query_spec = self._build_query_spec(
            entity=vm,
            counter_ids=list(counter_ids.values()),
            interval_id=interval_cfg["interval_id"],
            max_samples=interval_cfg["max_samples"],
        )

        try:
            perf_data = perf_manager.QueryPerf(querySpec=[query_spec])
        except Exception as e:
            logger.warning(f"PerformanceManager query failed: {e}")
            return {
                "vm_name": vm_name,
                "interval": interval,
                "error": f"PerformanceManager query failed: {e!s}",
                "quick_stats": self._get_quick_stats_fallback(vm),
            }

        # Parse results
        parsed = self._parse_perf_results(perf_data, reverse_counter_map)
        summary = self._summarize_metrics(parsed)
        highlights = self._generate_diagnostic_highlights(summary, vm)

        # Build response - PUT MOST IMPORTANT INFO FIRST for LLM
        result: dict[str, Any] = {
            "vm_name": vm_name,
            "power_state": str(vm.runtime.powerState),
            # DIAGNOSTIC SUMMARY FIRST - easy for LLM to show user
            "diagnostic_highlights": highlights,
            "key_metrics_summary": self._build_key_metrics_summary(summary),
            # Then metadata
            "interval": interval,
            "interval_description": interval_cfg["description"],
            "sample_count": parsed["sample_count"],
            "time_range": {
                "start": parsed["timestamps"][0] if parsed["timestamps"] else None,
                "end": parsed["timestamps"][-1] if parsed["timestamps"] else None,
            },
            # Full performance data last (may be truncated)
            "performance": summary,
            "quick_stats": self._get_quick_stats_fallback(vm),
        }

        return result

    async def _get_detailed_host_performance(self, params: dict[str, Any]) -> dict:
        """
        Get detailed host performance metrics including disk I/O and network.

        Similar to VM metrics but at the host level - useful for identifying
        infrastructure-level bottlenecks.
        """

        host_name = params.get("host_name")
        if not host_name:
            raise ValueError("host_name is required")

        host = self._find_host(host_name)
        if not host:
            raise ValueError(f"Host not found: {host_name}")

        interval = params.get("interval", "realtime")
        if interval not in INTERVAL_CONFIG:
            raise ValueError(f"Invalid interval: {interval}. Valid: {list(INTERVAL_CONFIG.keys())}")

        interval_cfg = INTERVAL_CONFIG[interval]

        # Host metrics - include all groups
        requested_groups = params.get("metrics", ["cpu", "memory", "disk", "network"])
        if isinstance(requested_groups, str):
            requested_groups = [requested_groups]

        metric_names = []
        for group in requested_groups:
            if group in METRIC_GROUPS:
                metric_names.extend(METRIC_GROUPS[group])

        counter_ids = self._get_counter_ids(metric_names)
        if not counter_ids:
            return {
                "host_name": host_name,
                "interval": interval,
                "error": "No performance counters available",
                "quick_stats": self._get_host_quick_stats_fallback(host),
            }

        reverse_counter_map = {v: k for k, v in counter_ids.items()}

        perf_manager = self._get_perf_manager()
        query_spec = self._build_query_spec(
            entity=host,
            counter_ids=list(counter_ids.values()),
            interval_id=interval_cfg["interval_id"],
            max_samples=interval_cfg["max_samples"],
        )

        try:
            perf_data = perf_manager.QueryPerf(querySpec=[query_spec])
        except Exception as e:
            logger.warning(f"PerformanceManager query failed: {e}")
            return {
                "host_name": host_name,
                "interval": interval,
                "error": f"PerformanceManager query failed: {e!s}",
                "quick_stats": self._get_host_quick_stats_fallback(host),
            }

        parsed = self._parse_perf_results(perf_data, reverse_counter_map)
        summary = self._summarize_metrics(parsed)

        highlights = self._generate_host_diagnostic_highlights(summary, host)

        return {
            "host_name": host_name,
            "connection_state": str(host.runtime.connectionState),
            "power_state": str(host.runtime.powerState),
            # DIAGNOSTIC SUMMARY FIRST - easy for LLM to show user
            "diagnostic_highlights": highlights,
            "key_metrics_summary": self._build_key_metrics_summary(summary),
            # Then metadata
            "interval": interval,
            "interval_description": interval_cfg["description"],
            "sample_count": parsed["sample_count"],
            "time_range": {
                "start": parsed["timestamps"][0] if parsed["timestamps"] else None,
                "end": parsed["timestamps"][-1] if parsed["timestamps"] else None,
            },
            # Full performance data last (may be truncated)
            "performance": summary,
            "quick_stats": self._get_host_quick_stats_fallback(host),
        }

    # NOTE: get_detailed_datastore_performance was REMOVED because:
    # Datastores are NOT valid performance providers in vSphere's PerformanceManager API.
    # IOPS/latency metrics CANNOT be queried directly from datastore objects.
    #
    # Use get_datastore_performance (in storage_handlers.py) for capacity info.
    #
    # For datastore IOPS/latency: call get_detailed_host_performance with metrics=["datastore"]
    # on a host that accesses the datastore. This returns read/write IOPS, latency, throughput.

    async def _get_cluster_detailed_performance(self, params: dict[str, Any]) -> dict:
        """
        Get aggregated performance metrics for all hosts in a cluster.

        Useful for identifying cluster-wide resource issues.
        """

        cluster_name = params.get("cluster_name")
        if not cluster_name:
            raise ValueError("cluster_name is required")

        cluster = self._find_cluster(cluster_name)
        if not cluster:
            raise ValueError(f"Cluster not found: {cluster_name}")

        interval = params.get("interval", "realtime")

        # Get metrics for each host in the cluster
        host_metrics = []
        for host in cluster.host:
            try:
                host_data = await self._get_detailed_host_performance(
                    {
                        "host_name": host.name,
                        "interval": interval,
                    }
                )
                host_metrics.append(host_data)
            except Exception as e:
                logger.warning(f"Failed to get metrics for host {host.name}: {e}")
                host_metrics.append(
                    {
                        "host_name": host.name,
                        "error": str(e),
                    }
                )

        # Aggregate metrics across hosts
        aggregated = self._aggregate_host_metrics(host_metrics)

        return {
            "cluster_name": cluster_name,
            "interval": interval,
            "num_hosts": len(cluster.host),
            "hosts_queried": len(host_metrics),
            "aggregated_performance": aggregated,
            "per_host_metrics": host_metrics,
            "cluster_summary": {
                "total_cpu_mhz": cluster.summary.totalCpu,
                "total_memory_mb": cluster.summary.totalMemory // (1024 * 1024),
                "num_effective_hosts": cluster.summary.numEffectiveHosts,
            },
        }

    # =========================================================================
    # DIAGNOSTIC HELPERS
    # =========================================================================

    def _get_quick_stats_fallback(self, vm: Any) -> dict[str, Any]:
        """Get quickStats as fallback when PerformanceManager fails."""
        qs = vm.summary.quickStats
        if not qs:
            return {}

        return {
            "cpu_usage_mhz": qs.overallCpuUsage,
            "memory_usage_mb": qs.guestMemoryUsage,
            "active_memory_mb": getattr(qs, "activeMemory", None),
            "consumed_overhead_mb": getattr(qs, "consumedOverheadMemory", None),
            "ballooned_memory_mb": getattr(qs, "balloonedMemory", None),
            "swapped_memory_mb": getattr(qs, "swappedMemory", None),
            "uptime_seconds": qs.uptimeSeconds,
        }

    def _get_host_quick_stats_fallback(self, host: Any) -> dict[str, Any]:
        """Get host quickStats as fallback."""
        qs = host.summary.quickStats
        hw = host.summary.hardware

        return {
            "cpu_usage_mhz": qs.overallCpuUsage if qs else None,
            "memory_usage_mb": qs.overallMemoryUsage if qs else None,
            "total_cpu_mhz": hw.cpuMhz * hw.numCpuCores if hw else None,
            "total_memory_mb": hw.memorySize // (1024 * 1024) if hw else None,
        }

    def _generate_diagnostic_highlights(  # NOSONAR (cognitive complexity)
        self,
        summary: dict[str, Any],
        vm: Any,
    ) -> dict[str, Any]:
        """
        Generate diagnostic highlights - key issues the LLM should pay attention to.

        These are pre-analyzed indicators that help the LLM understand what's
        important without needing to interpret raw numbers.
        """
        highlights: dict[str, list[str]] = {
            "issues": [],
            "warnings": [],
            "healthy": [],
        }

        # CPU analysis
        cpu_metrics = summary.get("cpu", {})
        for metric_name, data in cpu_metrics.items():
            if "ready" in metric_name and data.get("avg"):
                # CPU ready > 5% indicates contention
                ready_pct = data["avg"] / 20000 * 100  # Convert to percentage
                if ready_pct > 10:
                    highlights["issues"].append(
                        f"High CPU ready time ({ready_pct:.1f}%) - VM is waiting for CPU resources"
                    )
                elif ready_pct > 5:
                    highlights["warnings"].append(
                        f"Elevated CPU ready time ({ready_pct:.1f}%) - possible CPU contention"
                    )

        # Memory analysis
        mem_metrics = summary.get("memory", {})
        for metric_name, data in mem_metrics.items():
            if "swapped" in metric_name and data.get("avg") and data["avg"] > 0:
                highlights["issues"].append(
                    f"Memory swapping detected ({data['avg']:.0f} KB) - host memory pressure"
                )
            if "vmmemctl" in metric_name and data.get("avg") and data["avg"] > 0:
                highlights["warnings"].append(
                    f"Balloon driver active ({data['avg']:.0f} KB) - host reclaiming memory"
                )

        # Disk analysis
        disk_metrics = summary.get("disk", {})
        for metric_name, data in disk_metrics.items():
            if "Latency" in metric_name and data.get("avg"):
                latency = data["avg"]
                if latency > 50:
                    highlights["issues"].append(
                        f"High disk latency ({latency:.1f}ms) - storage performance issue"
                    )
                elif latency > 20:
                    highlights["warnings"].append(f"Elevated disk latency ({latency:.1f}ms)")

        # Network analysis
        net_metrics = summary.get("network", {})
        for metric_name, data in net_metrics.items():
            if "dropped" in metric_name and data.get("avg") and data["avg"] > 0:
                highlights["warnings"].append(
                    "Network packet drops detected - possible network congestion"
                )

        # Add healthy indicators if no issues
        if not highlights["issues"] and not highlights["warnings"]:
            highlights["healthy"].append("No performance issues detected")

        return highlights

    def _build_key_metrics_summary(
        self, summary: dict[str, Any]
    ) -> dict[str, Any]:  # NOSONAR (cognitive complexity)
        """
        Build a compact summary of key metrics with actual values.

        This is designed to be easy for the LLM to show directly to users,
        with human-readable values and units.
        """
        key_metrics: dict[str, Any] = {
            "cpu": {},
            "memory": {},
            "disk": {},
            "network": {},
        }

        # CPU metrics
        cpu = summary.get("cpu", {})
        if "cpu.usage.average" in cpu:
            key_metrics["cpu"]["usage_percent"] = f"{cpu['cpu.usage.average'].get('avg', 0):.1f}%"
        if "cpu.usagemhz.average" in cpu:
            key_metrics["cpu"]["usage_mhz"] = f"{cpu['cpu.usagemhz.average'].get('avg', 0):.0f} MHz"
        if "cpu.ready.summation" in cpu:
            ready_ms = cpu["cpu.ready.summation"].get("avg", 0)
            ready_pct = ready_ms / 20000 * 100
            key_metrics["cpu"]["ready_time"] = f"{ready_pct:.1f}% ({ready_ms:.0f}ms)"
        if "cpu.wait.summation" in cpu:
            key_metrics["cpu"]["wait_time"] = f"{cpu['cpu.wait.summation'].get('avg', 0):.0f}ms"

        # Memory metrics
        mem = summary.get("memory", {})
        if "mem.usage.average" in mem:
            key_metrics["memory"]["usage_percent"] = (
                f"{mem['mem.usage.average'].get('avg', 0):.1f}%"
            )
        if "mem.active.average" in mem:
            active_mb = mem["mem.active.average"].get("avg", 0) / 1024
            key_metrics["memory"]["active"] = f"{active_mb:.0f} MB"
        if "mem.swapped.average" in mem:
            swapped_kb = mem["mem.swapped.average"].get("avg", 0)
            key_metrics["memory"]["swapped"] = (
                f"{swapped_kb:.0f} KB" if swapped_kb > 0 else "0 (none)"
            )
        if "mem.vmmemctl.average" in mem:
            balloon_kb = mem["mem.vmmemctl.average"].get("avg", 0)
            key_metrics["memory"]["balloon"] = (
                f"{balloon_kb:.0f} KB" if balloon_kb > 0 else "0 (none)"
            )

        # Disk metrics
        disk = summary.get("disk", {})
        if "disk.read.average" in disk:
            key_metrics["disk"]["read_rate"] = f"{disk['disk.read.average'].get('avg', 0):.0f} KB/s"
        if "disk.write.average" in disk:
            key_metrics["disk"]["write_rate"] = (
                f"{disk['disk.write.average'].get('avg', 0):.0f} KB/s"
            )
        if "disk.numberRead.summation" in disk:
            key_metrics["disk"]["read_iops"] = (
                f"{disk['disk.numberRead.summation'].get('avg', 0):.0f}"
            )
        if "disk.numberWrite.summation" in disk:
            key_metrics["disk"]["write_iops"] = (
                f"{disk['disk.numberWrite.summation'].get('avg', 0):.0f}"
            )
        if "disk.maxTotalLatency.latest" in disk:
            key_metrics["disk"]["latency"] = (
                f"{disk['disk.maxTotalLatency.latest'].get('avg', 0):.1f}ms"
            )

        # Network metrics
        net = summary.get("network", {})
        if "net.bytesRx.average" in net:
            rx_kbps = net["net.bytesRx.average"].get("avg", 0)
            key_metrics["network"]["receive_rate"] = f"{rx_kbps:.0f} KB/s"
        if "net.bytesTx.average" in net:
            tx_kbps = net["net.bytesTx.average"].get("avg", 0)
            key_metrics["network"]["transmit_rate"] = f"{tx_kbps:.0f} KB/s"
        if "net.packetsRx.summation" in net:
            key_metrics["network"]["packets_received"] = (
                f"{net['net.packetsRx.summation'].get('avg', 0):.0f}/interval"
            )
        if "net.packetsTx.summation" in net:
            key_metrics["network"]["packets_sent"] = (
                f"{net['net.packetsTx.summation'].get('avg', 0):.0f}/interval"
            )
        if "net.droppedRx.summation" in net:
            dropped = net["net.droppedRx.summation"].get("avg", 0)
            key_metrics["network"]["dropped_rx"] = f"{dropped:.0f}" if dropped > 0 else "0 (none)"
        if "net.droppedTx.summation" in net:
            dropped = net["net.droppedTx.summation"].get("avg", 0)
            key_metrics["network"]["dropped_tx"] = f"{dropped:.0f}" if dropped > 0 else "0 (none)"

        return key_metrics

    def _generate_host_diagnostic_highlights(  # NOSONAR (cognitive complexity)
        self,
        summary: dict[str, Any],
        host: Any,
    ) -> dict[str, list[str]]:
        """Generate diagnostic highlights for host metrics."""
        highlights: dict[str, list[str]] = {
            "issues": [],
            "warnings": [],
            "healthy": [],
        }

        # Similar analysis as VM but at host level
        cpu_metrics = summary.get("cpu", {})
        for metric_name, data in cpu_metrics.items():
            if "usage" in metric_name and data.get("avg"):
                # Check overall CPU usage
                if data["avg"] > 90:
                    highlights["issues"].append(
                        f"High CPU utilization ({data['avg']:.1f}%) - host may be overloaded"
                    )
                elif data["avg"] > 75:
                    highlights["warnings"].append(f"Elevated CPU utilization ({data['avg']:.1f}%)")

        mem_metrics = summary.get("memory", {})
        for metric_name, data in mem_metrics.items():
            if "usage" in metric_name and data.get("avg"):
                if data["avg"] > 95:
                    highlights["issues"].append(
                        f"Critical memory usage ({data['avg']:.1f}%) - VMs may be swapping"
                    )
                elif data["avg"] > 85:
                    highlights["warnings"].append(f"High memory usage ({data['avg']:.1f}%)")

        if not highlights["issues"] and not highlights["warnings"]:
            highlights["healthy"].append("Host performance is healthy")

        return highlights

    def _aggregate_host_metrics(
        self,
        host_metrics: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Aggregate metrics across multiple hosts."""
        aggregated: dict[str, Any] = {
            "cpu": {"total_usage_mhz": 0, "hosts_reporting": 0},
            "memory": {"total_usage_mb": 0, "hosts_reporting": 0},
            "issues_count": 0,
            "warnings_count": 0,
        }

        for host_data in host_metrics:
            if "error" in host_data:
                continue

            qs = host_data.get("quick_stats", {})
            if qs.get("cpu_usage_mhz"):
                aggregated["cpu"]["total_usage_mhz"] += qs["cpu_usage_mhz"]
                aggregated["cpu"]["hosts_reporting"] += 1

            if qs.get("memory_usage_mb"):
                aggregated["memory"]["total_usage_mb"] += qs["memory_usage_mb"]
                aggregated["memory"]["hosts_reporting"] += 1

            highlights = host_data.get("diagnostic_highlights", {})
            aggregated["issues_count"] += len(highlights.get("issues", []))
            aggregated["warnings_count"] += len(highlights.get("warnings", []))

        return aggregated

    # =========================================================================
    # AVAILABLE METRICS DISCOVERY
    # =========================================================================

    async def _list_available_metrics(
        self, params: dict[str, Any]
    ) -> dict:  # NOSONAR (cognitive complexity)
        """
        List all available performance metrics for an entity.

        Useful for discovering what metrics are available on a specific
        vCenter/entity combination.
        """
        entity_type = params.get("entity_type", "vm")
        entity_name = params.get("entity_name")

        # Find entity
        if entity_type == "vm":
            if not entity_name:
                raise ValueError("entity_name required for VM")
            entity = self._find_vm(entity_name)
            if not entity:
                raise ValueError(f"VM not found: {entity_name}")
        elif entity_type == "host":
            if not entity_name:
                raise ValueError("entity_name required for host")
            entity = self._find_host(entity_name)
            if not entity:
                raise ValueError(f"Host not found: {entity_name}")
        elif entity_type == "datastore":
            if not entity_name:
                raise ValueError("entity_name required for datastore")
            entity = self._find_datastore(entity_name)
            if not entity:
                raise ValueError(f"Datastore not found: {entity_name}")
        else:
            raise ValueError(f"Invalid entity_type: {entity_type}. Valid: vm, host, datastore")

        perf_manager = self._get_perf_manager()

        # Get available metrics for this entity
        available = perf_manager.QueryAvailablePerfMetric(entity=entity)

        # Build counter map for names
        counter_map = self._build_counter_map()
        reverse_map = {v: k for k, v in counter_map.items()}

        # Categorize available metrics
        metrics_by_group: dict[str, list[str]] = {
            "cpu": [],
            "memory": [],
            "disk": [],
            "network": [],
            "datastore": [],
            "other": [],
        }

        for metric in available:
            counter_id = metric.counterId
            metric_name = reverse_map.get(counter_id, f"unknown_{counter_id}")

            # Categorize
            if metric_name.startswith("cpu."):
                metrics_by_group["cpu"].append(metric_name)
            elif metric_name.startswith("mem."):
                metrics_by_group["memory"].append(metric_name)
            elif metric_name.startswith("disk."):
                metrics_by_group["disk"].append(metric_name)
            elif metric_name.startswith("net."):
                metrics_by_group["network"].append(metric_name)
            elif metric_name.startswith("datastore."):
                metrics_by_group["datastore"].append(metric_name)
            else:
                metrics_by_group["other"].append(metric_name)

        return {
            "entity_type": entity_type,
            "entity_name": entity_name,
            "total_metrics_available": len(available),
            "metrics_by_group": metrics_by_group,
            "supported_intervals": list(INTERVAL_CONFIG.keys()),
        }
