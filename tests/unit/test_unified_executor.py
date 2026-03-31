# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Tests for the Unified Executor.

Tests the orchestration layer that integrates data reduction
with the agent execution flow.
"""

from unittest.mock import patch

import pytest

from meho_app.modules.agents.data_reduction.query_generator import QueryGeneratorOutput
from meho_app.modules.agents.data_reduction.query_schema import DataQuery
from meho_app.modules.agents.unified_executor import (
    ExecutionResult,
    ResponseAnalysis,
    UnifiedExecutor,
    analyze_response,
    process_api_response_for_llm,
    should_reduce_response,
)

# =============================================================================
# Response Analysis Tests
# =============================================================================


class TestResponseAnalysis:
    """Tests for response analysis."""

    def test_analyze_list_response(self):
        """Test analyzing a list response."""
        data = [{"id": i, "name": f"item-{i}"} for i in range(100)]

        analysis = analyze_response(data)

        assert analysis.total_records == 100
        assert analysis.source_path == ""
        assert "id" in analysis.detected_fields
        assert analysis.needs_reduction

    def test_analyze_nested_response(self):
        """Test analyzing a response with nested array."""
        data = {
            "clusters": [
                {"name": "cluster-1", "status": "healthy"},
                {"name": "cluster-2", "status": "warning"},
            ],
            "metadata": {"total": 2},
        }

        analysis = analyze_response(data)

        assert analysis.total_records == 2
        assert analysis.source_path == "clusters"
        assert "name" in analysis.detected_fields

    def test_analyze_small_response(self):
        """Test that small responses don't need reduction."""
        data = {"items": [{"id": i} for i in range(10)]}

        analysis = analyze_response(data)

        assert analysis.total_records == 10
        assert not analysis.needs_reduction

    def test_analyze_large_response(self):
        """Test that large responses need reduction."""
        data = {"items": [{"id": i, "data": "x" * 1000} for i in range(100)]}

        analysis = analyze_response(data)

        assert analysis.needs_reduction
        assert "large" in analysis.reason.lower()

    def test_analyze_empty_response(self):
        """Test analyzing empty response."""
        analysis = analyze_response({})

        assert analysis.total_records == 0
        assert not analysis.needs_reduction

    def test_is_large_by_records(self):
        """Test is_large property by record count."""
        analysis = ResponseAnalysis(total_records=150)
        assert analysis.is_large

        analysis = ResponseAnalysis(total_records=50)
        assert not analysis.is_large

    def test_is_large_by_size(self):
        """Test is_large property by size."""
        analysis = ResponseAnalysis(estimated_size_bytes=200 * 1024)
        assert analysis.is_large

        analysis = ResponseAnalysis(estimated_size_bytes=50 * 1024)
        assert not analysis.is_large


class TestShouldReduceResponse:
    """Tests for should_reduce_response helper."""

    def test_should_reduce_large_list(self):
        """Test detection of large list."""
        data = [{"id": i} for i in range(100)]
        assert should_reduce_response(data)

    def test_should_not_reduce_small_response(self):
        """Test small responses don't need reduction."""
        data = {"items": [{"id": 1}, {"id": 2}]}
        assert not should_reduce_response(data)


# =============================================================================
# Unified Executor Tests
# =============================================================================


class TestUnifiedExecutor:
    """Tests for UnifiedExecutor."""

    @pytest.fixture
    def executor(self):
        """Create executor with low thresholds for testing."""
        return UnifiedExecutor(
            auto_reduce_threshold=10,
            auto_reduce_size_kb=10,
        )

    @pytest.fixture
    def large_cluster_data(self):
        """Large cluster dataset for testing."""
        return {
            "clusters": [
                {
                    "name": f"cluster-{i:03d}",
                    "region": ["us-east", "us-west", "eu-west"][i % 3],
                    "memory_total_gb": 512,
                    "memory_used_gb": 400 + (i % 100),
                    "status": ["healthy", "warning", "critical"][i % 3],
                }
                for i in range(100)
            ]
        }

    @pytest.mark.asyncio
    async def test_process_small_response(self, executor):
        """Test that small responses pass through unchanged."""
        data = {"items": [{"id": 1, "name": "test"}]}

        result = await executor.process_response(
            question="What items do I have?",
            api_response=data,
        )

        assert not result.was_reduced
        assert result.raw_data == data
        assert "API Response" in result.llm_context

    @pytest.mark.asyncio
    async def test_process_large_response_reduces(self, executor, large_cluster_data):
        """Test that large responses are reduced."""
        # Mock the query generator
        mock_query = DataQuery(
            source_path="clusters",
            filter=None,
            limit=20,
        )
        mock_output = QueryGeneratorOutput(
            query=mock_query,
            reasoning="Returning all clusters with limit",
            confidence=0.8,
        )

        with patch(
            "meho_app.modules.agents.unified_executor.generate_query", return_value=mock_output
        ):
            result = await executor.process_response(
                question="Show me all clusters",
                api_response=large_cluster_data,
            )

        assert result.was_reduced
        assert result.reduced_data.total_source_records == 100
        assert result.reduced_data.returned_records <= 20
        assert "Query Results" in result.llm_context

    @pytest.mark.asyncio
    async def test_process_with_filtering_question(self, executor, large_cluster_data):
        """Test processing with a filtering question."""
        # Mock query generator to return a filtering query
        mock_query = DataQuery(
            source_path="clusters",
            limit=20,
        )
        mock_output = QueryGeneratorOutput(
            query=mock_query,
            reasoning="Filtering for critical clusters",
            confidence=0.9,
        )

        with patch(
            "meho_app.modules.agents.unified_executor.generate_query", return_value=mock_output
        ):
            result = await executor.process_response(
                question="Show me critical clusters",
                api_response=large_cluster_data,
            )

        assert result.was_reduced
        assert "Query Results" in result.llm_context
        assert "critical" in result.llm_context.lower() or "clusters" in result.llm_context.lower()

    @pytest.mark.asyncio
    async def test_force_reduction(self, executor):
        """Test forcing reduction on small responses."""
        data = {"items": [{"id": 1}, {"id": 2}]}

        mock_query = DataQuery(source_path="items", limit=10)
        mock_output = QueryGeneratorOutput(
            query=mock_query,
            reasoning="test",
            confidence=0.8,
        )

        with patch(
            "meho_app.modules.agents.unified_executor.generate_query", return_value=mock_output
        ):
            result = await executor.process_response(
                question="Show items",
                api_response=data,
                force_reduction=True,
            )

        assert result.was_reduced

    @pytest.mark.asyncio
    async def test_query_generation_failure_fallback(self, executor, large_cluster_data):
        """Test fallback when query generation fails."""
        with patch(
            "meho_app.modules.agents.unified_executor.generate_query",
            side_effect=Exception("LLM error"),
        ):
            result = await executor.process_response(
                question="Show me clusters",
                api_response=large_cluster_data,
            )

        # Should still work with default query
        assert result.was_reduced
        assert result.reduced_data is not None

    @pytest.mark.asyncio
    async def test_endpoint_info_passed(self, executor, large_cluster_data):
        """Test that endpoint info is passed to query generator."""
        mock_query = DataQuery(source_path="clusters", limit=20)
        mock_output = QueryGeneratorOutput(
            query=mock_query,
            reasoning="test",
            confidence=0.8,
        )

        with patch(
            "meho_app.modules.agents.unified_executor.generate_query", return_value=mock_output
        ) as mock_gen:
            await executor.process_response(
                question="Show clusters",
                api_response=large_cluster_data,
                endpoint_info={"path": "/api/v1/clusters", "method": "GET"},
            )

            # Check endpoint path was passed
            mock_gen.assert_called_once()
            assert mock_gen.call_args.kwargs.get("endpoint_path") == "/api/v1/clusters"


class TestExecutionResult:
    """Tests for ExecutionResult."""

    def test_was_reduced_true(self):
        """Test was_reduced when data was reduced."""
        from meho_app.modules.agents.data_reduction.query_schema import DataQuery, ReducedData

        reduced = ReducedData(
            records=[{"id": 1}],
            total_source_records=100,
            total_after_filter=10,
            returned_records=1,
            aggregates={},
            query_applied=DataQuery(source_path="test"),
            processing_time_ms=5,
        )

        result = ExecutionResult(
            reduced_data=reduced,
            raw_data={"test": []},
            analysis=ResponseAnalysis(),
            query_generated=None,
            llm_context="test",
        )

        assert result.was_reduced
        assert result.record_count == 1

    def test_was_reduced_false(self):
        """Test was_reduced when data was not reduced."""
        result = ExecutionResult(
            reduced_data=None,
            raw_data=[{"id": 1}, {"id": 2}],
            analysis=ResponseAnalysis(),
            query_generated=None,
            llm_context="test",
        )

        assert not result.was_reduced
        assert result.record_count == 2


# =============================================================================
# Integration Helper Tests
# =============================================================================


class TestProcessApiResponseForLlm:
    """Tests for the convenience function."""

    @pytest.mark.asyncio
    async def test_basic_usage(self):
        """Test basic usage of the convenience function."""
        data = {"items": [{"id": i} for i in range(5)]}

        result = await process_api_response_for_llm(
            question="What items are there?",
            api_response=data,
        )

        assert isinstance(result, str)
        assert len(result) > 0

    @pytest.mark.asyncio
    async def test_with_endpoint_path(self):
        """Test with endpoint path provided."""

        mock_query = DataQuery(source_path="clusters", limit=10)
        mock_output = QueryGeneratorOutput(
            query=mock_query,
            reasoning="test",
            confidence=0.8,
        )

        with patch(
            "meho_app.modules.agents.unified_executor.generate_query", return_value=mock_output
        ):
            result = await process_api_response_for_llm(
                question="Show clusters",
                api_response={"clusters": [{"id": i} for i in range(100)]},
                endpoint_path="/api/clusters",
            )

        assert "Query Results" in result
