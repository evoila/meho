# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Tests for the Query Generator.

These tests validate the query generation system that translates
natural language questions into DataQuery specifications.
"""

from unittest.mock import AsyncMock, patch

import pytest

from meho_app.modules.agents.data_reduction.query_generator import (
    QueryGeneratorContext,
    QueryGeneratorOutput,
    validate_query_against_schema,
)
from meho_app.modules.agents.data_reduction.query_schema import (
    AggregateFunction,
    AggregateSpec,
    ComputeField,
    DataQuery,
    FilterCondition,
    FilterGroup,
    FilterOperator,
    SortSpec,
)

# =============================================================================
# Test Fixtures
# =============================================================================


@pytest.fixture
def cluster_schema():
    """Schema for cluster API response."""
    return {
        "clusters": [
            {
                "name": "string",
                "region": "string",
                "cpu_cores": "integer",
                "cpu_used": "integer",
                "memory_total_gb": "integer",
                "memory_used_gb": "integer",
                "status": "string",
            }
        ]
    }


@pytest.fixture
def pod_schema():
    """Schema for Kubernetes pod response."""
    return {
        "items": [
            {
                "metadata": {
                    "name": "string",
                    "namespace": "string",
                },
                "status": {
                    "phase": "string",
                },
            }
        ]
    }


# =============================================================================
# Context Tests
# =============================================================================


class TestQueryGeneratorContext:
    """Tests for QueryGeneratorContext."""

    def test_context_creation(self, cluster_schema):
        """Test creating a context."""
        ctx = QueryGeneratorContext(
            question="Show me high memory clusters",
            response_schema=cluster_schema,
            endpoint_path="/api/v1/clusters",
            max_records=50,
        )

        assert ctx.question == "Show me high memory clusters"
        assert ctx.endpoint_path == "/api/v1/clusters"
        assert ctx.max_records == 50

    def test_context_with_sample_data(self, cluster_schema):
        """Test context with sample data."""
        sample = {"clusters": [{"name": "prod-01", "memory_used_gb": 400, "memory_total_gb": 512}]}

        ctx = QueryGeneratorContext(
            question="Show me high memory clusters",
            response_schema=cluster_schema,
            sample_data=sample,
        )

        assert ctx.sample_data is not None
        assert "clusters" in ctx.sample_data


# =============================================================================
# Output Model Tests
# =============================================================================


class TestQueryGeneratorOutput:
    """Tests for QueryGeneratorOutput model."""

    def test_output_creation(self):
        """Test creating an output."""
        query = DataQuery(
            source_path="clusters",
            filter=FilterGroup(
                conditions=[
                    FilterCondition(field="status", operator=FilterOperator.EQ, value="healthy")
                ]
            ),
        )

        output = QueryGeneratorOutput(
            query=query,
            reasoning="Filtering for healthy clusters as requested",
            confidence=0.9,
        )

        assert output.query.source_path == "clusters"
        assert output.confidence == 0.9

    def test_confidence_bounds(self):
        """Test confidence must be between 0 and 1."""
        query = DataQuery(source_path="test")

        with pytest.raises(ValueError):  # noqa: PT011 -- test validates exception type is sufficient
            QueryGeneratorOutput(
                query=query,
                reasoning="test",
                confidence=1.5,  # Invalid
            )

        with pytest.raises(ValueError):  # noqa: PT011 -- test validates exception type is sufficient
            QueryGeneratorOutput(
                query=query,
                reasoning="test",
                confidence=-0.1,  # Invalid
            )


# =============================================================================
# Query Validation Tests
# =============================================================================


class TestQueryValidation:
    """Tests for query validation against schema."""

    def test_validate_valid_query(self, cluster_schema):
        """Test validating a query with valid fields."""
        query = DataQuery(
            source_path="clusters",
            select=["name", "status", "memory_total_gb"],
            filter=FilterGroup(
                conditions=[
                    FilterCondition(field="status", operator=FilterOperator.EQ, value="healthy")
                ]
            ),
        )

        warnings = validate_query_against_schema(query, cluster_schema["clusters"][0])

        # Should have no warnings for valid fields
        assert len(warnings) == 0

    def test_validate_invalid_select_field(self, cluster_schema):
        """Test validation catches invalid select fields."""
        query = DataQuery(
            source_path="clusters",
            select=["name", "nonexistent_field"],
        )

        warnings = validate_query_against_schema(query, cluster_schema["clusters"][0])

        # Should warn about nonexistent field
        assert any("nonexistent_field" in w for w in warnings)

    def test_validate_invalid_filter_field(self, cluster_schema):
        """Test validation catches invalid filter fields."""
        query = DataQuery(
            source_path="clusters",
            filter=FilterGroup(
                conditions=[
                    FilterCondition(field="invalid_field", operator=FilterOperator.EQ, value="x")
                ]
            ),
        )

        warnings = validate_query_against_schema(query, cluster_schema["clusters"][0])

        assert any("invalid_field" in w for w in warnings)

    def test_validate_nested_fields(self, pod_schema):
        """Test validation handles nested fields."""
        query = DataQuery(
            source_path="items",
            select=["metadata.name", "status.phase"],
        )

        schema = pod_schema["items"][0]
        warnings = validate_query_against_schema(query, schema)

        # metadata and status are valid base fields
        assert len(warnings) == 0


# =============================================================================
# Generated Query Structure Tests
# =============================================================================


class TestGeneratedQueryStructure:
    """Tests for expected query structures based on question types."""

    def test_high_memory_query_structure(self):
        """Test expected structure for 'high memory' type questions."""
        # This is what we expect the LLM to generate for such questions
        expected_query = DataQuery(
            source_path="clusters",
            select=["name", "region", "memory_used_gb", "memory_total_gb"],
            compute=[
                ComputeField(name="memory_pct", expression="memory_used_gb / memory_total_gb * 100")
            ],
            filter=FilterGroup(
                conditions=[
                    FilterCondition(field="memory_pct", operator=FilterOperator.GT, value=80)
                ]
            ),
            sort=SortSpec(field="memory_pct", direction="desc"),
            limit=20,
            aggregates=[
                AggregateSpec(
                    name="avg_memory_pct", function=AggregateFunction.AVG, field="memory_pct"
                ),
                AggregateSpec(name="count", function=AggregateFunction.COUNT, field="*"),
            ],
        )

        # Verify the structure is valid
        assert expected_query.source_path == "clusters"
        assert len(expected_query.compute) == 1
        assert expected_query.filter is not None
        assert len(expected_query.aggregates) == 2

    def test_count_by_category_query_structure(self):
        """Test expected structure for 'count by X' type questions."""
        expected_query = DataQuery(
            source_path="items",
            select=["metadata.namespace"],
            group_by=["metadata.namespace"],
            aggregates=[
                AggregateSpec(name="count", function=AggregateFunction.COUNT, field="*"),
            ],
        )

        assert expected_query.group_by == ["metadata.namespace"]
        assert len(expected_query.aggregates) == 1

    def test_filter_by_status_query_structure(self):
        """Test expected structure for 'show X with status Y' questions."""
        expected_query = DataQuery(
            source_path="deployments",
            filter=FilterGroup(
                conditions=[
                    FilterCondition(field="status", operator=FilterOperator.EQ, value="Failed")
                ]
            ),
            sort=SortSpec(field="name", direction="asc"),
            limit=50,
        )

        assert expected_query.filter is not None
        assert expected_query.filter.conditions[0].value == "Failed"


# =============================================================================
# Integration-Style Tests (Mocked LLM)
# =============================================================================


class TestQueryGeneratorIntegration:
    """Integration tests with mocked LLM responses."""

    @pytest.mark.asyncio
    async def test_generate_query_mock(self, cluster_schema):
        """Test generate_query with mocked LLM response."""
        expected_query = DataQuery(
            source_path="clusters",
            filter=FilterGroup(
                conditions=[
                    FilterCondition(field="status", operator=FilterOperator.EQ, value="critical")
                ]
            ),
            limit=20,
        )

        expected_output = QueryGeneratorOutput(
            query=expected_query,
            reasoning="Filtering for critical status clusters",
            confidence=0.85,
        )

        # Mock the agent's run method
        mock_agent = AsyncMock()
        mock_result = AsyncMock()
        mock_result.output = expected_output
        mock_agent.run.return_value = mock_result

        with patch(
            "meho_app.modules.agents.data_reduction.query_generator.get_query_generator_agent",
            return_value=mock_agent,
        ):
            from meho_app.modules.agents.data_reduction.query_generator import generate_query

            result = await generate_query(
                question="Show me clusters with critical status",
                response_schema=cluster_schema,
            )

            assert result.query.source_path == "clusters"
            assert result.confidence > 0.8


# =============================================================================
# Edge Cases
# =============================================================================


class TestQueryGeneratorEdgeCases:
    """Tests for edge cases in query generation."""

    def test_empty_schema(self):
        """Test handling empty schema."""
        ctx = QueryGeneratorContext(
            question="Show me data",
            response_schema={},
        )

        assert ctx.response_schema == {}

    def test_deeply_nested_schema(self):
        """Test handling deeply nested schema."""
        schema = {"data": {"clusters": {"items": [{"metadata": {"labels": {"app": "string"}}}]}}}

        ctx = QueryGeneratorContext(
            question="Find items by app label",
            response_schema=schema,
        )

        # Should handle deep nesting
        assert "data" in ctx.response_schema
