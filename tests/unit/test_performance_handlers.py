# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Unit tests for PerformanceManager handlers.

Tests the detailed performance metrics operations that provide
disk I/O, network throughput, and historical metrics.
"""

from datetime import datetime
from unittest.mock import Mock

import pytest

from meho_app.modules.connectors.vmware.handlers.performance_handlers import (
    INTERVAL_CONFIG,
    METRIC_GROUPS,
    PerformanceHandlerMixin,
)


class TestMetricConfiguration:
    """Test metric group and interval configuration."""

    def test_metric_groups_defined(self):
        """All expected metric groups should be defined."""
        assert "cpu" in METRIC_GROUPS
        assert "memory" in METRIC_GROUPS
        assert "disk" in METRIC_GROUPS
        assert "network" in METRIC_GROUPS
        assert "datastore" in METRIC_GROUPS

    def test_cpu_metrics_include_ready_time(self):
        """CPU metrics should include ready time for contention detection."""
        cpu_metrics = METRIC_GROUPS["cpu"]
        assert any("ready" in m for m in cpu_metrics)

    def test_disk_metrics_include_latency(self):
        """Disk metrics should include latency for performance diagnosis."""
        disk_metrics = METRIC_GROUPS["disk"]
        assert any("Latency" in m for m in disk_metrics)
        assert any("read" in m.lower() for m in disk_metrics)
        assert any("write" in m.lower() for m in disk_metrics)

    def test_network_metrics_include_dropped_packets(self):
        """Network metrics should include dropped packets for health diagnosis."""
        net_metrics = METRIC_GROUPS["network"]
        assert any("dropped" in m.lower() for m in net_metrics)

    def test_interval_config_has_all_intervals(self):
        """All expected intervals should be configured."""
        expected = ["realtime", "5min", "1hour", "6hour", "12hour", "24hour", "7day"]
        for interval in expected:
            assert interval in INTERVAL_CONFIG

    def test_realtime_interval_uses_20_second_samples(self):
        """Realtime interval should use 20-second samples."""
        assert INTERVAL_CONFIG["realtime"]["interval_id"] == 20

    def test_intervals_have_required_fields(self):
        """Each interval config should have required fields."""
        for name, config in INTERVAL_CONFIG.items():
            assert "interval_id" in config, f"{name} missing interval_id"
            assert "max_samples" in config, f"{name} missing max_samples"
            assert "description" in config, f"{name} missing description"


class MockPerformanceHandlerMixin(PerformanceHandlerMixin):
    """Test implementation of PerformanceHandlerMixin."""

    def __init__(self):
        self._content = Mock()
        self._mock_vm = None
        self._mock_host = None
        self._mock_cluster = None
        self._mock_datastore = None

    def _find_vm(self, name: str):
        return self._mock_vm

    def _find_host(self, name: str):
        return self._mock_host

    def _find_cluster(self, name: str):
        return self._mock_cluster

    def _find_datastore(self, name: str):
        return self._mock_datastore


class TestCounterDiscovery:
    """Test counter ID discovery and mapping."""

    def test_build_counter_map(self):
        """Should build mapping of counter names to IDs."""
        handler = MockPerformanceHandlerMixin()

        # Mock perfManager with counters
        mock_counter1 = Mock()
        mock_counter1.groupInfo.key = "cpu"
        mock_counter1.nameInfo.key = "usage"
        mock_counter1.rollupType = "average"
        mock_counter1.key = 1

        mock_counter2 = Mock()
        mock_counter2.groupInfo.key = "disk"
        mock_counter2.nameInfo.key = "read"
        mock_counter2.rollupType = "average"
        mock_counter2.key = 2

        handler._content.perfManager.perfCounter = [mock_counter1, mock_counter2]

        counter_map = handler._build_counter_map()

        assert "cpu.usage.average" in counter_map
        assert counter_map["cpu.usage.average"] == 1
        assert "disk.read.average" in counter_map
        assert counter_map["disk.read.average"] == 2

    def test_get_counter_ids_filters_valid(self):
        """Should only return IDs for counters that exist."""
        handler = MockPerformanceHandlerMixin()

        mock_counter = Mock()
        mock_counter.groupInfo.key = "cpu"
        mock_counter.nameInfo.key = "usage"
        mock_counter.rollupType = "average"
        mock_counter.key = 1

        handler._content.perfManager.perfCounter = [mock_counter]

        result = handler._get_counter_ids(["cpu.usage.average", "nonexistent.metric"])

        assert "cpu.usage.average" in result
        assert "nonexistent.metric" not in result


class TestResultParsing:
    """Test performance result parsing."""

    def test_parse_empty_results(self):
        """Should handle empty results gracefully."""
        handler = MockPerformanceHandlerMixin()

        result = handler._parse_perf_results([], {})

        assert result["metrics"] == {}
        assert result["timestamps"] == []
        assert result["sample_count"] == 0

    def test_parse_results_extracts_values(self):
        """Should extract metric values from query results."""
        handler = MockPerformanceHandlerMixin()

        # Mock performance data structure
        mock_metric_value = Mock()
        mock_metric_value.id.counterId = 1
        mock_metric_value.id.instance = ""  # Empty instance = aggregate
        mock_metric_value.value = [100, 150, 200]

        mock_sample_info = Mock()
        mock_sample_info.timestamp = datetime(2024, 1, 1, 12, 0, 0)  # noqa: DTZ001 -- naive datetime for test compatibility

        mock_entity_data = Mock()
        mock_entity_data.sampleInfo = [mock_sample_info]
        mock_entity_data.value = [mock_metric_value]

        counter_map = {1: "cpu.usage.average"}

        result = handler._parse_perf_results([mock_entity_data], counter_map)

        # Empty instance becomes [aggregate] suffix
        metric_key = "cpu.usage.average[aggregate]"
        assert metric_key in result["metrics"]
        assert result["metrics"][metric_key]["values"] == [100, 150, 200]
        assert result["metrics"][metric_key]["avg"] == 150
        assert result["metrics"][metric_key]["max"] == 200
        assert result["metrics"][metric_key]["min"] == 100

    def test_parse_results_filters_negative_values(self):
        """Should filter -1 values (no data indicator)."""
        handler = MockPerformanceHandlerMixin()

        mock_metric_value = Mock()
        mock_metric_value.id.counterId = 1
        mock_metric_value.id.instance = ""  # Empty instance = aggregate
        mock_metric_value.value = [100, -1, 200]  # -1 indicates no data

        mock_entity_data = Mock()
        mock_entity_data.sampleInfo = []
        mock_entity_data.value = [mock_metric_value]

        counter_map = {1: "cpu.usage.average"}

        result = handler._parse_perf_results([mock_entity_data], counter_map)

        # Empty instance becomes [aggregate] suffix
        metric_key = "cpu.usage.average[aggregate]"
        # valid_values should exclude -1
        assert result["metrics"][metric_key]["valid_values"] == [100, 200]
        assert result["metrics"][metric_key]["avg"] == 150  # avg of valid only


class TestDiagnosticHighlights:
    """Test diagnostic highlight generation."""

    def test_high_cpu_ready_generates_issue(self):
        """High CPU ready time should generate an issue."""
        handler = MockPerformanceHandlerMixin()

        # CPU ready > 10% should be an issue
        # Ready time is in summation units (20ms samples)
        # 10% = 2000 out of 20000 summation units
        summary = {
            "cpu": {"cpu.ready.summation": {"avg": 2500}},  # ~12.5%
            "memory": {},
            "disk": {},
            "network": {},
            "datastore": {},
        }

        mock_vm = Mock()
        highlights = handler._generate_diagnostic_highlights(summary, mock_vm)

        assert len(highlights["issues"]) > 0
        assert "CPU ready" in highlights["issues"][0]

    def test_memory_swapping_generates_issue(self):
        """Memory swapping should generate an issue."""
        handler = MockPerformanceHandlerMixin()

        summary = {
            "cpu": {},
            "memory": {"mem.swapped.average": {"avg": 1000}},
            "disk": {},
            "network": {},
            "datastore": {},
        }

        mock_vm = Mock()
        highlights = handler._generate_diagnostic_highlights(summary, mock_vm)

        assert len(highlights["issues"]) > 0
        assert "swap" in highlights["issues"][0].lower()

    def test_high_disk_latency_generates_issue(self):
        """High disk latency should generate an issue."""
        handler = MockPerformanceHandlerMixin()

        summary = {
            "cpu": {},
            "memory": {},
            "disk": {"disk.totalReadLatency.average": {"avg": 60}},  # 60ms
            "network": {},
            "datastore": {},
        }

        mock_vm = Mock()
        highlights = handler._generate_diagnostic_highlights(summary, mock_vm)

        assert len(highlights["issues"]) > 0
        assert "latency" in highlights["issues"][0].lower()

    def test_packet_drops_generates_warning(self):
        """Network packet drops should generate a warning."""
        handler = MockPerformanceHandlerMixin()

        summary = {
            "cpu": {},
            "memory": {},
            "disk": {},
            "network": {"net.droppedRx.summation": {"avg": 5}},
            "datastore": {},
        }

        mock_vm = Mock()
        highlights = handler._generate_diagnostic_highlights(summary, mock_vm)

        assert len(highlights["warnings"]) > 0
        assert "drop" in highlights["warnings"][0].lower()

    def test_healthy_system_generates_healthy_indicator(self):
        """System with no issues should show healthy."""
        handler = MockPerformanceHandlerMixin()

        summary = {
            "cpu": {},
            "memory": {},
            "disk": {},
            "network": {},
            "datastore": {},
        }

        mock_vm = Mock()
        highlights = handler._generate_diagnostic_highlights(summary, mock_vm)

        assert len(highlights["healthy"]) > 0
        assert "No performance issues" in highlights["healthy"][0]


class TestDetailedVMPerformance:
    """Test detailed VM performance operation."""

    @pytest.mark.asyncio
    async def test_requires_vm_name(self):
        """Should require vm_name parameter."""
        handler = MockPerformanceHandlerMixin()

        with pytest.raises(ValueError, match="vm_name is required"):
            await handler._get_detailed_vm_performance({})

    @pytest.mark.asyncio
    async def test_validates_interval(self):
        """Should validate interval parameter."""
        handler = MockPerformanceHandlerMixin()
        handler._mock_vm = Mock()

        with pytest.raises(ValueError, match="Invalid interval"):
            await handler._get_detailed_vm_performance(
                {"vm_name": "test-vm", "interval": "invalid"}
            )

    @pytest.mark.asyncio
    async def test_returns_fallback_on_no_counters(self):
        """Should return quickStats fallback if no counters available."""
        handler = MockPerformanceHandlerMixin()

        mock_vm = Mock()
        mock_vm.summary.quickStats.overallCpuUsage = 500
        mock_vm.summary.quickStats.guestMemoryUsage = 1024
        mock_vm.summary.quickStats.uptimeSeconds = 3600
        mock_vm.runtime.powerState = "poweredOn"
        handler._mock_vm = mock_vm

        # No counters available
        handler._content.perfManager.perfCounter = []

        result = await handler._get_detailed_vm_performance(
            {"vm_name": "test-vm", "interval": "realtime"}
        )

        assert "error" in result
        assert "quick_stats" in result

    @pytest.mark.asyncio
    async def test_vm_not_found_raises_error(self):
        """Should raise error if VM not found."""
        handler = MockPerformanceHandlerMixin()
        handler._mock_vm = None

        with pytest.raises(ValueError, match="VM not found"):
            await handler._get_detailed_vm_performance({"vm_name": "nonexistent"})


class TestDetailedHostPerformance:
    """Test detailed host performance operation."""

    @pytest.mark.asyncio
    async def test_requires_host_name(self):
        """Should require host_name parameter."""
        handler = MockPerformanceHandlerMixin()

        with pytest.raises(ValueError, match="host_name is required"):
            await handler._get_detailed_host_performance({})

    @pytest.mark.asyncio
    async def test_host_not_found_raises_error(self):
        """Should raise error if host not found."""
        handler = MockPerformanceHandlerMixin()
        handler._mock_host = None

        with pytest.raises(ValueError, match="Host not found"):
            await handler._get_detailed_host_performance({"host_name": "nonexistent"})


class TestListAvailableMetrics:
    """Test available metrics discovery."""

    @pytest.mark.asyncio
    async def test_requires_entity_type(self):
        """Should require entity_type parameter."""
        handler = MockPerformanceHandlerMixin()

        with pytest.raises(ValueError, match="Invalid entity_type"):
            await handler._list_available_metrics({"entity_type": "invalid", "entity_name": "test"})

    @pytest.mark.asyncio
    async def test_categorizes_metrics(self):
        """Should categorize metrics by group."""
        handler = MockPerformanceHandlerMixin()

        mock_vm = Mock()
        handler._mock_vm = mock_vm

        # Mock available metrics
        mock_metric1 = Mock()
        mock_metric1.counterId = 1

        mock_metric2 = Mock()
        mock_metric2.counterId = 2

        handler._content.perfManager.QueryAvailablePerfMetric.return_value = [
            mock_metric1,
            mock_metric2,
        ]

        # Mock counter map
        mock_counter1 = Mock()
        mock_counter1.groupInfo.key = "cpu"
        mock_counter1.nameInfo.key = "usage"
        mock_counter1.rollupType = "average"
        mock_counter1.key = 1

        mock_counter2 = Mock()
        mock_counter2.groupInfo.key = "disk"
        mock_counter2.nameInfo.key = "read"
        mock_counter2.rollupType = "average"
        mock_counter2.key = 2

        handler._content.perfManager.perfCounter = [mock_counter1, mock_counter2]

        result = await handler._list_available_metrics(
            {"entity_type": "vm", "entity_name": "test-vm"}
        )

        assert "metrics_by_group" in result
        assert "cpu" in result["metrics_by_group"]
        assert "disk" in result["metrics_by_group"]
        assert "supported_intervals" in result


class TestQuickStatsFallback:
    """Test quickStats fallback methods."""

    def test_vm_quick_stats_fallback(self):
        """Should extract VM quickStats correctly."""
        handler = MockPerformanceHandlerMixin()

        mock_vm = Mock()
        mock_vm.summary.quickStats.overallCpuUsage = 500
        mock_vm.summary.quickStats.guestMemoryUsage = 1024
        mock_vm.summary.quickStats.activeMemory = 800
        mock_vm.summary.quickStats.consumedOverheadMemory = 50
        mock_vm.summary.quickStats.balloonedMemory = 0
        mock_vm.summary.quickStats.swappedMemory = 0
        mock_vm.summary.quickStats.uptimeSeconds = 3600

        result = handler._get_quick_stats_fallback(mock_vm)

        assert result["cpu_usage_mhz"] == 500
        assert result["memory_usage_mb"] == 1024
        assert result["uptime_seconds"] == 3600

    def test_host_quick_stats_fallback(self):
        """Should extract host quickStats correctly."""
        handler = MockPerformanceHandlerMixin()

        mock_host = Mock()
        mock_host.summary.quickStats.overallCpuUsage = 2000
        mock_host.summary.quickStats.overallMemoryUsage = 8192
        mock_host.summary.hardware.cpuMhz = 3000
        mock_host.summary.hardware.numCpuCores = 8
        mock_host.summary.hardware.memorySize = 64 * 1024 * 1024 * 1024  # 64GB

        result = handler._get_host_quick_stats_fallback(mock_host)

        assert result["cpu_usage_mhz"] == 2000
        assert result["memory_usage_mb"] == 8192
        assert result["total_cpu_mhz"] == 24000  # 3000 * 8
        assert result["total_memory_mb"] == 65536  # 64GB


class TestMetricSummarization:
    """Test metric summarization."""

    def test_summarize_categorizes_metrics(self):
        """Should categorize metrics into groups."""
        handler = MockPerformanceHandlerMixin()

        parsed_data = {
            "metrics": {
                "cpu.usage.average": {"avg": 50, "max": 80, "min": 20, "latest": 60},
                "mem.usage.average": {"avg": 70, "max": 90, "min": 50, "latest": 75},
                "disk.read.average": {"avg": 100, "max": 200, "min": 50, "latest": 120},
                "net.received.average": {"avg": 500, "max": 1000, "min": 100, "latest": 600},
            }
        }

        summary = handler._summarize_metrics(parsed_data)

        assert "cpu" in summary
        assert "memory" in summary
        assert "disk" in summary
        assert "network" in summary

        # Check that metrics are in correct categories
        assert any("cpu" in k for k in summary["cpu"])
        assert any("mem" in k for k in summary["memory"])


class TestHostDiagnosticHighlights:
    """Test host-level diagnostic highlights."""

    def test_high_cpu_usage_generates_issue(self):
        """High host CPU usage should generate an issue."""
        handler = MockPerformanceHandlerMixin()

        summary = {
            "cpu": {"cpu.usage.average": {"avg": 95}},
            "memory": {},
            "disk": {},
            "network": {},
            "datastore": {},
        }

        mock_host = Mock()
        highlights = handler._generate_host_diagnostic_highlights(summary, mock_host)

        assert len(highlights["issues"]) > 0
        assert "CPU" in highlights["issues"][0]

    def test_critical_memory_generates_issue(self):
        """Critical memory usage should generate an issue."""
        handler = MockPerformanceHandlerMixin()

        summary = {
            "cpu": {},
            "memory": {"mem.usage.average": {"avg": 98}},
            "disk": {},
            "network": {},
            "datastore": {},
        }

        mock_host = Mock()
        highlights = handler._generate_host_diagnostic_highlights(summary, mock_host)

        assert len(highlights["issues"]) > 0
        assert "memory" in highlights["issues"][0].lower()


class TestClusterAggregation:
    """Test cluster metrics aggregation."""

    def test_aggregate_host_metrics(self):
        """Should aggregate metrics across hosts."""
        handler = MockPerformanceHandlerMixin()

        host_metrics = [
            {
                "host_name": "host1",
                "quick_stats": {"cpu_usage_mhz": 1000, "memory_usage_mb": 4096},
                "diagnostic_highlights": {"issues": [], "warnings": ["warning1"]},
            },
            {
                "host_name": "host2",
                "quick_stats": {"cpu_usage_mhz": 2000, "memory_usage_mb": 8192},
                "diagnostic_highlights": {"issues": ["issue1"], "warnings": []},
            },
        ]

        aggregated = handler._aggregate_host_metrics(host_metrics)

        assert aggregated["cpu"]["total_usage_mhz"] == 3000
        assert aggregated["cpu"]["hosts_reporting"] == 2
        assert aggregated["memory"]["total_usage_mb"] == 12288
        assert aggregated["issues_count"] == 1
        assert aggregated["warnings_count"] == 1

    def test_aggregate_skips_error_hosts(self):
        """Should skip hosts with errors in aggregation."""
        handler = MockPerformanceHandlerMixin()

        host_metrics = [
            {
                "host_name": "host1",
                "quick_stats": {"cpu_usage_mhz": 1000, "memory_usage_mb": 4096},
                "diagnostic_highlights": {"issues": [], "warnings": []},
            },
            {
                "host_name": "host2",
                "error": "Connection failed",
            },
        ]

        aggregated = handler._aggregate_host_metrics(host_metrics)

        assert aggregated["cpu"]["total_usage_mhz"] == 1000
        assert aggregated["cpu"]["hosts_reporting"] == 1
