# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Tests for the Data Reduction adapter.

These tests validate the DataQuery-to-QueryEngine adapter that replaces
the deleted pandas-based DataReductionEngine.
"""

import pytest

from meho_app.modules.agents.data_reduction.adapter import execute_data_query
from meho_app.modules.agents.data_reduction.query_schema import (
    AggregateFunction,
    AggregateSpec,
    ComputeField,
    DataQuery,
    FilterCondition,
    FilterGroup,
    FilterOperator,
    ReducedData,
    SortSpec,
)

# =============================================================================
# Test Data Fixtures
# =============================================================================


@pytest.fixture
def sample_clusters():
    """Sample cluster data simulating a VCF/vSphere response."""
    return {
        "clusters": [
            {
                "name": "cluster-prod-01",
                "region": "us-east",
                "cpu_cores": 256,
                "cpu_used": 200,
                "memory_total_gb": 512,
                "memory_used_gb": 460,
                "status": "healthy",
            },
            {
                "name": "cluster-prod-02",
                "region": "us-west",
                "cpu_cores": 128,
                "cpu_used": 80,
                "memory_total_gb": 256,
                "memory_used_gb": 180,
                "status": "healthy",
            },
            {
                "name": "cluster-dev-01",
                "region": "us-east",
                "cpu_cores": 64,
                "cpu_used": 30,
                "memory_total_gb": 128,
                "memory_used_gb": 60,
                "status": "warning",
            },
            {
                "name": "cluster-staging",
                "region": "eu-west",
                "cpu_cores": 32,
                "cpu_used": 10,
                "memory_total_gb": 64,
                "memory_used_gb": 20,
                "status": "healthy",
            },
            {
                "name": "cluster-prod-03",
                "region": "us-east",
                "cpu_cores": 256,
                "cpu_used": 240,
                "memory_total_gb": 512,
                "memory_used_gb": 500,
                "status": "critical",
            },
        ]
    }


@pytest.fixture
def sample_pods():
    """Sample Kubernetes pod data."""
    return {
        "items": [
            {
                "metadata": {"name": "pod-1", "namespace": "default"},
                "status": {"phase": "Running"},
                "spec": {"containers": [{"resources": {"requests": {"cpu": "100m"}}}]},
            },
            {
                "metadata": {"name": "pod-2", "namespace": "default"},
                "status": {"phase": "Running"},
                "spec": {"containers": [{"resources": {"requests": {"cpu": "200m"}}}]},
            },
            {
                "metadata": {"name": "pod-3", "namespace": "kube-system"},
                "status": {"phase": "Pending"},
                "spec": {"containers": [{"resources": {"requests": {"cpu": "50m"}}}]},
            },
            {
                "metadata": {"name": "pod-4", "namespace": "default"},
                "status": {"phase": "Failed"},
                "spec": {"containers": [{"resources": {"requests": {"cpu": "100m"}}}]},
            },
            {
                "metadata": {"name": "pod-5", "namespace": "monitoring"},
                "status": {"phase": "Running"},
                "spec": {"containers": [{"resources": {"requests": {"cpu": "500m"}}}]},
            },
        ]
    }


# =============================================================================
# Query Schema Tests
# =============================================================================


class TestQuerySchema:
    """Tests for DataQuery and related models."""

    def test_simple_query_creation(self):
        """Test creating a simple query."""
        query = DataQuery(
            source_path="clusters",
            select=["name", "status"],
            limit=10,
        )
        assert query.source_path == "clusters"
        assert query.select == ["name", "status"]
        assert query.limit == 10

    def test_query_with_filter(self):
        """Test creating a query with filters."""
        query = DataQuery(
            source_path="items",
            filter=FilterGroup(
                conditions=[
                    FilterCondition(field="status", operator=FilterOperator.EQ, value="Running")
                ]
            ),
        )
        assert query.filter is not None
        assert len(query.filter.conditions) == 1

    def test_query_with_compute(self):
        """Test creating a query with computed fields."""
        query = DataQuery(
            source_path="clusters",
            compute=[ComputeField(name="cpu_pct", expression="cpu_used / cpu_cores * 100")],
        )
        assert len(query.compute) == 1
        assert query.compute[0].name == "cpu_pct"

    def test_compute_field_validation(self):
        """Test that dangerous expressions are rejected."""
        with pytest.raises(ValueError, match="forbidden term"):
            ComputeField(name="bad", expression="__import__('os')")

        with pytest.raises(ValueError, match="forbidden term"):
            ComputeField(name="bad", expression="exec('code')")

    def test_query_with_aggregates(self):
        """Test creating a query with aggregations."""
        query = DataQuery(
            source_path="clusters",
            aggregates=[
                AggregateSpec(
                    name="total_memory", function=AggregateFunction.SUM, field="memory_total_gb"
                ),
                AggregateSpec(name="cluster_count", function=AggregateFunction.COUNT, field="*"),
            ],
        )
        assert len(query.aggregates) == 2


class TestReducedData:
    """Tests for ReducedData model."""

    def test_reduced_data_properties(self):
        """Test ReducedData computed properties."""
        query = DataQuery(source_path="test")
        data = ReducedData(
            records=[{"a": 1}, {"a": 2}],
            total_source_records=100,
            total_after_filter=50,
            returned_records=2,
            aggregates={"sum": 3},
            query_applied=query,
            processing_time_ms=10.5,
        )

        assert data.is_truncated  # 2 < 50
        assert data.reduction_ratio == pytest.approx(0.02)  # 2/100

    def test_to_llm_context(self):
        """Test formatting for LLM context."""
        query = DataQuery(source_path="test")
        data = ReducedData(
            records=[{"name": "item1"}, {"name": "item2"}],
            total_source_records=100,
            total_after_filter=2,
            returned_records=2,
            aggregates={"count": 2},
            query_applied=query,
            processing_time_ms=5.0,
        )

        context = data.to_llm_context()
        assert "2 of 100 records" in context
        assert "count: 2" in context


# =============================================================================
# Adapter Tests - Source Extraction
# =============================================================================


class TestSourceExtraction:
    """Tests for extracting data from different source structures."""

    def test_extract_from_nested_path(self, sample_clusters):
        """Test extracting data from a nested path."""
        query = DataQuery(source_path="clusters")
        result = execute_data_query(sample_clusters, query)

        assert result.total_source_records == 5
        assert len(result.records) == 5

    def test_extract_from_items(self, sample_pods):
        """Test extracting from 'items' path."""
        query = DataQuery(source_path="items")
        result = execute_data_query(sample_pods, query)

        assert result.total_source_records == 5

    def test_extract_from_root_list(self):
        """Test extracting when root is a list."""
        data = [{"a": 1}, {"a": 2}, {"a": 3}]
        query = DataQuery(source_path="")
        result = execute_data_query(data, query)

        assert result.total_source_records == 3

    def test_extract_empty_path(self):
        """Test extracting with empty source path."""
        data = {"data": [{"x": 1}]}
        query = DataQuery(source_path="")
        result = execute_data_query(data, query)

        # Should find 'data' key automatically
        assert result.total_source_records == 1


# =============================================================================
# Adapter Tests - Field Selection
# =============================================================================


class TestFieldSelection:
    """Tests for field selection."""

    def test_select_specific_fields(self, sample_clusters):
        """Test selecting specific fields."""
        query = DataQuery(
            source_path="clusters",
            select=["name", "region"],
        )
        result = execute_data_query(sample_clusters, query)

        assert len(result.records) == 5
        assert set(result.records[0].keys()) == {"name", "region"}

    def test_select_all_fields(self, sample_clusters):
        """Test selecting all fields (select=None)."""
        query = DataQuery(source_path="clusters")
        result = execute_data_query(sample_clusters, query)

        # Should have all fields
        assert "cpu_cores" in result.records[0]
        assert "memory_total_gb" in result.records[0]


# =============================================================================
# Adapter Tests - Computed Fields
# =============================================================================


class TestComputedFields:
    """Tests for computed/derived fields."""

    def test_compute_percentage(self, sample_clusters):
        """Test computing a percentage field."""
        query = DataQuery(
            source_path="clusters",
            select=["name", "memory_total_gb", "memory_used_gb"],
            compute=[
                ComputeField(name="memory_pct", expression="memory_used_gb / memory_total_gb * 100")
            ],
        )
        result = execute_data_query(sample_clusters, query)

        assert "memory_pct" in result.records[0]
        # cluster-prod-01: 460/512 * 100 ~ 89.84
        prod_01 = next(r for r in result.records if r["name"] == "cluster-prod-01")
        assert 89 < prod_01["memory_pct"] < 90

    def test_compute_multiple_fields(self, sample_clusters):
        """Test computing multiple derived fields."""
        query = DataQuery(
            source_path="clusters",
            compute=[
                ComputeField(name="cpu_pct", expression="cpu_used / cpu_cores * 100"),
                ComputeField(
                    name="memory_pct", expression="memory_used_gb / memory_total_gb * 100"
                ),
            ],
        )
        result = execute_data_query(sample_clusters, query)

        assert "cpu_pct" in result.records[0]
        assert "memory_pct" in result.records[0]


# =============================================================================
# Adapter Tests - Filtering
# =============================================================================


class TestFiltering:
    """Tests for filter conditions."""

    def test_simple_equality_filter(self, sample_clusters):
        """Test simple equality filter."""
        query = DataQuery(
            source_path="clusters",
            filter=FilterGroup(
                conditions=[
                    FilterCondition(field="status", operator=FilterOperator.EQ, value="healthy")
                ]
            ),
        )
        result = execute_data_query(sample_clusters, query)

        assert result.total_after_filter == 3
        assert all(r["status"] == "healthy" for r in result.records)

    def test_greater_than_filter(self, sample_clusters):
        """Test greater than filter."""
        query = DataQuery(
            source_path="clusters",
            filter=FilterGroup(
                conditions=[
                    FilterCondition(field="memory_total_gb", operator=FilterOperator.GT, value=100)
                ]
            ),
        )
        result = execute_data_query(sample_clusters, query)

        assert result.total_after_filter == 4  # All except cluster-staging (64GB)

    def test_filter_on_computed_field(self, sample_clusters):
        """Test filtering on a computed field."""
        query = DataQuery(
            source_path="clusters",
            compute=[
                ComputeField(name="memory_pct", expression="memory_used_gb / memory_total_gb * 100")
            ],
            filter=FilterGroup(
                conditions=[
                    FilterCondition(field="memory_pct", operator=FilterOperator.GT, value=80)
                ]
            ),
        )
        result = execute_data_query(sample_clusters, query)

        # cluster-prod-01 (89.8%), cluster-prod-03 (97.6%) should match
        assert result.total_after_filter == 2

    def test_contains_filter(self, sample_clusters):
        """Test string contains filter."""
        query = DataQuery(
            source_path="clusters",
            filter=FilterGroup(
                conditions=[
                    FilterCondition(field="name", operator=FilterOperator.CONTAINS, value="prod")
                ]
            ),
        )
        result = execute_data_query(sample_clusters, query)

        assert result.total_after_filter == 3
        assert all("prod" in r["name"] for r in result.records)

    def test_in_filter(self, sample_clusters):
        """Test IN filter."""
        query = DataQuery(
            source_path="clusters",
            filter=FilterGroup(
                conditions=[
                    FilterCondition(
                        field="region", operator=FilterOperator.IN, value=["us-east", "eu-west"]
                    )
                ]
            ),
        )
        result = execute_data_query(sample_clusters, query)

        assert result.total_after_filter == 4  # 3 us-east + 1 eu-west

    def test_and_filter(self, sample_clusters):
        """Test AND filter logic."""
        query = DataQuery(
            source_path="clusters",
            filter=FilterGroup(
                conditions=[
                    FilterCondition(field="status", operator=FilterOperator.EQ, value="healthy"),
                    FilterCondition(field="region", operator=FilterOperator.EQ, value="us-east"),
                ],
                logic="and",
            ),
        )
        result = execute_data_query(sample_clusters, query)

        assert result.total_after_filter == 1  # Only cluster-prod-01

    def test_or_filter(self, sample_clusters):
        """Test OR filter logic."""
        query = DataQuery(
            source_path="clusters",
            filter=FilterGroup(
                conditions=[
                    FilterCondition(field="status", operator=FilterOperator.EQ, value="critical"),
                    FilterCondition(field="status", operator=FilterOperator.EQ, value="warning"),
                ],
                logic="or",
            ),
        )
        result = execute_data_query(sample_clusters, query)

        assert result.total_after_filter == 2  # dev-01 (warning) + prod-03 (critical)


# =============================================================================
# Adapter Tests - Sorting
# =============================================================================


class TestSorting:
    """Tests for sorting."""

    def test_sort_descending(self, sample_clusters):
        """Test descending sort."""
        query = DataQuery(
            source_path="clusters",
            sort=SortSpec(field="memory_total_gb", direction="desc"),
        )
        result = execute_data_query(sample_clusters, query)

        memories = [r["memory_total_gb"] for r in result.records]
        assert memories == sorted(memories, reverse=True)

    def test_sort_ascending(self, sample_clusters):
        """Test ascending sort."""
        query = DataQuery(
            source_path="clusters",
            sort=SortSpec(field="name", direction="asc"),
        )
        result = execute_data_query(sample_clusters, query)

        names = [r["name"] for r in result.records]
        assert names == sorted(names)

    def test_sort_on_computed_field(self, sample_clusters):
        """Test sorting on a computed field."""
        query = DataQuery(
            source_path="clusters",
            compute=[
                ComputeField(name="memory_pct", expression="memory_used_gb / memory_total_gb * 100")
            ],
            sort=SortSpec(field="memory_pct", direction="desc"),
        )
        result = execute_data_query(sample_clusters, query)

        pcts = [r["memory_pct"] for r in result.records]
        assert pcts == sorted(pcts, reverse=True)


# =============================================================================
# Adapter Tests - Pagination
# =============================================================================


class TestPagination:
    """Tests for limit and offset."""

    def test_limit(self, sample_clusters):
        """Test limiting results."""
        query = DataQuery(
            source_path="clusters",
            limit=2,
        )
        result = execute_data_query(sample_clusters, query)

        assert len(result.records) == 2
        assert result.total_source_records == 5
        assert result.is_truncated

    def test_offset(self, sample_clusters):
        """Test offset with sort."""
        query = DataQuery(
            source_path="clusters",
            sort=SortSpec(field="name", direction="asc"),
            offset=2,
            limit=2,
        )
        result = execute_data_query(sample_clusters, query)

        # Should skip first 2, get next 2
        assert len(result.records) == 2


# =============================================================================
# Adapter Tests - Aggregations
# =============================================================================


class TestAggregations:
    """Tests for aggregate functions."""

    def test_count_aggregate(self, sample_clusters):
        """Test COUNT aggregation."""
        query = DataQuery(
            source_path="clusters",
            aggregates=[
                AggregateSpec(name="total_clusters", function=AggregateFunction.COUNT, field="*")
            ],
        )
        result = execute_data_query(sample_clusters, query)

        assert result.aggregates["total_clusters"] == 5

    def test_sum_aggregate(self, sample_clusters):
        """Test SUM aggregation."""
        query = DataQuery(
            source_path="clusters",
            aggregates=[
                AggregateSpec(
                    name="total_memory", function=AggregateFunction.SUM, field="memory_total_gb"
                )
            ],
        )
        result = execute_data_query(sample_clusters, query)

        expected = 512 + 256 + 128 + 64 + 512
        assert result.aggregates["total_memory"] == expected

    def test_avg_aggregate(self, sample_clusters):
        """Test AVG aggregation."""
        query = DataQuery(
            source_path="clusters",
            aggregates=[
                AggregateSpec(
                    name="avg_memory", function=AggregateFunction.AVG, field="memory_total_gb"
                )
            ],
        )
        result = execute_data_query(sample_clusters, query)

        expected = (512 + 256 + 128 + 64 + 512) / 5
        assert abs(result.aggregates["avg_memory"] - expected) < 0.01

    def test_min_max_aggregate(self, sample_clusters):
        """Test MIN and MAX aggregations."""
        query = DataQuery(
            source_path="clusters",
            aggregates=[
                AggregateSpec(
                    name="min_memory", function=AggregateFunction.MIN, field="memory_total_gb"
                ),
                AggregateSpec(
                    name="max_memory", function=AggregateFunction.MAX, field="memory_total_gb"
                ),
            ],
        )
        result = execute_data_query(sample_clusters, query)

        assert result.aggregates["min_memory"] == 64
        assert result.aggregates["max_memory"] == 512

    def test_aggregates_with_filter(self, sample_clusters):
        """Test aggregations are computed after filtering."""
        query = DataQuery(
            source_path="clusters",
            filter=FilterGroup(
                conditions=[
                    FilterCondition(field="status", operator=FilterOperator.EQ, value="healthy")
                ]
            ),
            aggregates=[
                AggregateSpec(name="healthy_count", function=AggregateFunction.COUNT, field="*"),
                AggregateSpec(
                    name="healthy_memory", function=AggregateFunction.SUM, field="memory_total_gb"
                ),
            ],
        )
        result = execute_data_query(sample_clusters, query)

        assert result.aggregates["healthy_count"] == 3
        assert result.aggregates["healthy_memory"] == 512 + 256 + 64  # prod-01, prod-02, staging


# =============================================================================
# Adapter Tests - Complex Queries
# =============================================================================


class TestComplexQueries:
    """Tests for complex, real-world queries."""

    def test_high_memory_clusters_query(self, sample_clusters):
        """
        Real-world query: Find clusters with high memory utilization,
        sorted by utilization, with summary stats.
        """
        query = DataQuery(
            source_path="clusters",
            select=["name", "region", "memory_used_gb", "memory_total_gb"],
            compute=[
                ComputeField(name="memory_pct", expression="memory_used_gb / memory_total_gb * 100")
            ],
            filter=FilterGroup(
                conditions=[
                    FilterCondition(field="memory_pct", operator=FilterOperator.GT, value=70)
                ]
            ),
            sort=SortSpec(field="memory_pct", direction="desc"),
            limit=10,
            aggregates=[
                AggregateSpec(
                    name="avg_utilization", function=AggregateFunction.AVG, field="memory_pct"
                ),
                AggregateSpec(name="count", function=AggregateFunction.COUNT, field="*"),
            ],
        )
        result = execute_data_query(sample_clusters, query)

        # prod-01 (89.8%), prod-02 (70.3%), prod-03 (97.6%)
        assert result.total_after_filter == 3
        assert result.records[0]["name"] == "cluster-prod-03"  # Highest
        assert "avg_utilization" in result.aggregates

    def test_regional_summary_query(self, sample_clusters):
        """
        Query: Get total capacity by region.
        """
        query = DataQuery(
            source_path="clusters",
            select=["name", "region", "cpu_cores", "memory_total_gb"],
            filter=FilterGroup(
                conditions=[
                    FilterCondition(field="region", operator=FilterOperator.EQ, value="us-east")
                ]
            ),
            aggregates=[
                AggregateSpec(name="total_cpu", function=AggregateFunction.SUM, field="cpu_cores"),
                AggregateSpec(
                    name="total_memory", function=AggregateFunction.SUM, field="memory_total_gb"
                ),
                AggregateSpec(name="cluster_count", function=AggregateFunction.COUNT, field="*"),
            ],
        )
        result = execute_data_query(sample_clusters, query)

        # us-east: prod-01 (256 CPU, 512 MEM), dev-01 (64 CPU, 128 MEM), prod-03 (256 CPU, 512 MEM)
        assert result.aggregates["total_cpu"] == 256 + 64 + 256
        assert result.aggregates["total_memory"] == 512 + 128 + 512
        assert result.aggregates["cluster_count"] == 3


# =============================================================================
# Adapter Tests - Edge Cases
# =============================================================================


class TestEdgeCases:
    """Tests for edge cases and error handling."""

    def test_empty_data(self):
        """Test handling empty data."""
        query = DataQuery(source_path="clusters")
        result = execute_data_query({"clusters": []}, query)

        assert result.total_source_records == 0
        assert result.records == []

    def test_missing_source_path(self):
        """Test handling missing source path."""
        query = DataQuery(source_path="nonexistent")
        result = execute_data_query({"data": []}, query)

        assert result.total_source_records == 0

    def test_engine_py_deleted(self):
        """Test that import of the deleted engine.py raises ImportError."""
        with pytest.raises(ImportError):
            import meho_app.modules.agents.data_reduction.engine  # noqa: F401


# =============================================================================
# Performance Tests
# =============================================================================


class TestPerformance:
    """Tests for performance characteristics."""

    def test_large_dataset_processing(self):
        """Test processing a larger dataset."""
        # Generate 1000 records
        data = {
            "items": [{"id": i, "value": i * 10, "category": f"cat-{i % 10}"} for i in range(1000)]
        }

        query = DataQuery(
            source_path="items",
            filter=FilterGroup(
                conditions=[FilterCondition(field="value", operator=FilterOperator.GT, value=5000)]
            ),
            sort=SortSpec(field="value", direction="desc"),
            limit=100,
        )

        result = execute_data_query(data, query)

        assert result.total_source_records == 1000
        assert result.total_after_filter == 499  # 501-1000
        assert result.returned_records == 100
        assert result.processing_time_ms < 1000  # Should be fast

    def test_max_records_safety_limit(self):
        """Test max_records safety limit truncates input."""
        data = {"items": [{"id": i} for i in range(100)]}

        query = DataQuery(source_path="items")
        result = execute_data_query(data, query, max_records=50)

        # Only first 50 processed, but total_source shows 100
        assert result.total_source_records == 100
        assert result.returned_records <= 50

    def test_max_output_records_limit(self):
        """Test max_output_records caps the output."""
        data = {"items": [{"id": i} for i in range(100)]}

        query = DataQuery(source_path="items", limit=200)
        result = execute_data_query(data, query, max_output_records=10)

        assert result.returned_records <= 10
