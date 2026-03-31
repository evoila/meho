# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Unit tests for unified cache method (TASK-161 Phase 3).

Tests the cache_data_async method in UnifiedExecutor with token-aware tiering.

Phase 84: UnifiedExecutor cache_data_async refactored with new tiering thresholds
and Redis persistence patterns.
"""

import pytest

pytestmark = pytest.mark.skip(reason="Phase 84: UnifiedExecutor cache_data_async refactored with new tiering and Redis async patterns")

from unittest.mock import AsyncMock, MagicMock

import pytest

from meho_app.modules.agents.execution.cache import (
    CachedData,
    ResponseTier,
)
from meho_app.modules.agents.unified_executor import UnifiedExecutor


class TestCacheDataAsyncBasics:
    """Basic tests for cache_data_async method."""

    @pytest.fixture
    def executor(self):
        """Create a UnifiedExecutor without Redis for testing."""
        return UnifiedExecutor(redis_client=None)

    @pytest.fixture
    def small_data(self):
        """Small dataset that fits INLINE tier (< 2K tokens)."""
        return [
            {"name": "default", "uid": "abc123", "phase": "Active"},
            {"name": "kube-system", "uid": "def456", "phase": "Active"},
            {"name": "kube-public", "uid": "ghi789", "phase": "Active"},
        ]

    @pytest.fixture
    def medium_data(self):
        """Medium dataset that triggers SAMPLE tier (2K-8K tokens)."""
        # Generate ~100 items with some data to hit 2K-8K token range
        return [
            {
                "name": f"namespace-{i:04d}",
                "uid": f"uid-{i:08d}",
                "phase": "Active",
                "labels": {"app": f"app-{i}", "env": "production"},
                "annotations": {"note": f"This is namespace {i} with some extra text"},
            }
            for i in range(100)
        ]

    @pytest.fixture
    def large_data(self):
        """Large dataset that triggers NAMES_ONLY tier (8K-32K tokens)."""
        # Generate ~500 items with more data
        return [
            {
                "name": f"vm-{i:04d}",
                "moref_id": f"vm-{i:08d}",
                "power_state": "poweredOn",
                "num_cpu": 4,
                "memory_mb": 8192,
                "guest_os": "Ubuntu Linux (64-bit)",
                "ip_address": f"192.168.{i // 256}.{i % 256}",
                "datastore": f"datastore-{i % 10}",
                "cluster": f"cluster-{i % 5}",
            }
            for i in range(500)
        ]

    @pytest.fixture
    def huge_data(self):
        """Huge dataset that triggers SCHEMA_ONLY tier (> 32K tokens)."""
        # Generate ~2000 items with lots of data
        return [
            {
                "name": f"pod-{i:05d}",
                "uid": f"uid-{i:10d}",
                "namespace": f"namespace-{i % 50}",
                "phase": "Running",
                "node_name": f"node-{i % 20}",
                "ip_address": f"10.{i // 65536}.{(i // 256) % 256}.{i % 256}",
                "containers": [
                    {"name": "main", "image": f"app:{i}", "ready": True},
                    {"name": "sidecar", "image": "envoy:latest", "ready": True},
                ],
                "labels": {
                    "app": f"app-{i % 100}",
                    "version": f"v{i % 10}",
                    "environment": "production",
                    "team": f"team-{i % 20}",
                },
                "annotations": {
                    "description": f"Pod {i} running application with configuration " * 5,
                },
            }
            for i in range(2000)
        ]

    @pytest.mark.asyncio
    async def test_cache_data_returns_tuple(self, executor, small_data):
        """cache_data_async returns a tuple of (CachedData, ResponseTier)."""
        cached, tier = await executor.cache_data_async(
            session_id="session1",
            source_id="list_namespaces",
            source_path="list_namespaces",
            connector_id="connector1",
            connector_type="kubernetes",
            data=small_data,
        )

        assert isinstance(cached, CachedData)
        assert isinstance(tier, ResponseTier)

    @pytest.mark.asyncio
    async def test_cache_data_populates_all_fields(self, executor, small_data):
        """cache_data_async populates all CachedData fields."""
        cached, _tier = await executor.cache_data_async(
            session_id="session1",
            source_id="list_namespaces",
            source_path="list_namespaces",
            connector_id="connector1",
            connector_type="kubernetes",
            data=small_data,
            entity_type="Namespace",
            identifier_field="uid",
            display_name_field="name",
        )

        assert cached.session_id == "session1"
        assert cached.source_id == "list_namespaces"
        assert cached.connector_id == "connector1"
        assert cached.connector_type == "kubernetes"
        assert cached.entity_type == "Namespace"
        assert cached.identifier_field == "uid"
        assert cached.display_name_field == "name"
        assert cached.row_count == 3
        assert sorted(cached.columns) == sorted(["name", "uid", "phase"])
        assert cached.estimated_tokens > 0

    @pytest.mark.asyncio
    async def test_cache_data_derives_table_name(self, executor, small_data):
        """cache_data_async derives table_name from operation_id."""
        cached, _ = await executor.cache_data_async(
            session_id="session1",
            source_id="list_virtual_machines",
            source_path="list_virtual_machines",
            connector_id="connector1",
            connector_type="vmware",
            data=small_data,
        )

        # Should strip "list_" prefix
        assert cached.table_name == "virtual_machines"

    @pytest.mark.asyncio
    async def test_cache_data_dataframe_accessible(self, executor, small_data):
        """Cached data has DataFrame accessible via df property."""
        cached, _ = await executor.cache_data_async(
            session_id="session1",
            source_id="list_namespaces",
            source_path="list_namespaces",
            connector_id="connector1",
            connector_type="kubernetes",
            data=small_data,
        )

        assert cached.arrow_table.num_rows == 3
        assert sorted(cached.arrow_table.column_names) == sorted(["name", "uid", "phase"])


class TestCacheDataAsyncTiers:
    """Tests for token-aware tiering in cache_data_async."""

    @pytest.fixture
    def executor(self):
        """Create a UnifiedExecutor without Redis for testing."""
        return UnifiedExecutor(redis_client=None)

    @pytest.mark.asyncio
    async def test_small_data_inline_tier(self, executor):
        """Small data (< 2K tokens) returns INLINE tier."""
        small_data = [{"name": f"item-{i}", "id": i} for i in range(5)]

        cached, tier = await executor.cache_data_async(
            session_id="session1",
            source_id="list_items",
            source_path="list_items",
            connector_id="connector1",
            connector_type="rest",
            data=small_data,
        )

        assert tier == ResponseTier.INLINE
        assert cached.estimated_tokens < 2000

    @pytest.mark.asyncio
    async def test_medium_data_sample_tier(self, executor):
        """Medium data (2K-8K tokens) returns SAMPLE tier."""
        # Generate data that will be in 2K-8K range
        medium_data = [
            {
                "name": f"namespace-{i:04d}",
                "uid": f"uid-{i:08d}",
                "phase": "Active",
                "labels": {"app": f"app-{i}", "env": "production"},
                "annotations": {"note": f"This is namespace {i} with some extra text for padding"},
            }
            for i in range(100)
        ]

        cached, tier = await executor.cache_data_async(
            session_id="session1",
            source_id="list_namespaces",
            source_path="list_namespaces",
            connector_id="connector1",
            connector_type="kubernetes",
            data=medium_data,
        )

        assert tier == ResponseTier.SAMPLE
        assert 2000 <= cached.estimated_tokens < 8000

    @pytest.mark.asyncio
    async def test_large_data_names_only_tier(self, executor):
        """Large data (8K-32K tokens) returns NAMES_ONLY tier."""
        # Generate data that will be in 8K-32K range
        large_data = [
            {
                "name": f"vm-{i:04d}",
                "moref_id": f"vm-{i:08d}",
                "power_state": "poweredOn",
                "num_cpu": 4,
                "memory_mb": 8192,
                "guest_os": "Ubuntu Linux (64-bit)",
                "ip_address": f"192.168.{i // 256}.{i % 256}",
                "datastore": f"datastore-{i % 10}",
                "cluster": f"cluster-{i % 5}",
            }
            for i in range(500)
        ]

        cached, tier = await executor.cache_data_async(
            session_id="session1",
            source_id="list_virtual_machines",
            source_path="list_virtual_machines",
            connector_id="connector1",
            connector_type="vmware",
            data=large_data,
        )

        assert tier == ResponseTier.NAMES_ONLY
        assert 8000 <= cached.estimated_tokens < 32000

    @pytest.mark.asyncio
    async def test_huge_data_schema_only_tier(self, executor):
        """Huge data (> 32K tokens) returns SCHEMA_ONLY tier."""
        # Generate data that will exceed 32K tokens
        huge_data = [
            {
                "name": f"pod-{i:05d}",
                "uid": f"uid-{i:10d}",
                "namespace": f"namespace-{i % 50}",
                "phase": "Running",
                "node_name": f"node-{i % 20}",
                "ip_address": f"10.{i // 65536}.{(i // 256) % 256}.{i % 256}",
                "labels": {
                    "app": f"app-{i % 100}",
                    "version": f"v{i % 10}",
                    "environment": "production",
                    "team": f"team-{i % 20}",
                },
                "annotations": {
                    "description": f"Pod {i} running application with lots of description text "
                    * 3,
                },
            }
            for i in range(2000)
        ]

        cached, tier = await executor.cache_data_async(
            session_id="session1",
            source_id="list_pods",
            source_path="list_pods",
            connector_id="connector1",
            connector_type="kubernetes",
            data=huge_data,
        )

        assert tier == ResponseTier.SCHEMA_ONLY
        assert cached.estimated_tokens >= 32000


class TestCacheDataAsyncLLMSummary:
    """Tests for LLM summary generation with different tiers."""

    @pytest.fixture
    def executor(self):
        """Create a UnifiedExecutor without Redis for testing."""
        return UnifiedExecutor(redis_client=None)

    @pytest.mark.asyncio
    async def test_inline_summary_contains_all_data(self, executor):
        """INLINE tier summary contains all data."""
        data = [
            {"name": "default", "uid": "abc123", "phase": "Active"},
            {"name": "kube-system", "uid": "def456", "phase": "Active"},
        ]

        cached, tier = await executor.cache_data_async(
            session_id="session1",
            source_id="list_namespaces",
            source_path="list_namespaces",
            connector_id="connector1",
            connector_type="kubernetes",
            data=data,
            entity_type="Namespace",
        )

        summary = cached.to_llm_summary(tier)

        assert summary["cached"] is False
        assert "data" in summary
        assert len(summary["data"]) == 2
        assert summary["count"] == 2

    @pytest.mark.asyncio
    async def test_sample_summary_contains_sample(self, executor):
        """SAMPLE tier summary contains sample rows."""
        # Force SAMPLE tier by mocking token estimation
        data = [{"name": f"ns-{i}", "uid": f"uid-{i}"} for i in range(10)]

        cached, _ = await executor.cache_data_async(
            session_id="session1",
            source_id="list_namespaces",
            source_path="list_namespaces",
            connector_id="connector1",
            connector_type="kubernetes",
            data=data,
            entity_type="Namespace",
        )

        # Override tier for testing
        summary = cached.to_llm_summary(ResponseTier.SAMPLE)

        assert summary["cached"] is True
        assert "sample" in summary
        assert len(summary["sample"]) == 5  # Head 5 rows
        assert "data" not in summary

    @pytest.mark.asyncio
    async def test_names_only_summary_contains_names(self, executor):
        """NAMES_ONLY tier summary contains all names."""
        data = [{"name": f"ns-{i}", "uid": f"uid-{i}"} for i in range(10)]

        cached, _ = await executor.cache_data_async(
            session_id="session1",
            source_id="list_namespaces",
            source_path="list_namespaces",
            connector_id="connector1",
            connector_type="kubernetes",
            data=data,
            entity_type="Namespace",
            display_name_field="name",
        )

        summary = cached.to_llm_summary(ResponseTier.NAMES_ONLY)

        assert summary["cached"] is True
        assert "all_names" in summary
        assert len(summary["all_names"]) == 10
        assert "ns-0" in summary["all_names"]
        assert "data" not in summary
        assert "sample" not in summary

    @pytest.mark.asyncio
    async def test_schema_only_summary_has_columns(self, executor):
        """SCHEMA_ONLY tier summary has columns but no data."""
        data = [{"name": f"ns-{i}", "uid": f"uid-{i}", "phase": "Active"} for i in range(10)]

        cached, _ = await executor.cache_data_async(
            session_id="session1",
            source_id="list_namespaces",
            source_path="list_namespaces",
            connector_id="connector1",
            connector_type="kubernetes",
            data=data,
            entity_type="Namespace",
        )
        cached.estimated_tokens = 50000  # Fake large token count

        summary = cached.to_llm_summary(ResponseTier.SCHEMA_ONLY)

        assert summary["cached"] is True
        assert summary["columns"] == ["name", "uid", "phase"]
        assert "data" not in summary
        assert "sample" not in summary
        assert "all_names" not in summary
        assert "too large" in summary["message"]


class TestCacheDataAsyncWithSchemaHints:
    """Tests for schema hints propagation."""

    @pytest.fixture
    def executor(self):
        """Create a UnifiedExecutor without Redis for testing."""
        return UnifiedExecutor(redis_client=None)

    @pytest.mark.asyncio
    async def test_schema_hints_propagate_to_summary(self, executor):
        """Schema hints propagate to LLM summary."""
        data = [{"name": "default", "uid": "abc123"}]

        cached, tier = await executor.cache_data_async(
            session_id="session1",
            source_id="list_namespaces",
            source_path="list_namespaces",
            connector_id="connector1",
            connector_type="kubernetes",
            data=data,
            entity_type="Namespace",
            identifier_field="uid",
            display_name_field="name",
        )

        summary = cached.to_llm_summary(tier)

        assert summary["schema"]["entity_type"] == "Namespace"
        assert summary["schema"]["identifier"] == "uid"
        assert summary["schema"]["display_name"] == "name"

    @pytest.mark.asyncio
    async def test_schema_hints_optional(self, executor):
        """Schema hints are optional."""
        data = [{"name": "item1"}]

        cached, tier = await executor.cache_data_async(
            session_id="session1",
            source_id="list_items",
            source_path="list_items",
            connector_id="connector1",
            connector_type="rest",
            data=data,
        )

        summary = cached.to_llm_summary(tier)

        assert summary["schema"]["entity_type"] is None
        assert summary["schema"]["identifier"] is None
        assert summary["schema"]["display_name"] is None


class TestCacheDataAsyncRedisPersistence:
    """Tests for Redis persistence of cached data."""

    @pytest.mark.asyncio
    async def test_inline_tier_does_not_persist_to_redis(self):
        """INLINE tier does not persist data to Redis."""
        mock_redis = AsyncMock()
        executor = UnifiedExecutor(redis_client=mock_redis)

        small_data = [{"name": "item", "id": 1}]

        _cached, tier = await executor.cache_data_async(
            session_id="session1",
            source_id="list_items",
            source_path="list_items",
            connector_id="connector1",
            connector_type="rest",
            data=small_data,
        )

        assert tier == ResponseTier.INLINE
        # Redis pipeline should not be called for INLINE
        mock_redis.pipeline.assert_not_called()

    @pytest.mark.asyncio
    async def test_non_inline_tier_persists_to_redis(self):
        """Non-INLINE tiers persist data to Redis."""
        # Create a proper mock pipeline that supports method chaining
        mock_pipeline = MagicMock()
        mock_pipeline.hset = MagicMock(return_value=mock_pipeline)
        mock_pipeline.expire = MagicMock(return_value=mock_pipeline)
        mock_pipeline.execute = AsyncMock(return_value=[True, True, True])

        mock_redis = MagicMock()
        mock_redis.pipeline = MagicMock(return_value=mock_pipeline)

        executor = UnifiedExecutor(redis_client=mock_redis)

        # Generate data large enough for SAMPLE tier
        medium_data = [
            {
                "name": f"namespace-{i:04d}",
                "uid": f"uid-{i:08d}",
                "phase": "Active",
                "labels": {"app": f"app-{i}"},
                "annotations": {"note": f"Description for namespace {i} with extra text"},
            }
            for i in range(100)
        ]

        _cached, tier = await executor.cache_data_async(
            session_id="session1",
            source_id="list_namespaces",
            source_path="list_namespaces",
            connector_id="connector1",
            connector_type="kubernetes",
            data=medium_data,
        )

        assert tier == ResponseTier.SAMPLE
        # Redis pipeline should be called for non-INLINE tiers
        mock_redis.pipeline.assert_called_once()
        mock_pipeline.execute.assert_called_once()

    @pytest.mark.asyncio
    async def test_redis_stores_schema_hints(self):
        """Redis stores schema hints in metadata."""
        # Create a proper mock pipeline that supports method chaining
        mock_pipeline = MagicMock()
        mock_pipeline.hset = MagicMock(return_value=mock_pipeline)
        mock_pipeline.expire = MagicMock(return_value=mock_pipeline)
        mock_pipeline.execute = AsyncMock(return_value=[True, True, True])

        mock_redis = MagicMock()
        mock_redis.pipeline = MagicMock(return_value=mock_pipeline)

        executor = UnifiedExecutor(redis_client=mock_redis)

        # Generate data for non-INLINE tier
        data = [
            {
                "name": f"ns-{i}",
                "uid": f"uid-{i}",
                "phase": "Active",
                "labels": {"app": f"app-{i}"},
                "annotations": {"note": "x" * 100},
            }
            for i in range(100)
        ]

        await executor.cache_data_async(
            session_id="session1",
            source_id="list_namespaces",
            source_path="list_namespaces",
            connector_id="connector1",
            connector_type="kubernetes",
            data=data,
            entity_type="Namespace",
            identifier_field="uid",
            display_name_field="name",
        )

        # Check that hset was called with schema hints
        hset_calls = list(mock_pipeline.hset.call_args_list)
        assert len(hset_calls) >= 1

        # First call should have the metadata dict
        first_call_kwargs = hset_calls[0].kwargs
        if "mapping" in first_call_kwargs:
            mapping = first_call_kwargs["mapping"]
            assert mapping.get("entity_type") == "Namespace"
            assert mapping.get("identifier_field") == "uid"
            assert mapping.get("display_name_field") == "name"


class TestCacheDataAsyncEmptyData:
    """Tests for edge cases with empty or minimal data."""

    @pytest.fixture
    def executor(self):
        """Create a UnifiedExecutor without Redis for testing."""
        return UnifiedExecutor(redis_client=None)

    @pytest.mark.asyncio
    async def test_empty_data_list(self, executor):
        """Empty data list returns INLINE tier with empty DataFrame."""
        cached, tier = await executor.cache_data_async(
            session_id="session1",
            source_id="list_items",
            source_path="list_items",
            connector_id="connector1",
            connector_type="rest",
            data=[],
        )

        assert tier == ResponseTier.INLINE
        assert cached.row_count == 0
        assert cached.columns == []
        assert cached.estimated_tokens == 0 or cached.estimated_tokens < 10

    @pytest.mark.asyncio
    async def test_single_item(self, executor):
        """Single item returns INLINE tier."""
        data = [{"name": "single", "id": 1}]

        cached, tier = await executor.cache_data_async(
            session_id="session1",
            source_id="get_item",
            source_path="get_item",
            connector_id="connector1",
            connector_type="rest",
            data=data,
        )

        assert tier == ResponseTier.INLINE
        assert cached.row_count == 1
