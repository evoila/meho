# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Integration tests for Unified Token-Aware Cache (TASK-161).

Tests the flow from handlers → database schema hints → unified cache → LLM response.
"""

import pytest

from meho_app.modules.agents.execution.cache import (
    ResponseTier,
    determine_response_tier,
    estimate_tokens,
)
from meho_app.modules.agents.unified_executor import UnifiedExecutor


class TestCacheDataAsync:
    """Tests for the unified cache_data_async method."""

    @pytest.fixture
    def executor(self):
        """Create executor without Redis for testing."""
        return UnifiedExecutor(redis_client=None)

    @pytest.mark.asyncio
    async def test_cache_data_returns_inline_tier_for_small_data(self, executor):
        """Small responses should return INLINE tier."""
        data = [{"name": "ns1", "uid": "abc"}, {"name": "ns2", "uid": "def"}]

        cached, tier = await executor.cache_data_async(
            session_id="test-session",
            source_id="list_namespaces",
            source_path="list_namespaces",
            connector_id="k8s-connector",
            connector_type="kubernetes",
            data=data,
            entity_type="Namespace",
            identifier_field="uid",
            display_name_field="name",
        )

        assert tier == ResponseTier.INLINE
        assert cached.row_count == 2
        assert cached.entity_type == "Namespace"
        assert cached.identifier_field == "uid"
        assert cached.display_name_field == "name"

    @pytest.mark.asyncio
    async def test_cache_data_returns_sample_tier_for_medium_data(self, executor):
        """Medium responses should return SAMPLE tier."""
        # Create data that exceeds INLINE threshold (~2000 tokens)
        data = [
            {
                "name": f"ns-{i}",
                "uid": f"uid-{i}",
                "labels": {f"label-{j}": f"value-{j}" for j in range(10)},
            }
            for i in range(100)
        ]

        cached, tier = await executor.cache_data_async(
            session_id="test-session",
            source_id="list_namespaces",
            source_path="list_namespaces",
            connector_id="k8s-connector",
            connector_type="kubernetes",
            data=data,
        )

        assert tier in (ResponseTier.SAMPLE, ResponseTier.NAMES_ONLY)
        assert cached.row_count == 100

    @pytest.mark.asyncio
    async def test_cache_data_returns_names_only_tier_for_large_data(self, executor):
        """Large responses should return NAMES_ONLY tier."""
        # Create data that exceeds SAMPLE threshold (~8000 tokens)
        data = [
            {
                "name": f"vm-{i}",
                "moref_id": f"vm-{i}",
                "power_state": "poweredOn",
                "num_cpu": 4,
                "memory_mb": 8192,
                "guest_os": "Linux",
                "config": {"a": "b" * 100, "c": "d" * 100},
            }
            for i in range(200)
        ]

        cached, tier = await executor.cache_data_async(
            session_id="test-session",
            source_id="list_virtual_machines",
            source_path="list_virtual_machines",
            connector_id="vmware-connector",
            connector_type="vmware",
            data=data,
            entity_type="VirtualMachine",
            identifier_field="moref_id",
            display_name_field="name",
        )

        assert tier in (ResponseTier.NAMES_ONLY, ResponseTier.SCHEMA_ONLY)
        assert cached.row_count == 200
        assert cached.entity_type == "VirtualMachine"

    @pytest.mark.asyncio
    async def test_cache_data_schema_hints_propagate(self, executor):
        """Schema hints should propagate to CachedData."""
        data = [{"name": "pod-1", "uid": "abc123", "status": "Running"}]

        cached, _ = await executor.cache_data_async(
            session_id="test-session",
            source_id="list_pods",
            source_path="list_pods",
            connector_id="k8s-connector",
            connector_type="kubernetes",
            data=data,
            entity_type="Pod",
            identifier_field="uid",
            display_name_field="name",
        )

        assert cached.entity_type == "Pod"
        assert cached.identifier_field == "uid"
        assert cached.display_name_field == "name"
        assert cached.connector_type == "kubernetes"


class TestToLLMSummary:
    """Tests for the to_llm_summary method with different tiers."""

    @pytest.mark.asyncio
    async def test_inline_tier_includes_full_data(self):
        """INLINE tier should include all data."""
        executor = UnifiedExecutor(redis_client=None)
        data = [
            {"name": "ns1", "uid": "abc"},
            {"name": "ns2", "uid": "def"},
        ]

        cached, _tier = await executor.cache_data_async(
            session_id="test-session",
            source_id="list_namespaces",
            source_path="list_namespaces",
            connector_id="k8s-connector",
            connector_type="kubernetes",
            data=data,
            entity_type="Namespace",
            identifier_field="uid",
            display_name_field="name",
        )

        summary = cached.to_llm_summary(ResponseTier.INLINE)

        assert summary["success"] is True
        assert summary["cached"] is False  # INLINE means not cached
        assert "data" in summary
        assert len(summary["data"]) == 2
        assert summary["count"] == 2
        assert summary["schema"]["entity_type"] == "Namespace"

    @pytest.mark.asyncio
    async def test_sample_tier_includes_samples(self):
        """SAMPLE tier should include sample rows."""
        executor = UnifiedExecutor(redis_client=None)
        data = [{"name": f"ns-{i}", "uid": f"uid-{i}"} for i in range(50)]

        cached, _ = await executor.cache_data_async(
            session_id="test-session",
            source_id="list_namespaces",
            source_path="list_namespaces",
            connector_id="k8s-connector",
            connector_type="kubernetes",
            data=data,
        )

        summary = cached.to_llm_summary(ResponseTier.SAMPLE)

        assert summary["success"] is True
        assert summary["cached"] is True
        assert "sample" in summary
        assert len(summary["sample"]) <= 5  # Sample size
        assert summary["count"] == 50
        assert "table" in summary

    @pytest.mark.asyncio
    async def test_names_only_tier_includes_all_names(self):
        """NAMES_ONLY tier should include all entity names."""
        executor = UnifiedExecutor(redis_client=None)
        data = [{"name": f"vm-{i}", "moref_id": f"vm-{i}"} for i in range(100)]

        cached, _ = await executor.cache_data_async(
            session_id="test-session",
            source_id="list_virtual_machines",
            source_path="list_virtual_machines",
            connector_id="vmware-connector",
            connector_type="vmware",
            data=data,
            display_name_field="name",
        )

        summary = cached.to_llm_summary(ResponseTier.NAMES_ONLY)

        assert summary["success"] is True
        assert summary["cached"] is True
        assert "all_names" in summary
        assert len(summary["all_names"]) == 100
        assert summary["count"] == 100

    @pytest.mark.asyncio
    async def test_schema_only_tier_no_data(self):
        """SCHEMA_ONLY tier should not include any data."""
        executor = UnifiedExecutor(redis_client=None)
        data = [{"name": f"vm-{i}", "moref_id": f"vm-{i}"} for i in range(10)]

        cached, _ = await executor.cache_data_async(
            session_id="test-session",
            source_id="list_virtual_machines",
            source_path="list_virtual_machines",
            connector_id="vmware-connector",
            connector_type="vmware",
            data=data,
        )

        summary = cached.to_llm_summary(ResponseTier.SCHEMA_ONLY)

        assert summary["success"] is True
        assert summary["cached"] is True
        assert "data" not in summary
        assert "sample" not in summary
        assert "all_names" not in summary
        assert "columns" in summary
        assert summary["count"] == 10


class TestSchemaHintsFlow:
    """Tests for schema hints flowing from database to LLM response."""

    @pytest.mark.asyncio
    async def test_operation_schema_hints_to_llm_response(self):
        """Schema hints from connector_operation should appear in LLM response."""
        executor = UnifiedExecutor(redis_client=None)

        # Simulate what operation_handlers.py does
        data = [
            {"name": "default", "uid": "ns-001", "status": "Active"},
            {"name": "kube-system", "uid": "ns-002", "status": "Active"},
        ]

        # These would come from database query
        entity_type = "Namespace"
        identifier_field = "uid"
        display_name_field = "name"

        cached, tier = await executor.cache_data_async(
            session_id="test-session",
            source_id="list_namespaces",
            source_path="list_namespaces",
            connector_id="k8s-connector-123",
            connector_type="kubernetes",
            data=data,
            entity_type=entity_type,
            identifier_field=identifier_field,
            display_name_field=display_name_field,
        )

        summary = cached.to_llm_summary(tier)

        # Verify schema hints are in the response
        assert summary["schema"]["entity_type"] == "Namespace"
        assert summary["schema"]["identifier"] == "uid"
        assert summary["schema"]["display_name"] == "name"

    @pytest.mark.asyncio
    async def test_vmware_schema_hints_to_llm_response(self):
        """VMware operations should get correct schema hints."""
        executor = UnifiedExecutor(redis_client=None)

        data = [
            {"name": "vm-001", "moref_id": "vm-123", "power_state": "poweredOn"},
            {"name": "vm-002", "moref_id": "vm-456", "power_state": "poweredOff"},
        ]

        cached, tier = await executor.cache_data_async(
            session_id="test-session",
            source_id="list_virtual_machines",
            source_path="list_virtual_machines",
            connector_id="vmware-connector-123",
            connector_type="vmware",
            data=data,
            entity_type="VirtualMachine",
            identifier_field="moref_id",
            display_name_field="name",
        )

        summary = cached.to_llm_summary(tier)

        assert summary["schema"]["entity_type"] == "VirtualMachine"
        assert summary["schema"]["identifier"] == "moref_id"
        assert summary["schema"]["display_name"] == "name"


class TestTableNaming:
    """Tests for table name derivation."""

    @pytest.mark.asyncio
    async def test_table_name_from_list_operation(self):
        """List operations should derive clean table names."""
        executor = UnifiedExecutor(redis_client=None)

        data = [{"name": "ns1"}]

        cached, _ = await executor.cache_data_async(
            session_id="test-session",
            source_id="list_virtual_machines",
            source_path="list_virtual_machines",
            connector_id="vmware-connector",
            connector_type="vmware",
            data=data,
        )

        assert cached.table_name == "virtual_machines"

    @pytest.mark.asyncio
    async def test_table_name_from_get_operation(self):
        """Get operations should derive table names."""
        executor = UnifiedExecutor(redis_client=None)

        data = [{"name": "cluster-1"}]

        cached, _ = await executor.cache_data_async(
            session_id="test-session",
            source_id="get_all_clusters",
            source_path="get_all_clusters",
            connector_id="vmware-connector",
            connector_type="vmware",
            data=data,
        )

        assert cached.table_name == "clusters"


class TestTokenEstimation:
    """Tests for token estimation accuracy."""

    def test_estimate_tokens_small_data(self):
        """Small data should estimate low tokens."""
        data = [{"name": "test"}]
        tokens = estimate_tokens(data)
        assert tokens < 50

    def test_estimate_tokens_large_data(self):
        """Large data should estimate high tokens."""
        data = [{"name": f"item-{i}", "data": "x" * 100} for i in range(100)]
        tokens = estimate_tokens(data)
        assert tokens > 2000

    def test_determine_tier_inline(self):
        """Low token count should return INLINE."""
        assert determine_response_tier(500) == ResponseTier.INLINE

    def test_determine_tier_sample(self):
        """Medium token count should return SAMPLE."""
        assert determine_response_tier(5000) == ResponseTier.SAMPLE

    def test_determine_tier_names_only(self):
        """High token count should return NAMES_ONLY."""
        assert determine_response_tier(20000) == ResponseTier.NAMES_ONLY

    def test_determine_tier_schema_only(self):
        """Very high token count should return SCHEMA_ONLY."""
        assert determine_response_tier(50000) == ResponseTier.SCHEMA_ONLY


class TestConnectorTypeHandling:
    """Tests that all connector types are handled correctly."""

    @pytest.mark.asyncio
    async def test_kubernetes_connector_type(self):
        """Kubernetes connector type should be stored."""
        executor = UnifiedExecutor(redis_client=None)

        cached, _ = await executor.cache_data_async(
            session_id="test",
            source_id="list_pods",
            source_path="list_pods",
            connector_id="k8s-123",
            connector_type="kubernetes",
            data=[{"name": "pod-1"}],
        )

        assert cached.connector_type == "kubernetes"

    @pytest.mark.asyncio
    async def test_vmware_connector_type(self):
        """VMware connector type should be stored."""
        executor = UnifiedExecutor(redis_client=None)

        cached, _ = await executor.cache_data_async(
            session_id="test",
            source_id="list_vms",
            source_path="list_vms",
            connector_id="vcenter-123",
            connector_type="vmware",
            data=[{"name": "vm-1"}],
        )

        assert cached.connector_type == "vmware"

    @pytest.mark.asyncio
    async def test_proxmox_connector_type(self):
        """Proxmox connector type should be stored."""
        executor = UnifiedExecutor(redis_client=None)

        cached, _ = await executor.cache_data_async(
            session_id="test",
            source_id="list_nodes",
            source_path="list_nodes",
            connector_id="pve-123",
            connector_type="proxmox",
            data=[{"name": "node-1"}],
        )

        assert cached.connector_type == "proxmox"

    @pytest.mark.asyncio
    async def test_gcp_connector_type(self):
        """GCP connector type should be stored."""
        executor = UnifiedExecutor(redis_client=None)

        cached, _ = await executor.cache_data_async(
            session_id="test",
            source_id="list_instances",
            source_path="list_instances",
            connector_id="gcp-123",
            connector_type="gcp",
            data=[{"name": "instance-1"}],
        )

        assert cached.connector_type == "gcp"

    @pytest.mark.asyncio
    async def test_rest_connector_type(self):
        """REST connector type should be stored."""
        executor = UnifiedExecutor(redis_client=None)

        cached, _ = await executor.cache_data_async(
            session_id="test",
            source_id="get_resources",
            source_path="/api/resources",
            connector_id="rest-123",
            connector_type="rest",
            data=[{"name": "resource-1"}],
        )

        assert cached.connector_type == "rest"
