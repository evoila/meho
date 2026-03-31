# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Unit tests for cache data structures (TASK-161).

Tests the ResponseTier enum, CachedData dataclass, and
extended OperationDefinition with response schema fields.
"""

from datetime import datetime

import pyarrow as pa
import pytest

from meho_app.modules.agents.execution.cache import (
    CachedData,
    CachedResponse,
    CachedTable,
    ResponseTier,
    SchemaSummary,
)
from meho_app.modules.connectors.base import OperationDefinition

# Sample data used across tests
_SAMPLE_ROWS = [
    {"name": "default", "uid": "abc123", "phase": "Active"},
    {"name": "kube-system", "uid": "def456", "phase": "Active"},
    {"name": "kube-public", "uid": "ghi789", "phase": "Active"},
    {"name": "meho", "uid": "jkl012", "phase": "Active"},
    {"name": "monitoring", "uid": "mno345", "phase": "Active"},
    {"name": "production", "uid": "pqr678", "phase": "Active"},
]


class TestResponseTier:
    """Tests for ResponseTier enum."""

    def test_response_tier_values(self):
        """ResponseTier has all expected values."""
        assert ResponseTier.INLINE.value == "inline"
        assert ResponseTier.CACHED.value == "cached"

    def test_response_tier_count(self):
        """ResponseTier has exactly 2 values."""
        assert len(ResponseTier) == 2

    def test_response_tier_from_string(self):
        """Can create ResponseTier from string value."""
        assert ResponseTier("inline") == ResponseTier.INLINE
        assert ResponseTier("cached") == ResponseTier.CACHED


class TestCachedData:
    """Tests for CachedData dataclass."""

    @pytest.fixture
    def sample_arrow(self):
        """Create a sample Arrow table for testing."""
        return pa.table(
            {
                "name": [r["name"] for r in _SAMPLE_ROWS],
                "uid": [r["uid"] for r in _SAMPLE_ROWS],
                "phase": [r["phase"] for r in _SAMPLE_ROWS],
            }
        )

    @pytest.fixture
    def cached_data(self, sample_arrow):
        """Create a CachedData instance for testing."""
        cached = CachedData(
            cache_key="session1:connector1:list_namespaces",
            session_id="session1",
            table_name="namespaces",
            source_id="list_namespaces",
            source_path="list_namespaces",
            connector_id="connector1",
            connector_type="kubernetes",
            entity_type="Namespace",
            identifier_field="uid",
            display_name_field="name",
            columns=sample_arrow.column_names,
            row_count=sample_arrow.num_rows,
            estimated_tokens=500,
        )
        cached._df = sample_arrow
        return cached

    def test_cached_data_creation(self, cached_data):
        """CachedData can be created with all fields."""
        assert cached_data.cache_key == "session1:connector1:list_namespaces"
        assert cached_data.table_name == "namespaces"
        assert cached_data.connector_type == "kubernetes"
        assert cached_data.entity_type == "Namespace"
        assert cached_data.identifier_field == "uid"
        assert cached_data.display_name_field == "name"
        assert cached_data.row_count == 6
        assert cached_data.estimated_tokens == 500

    def test_cached_data_arrow_table_property(self, cached_data, sample_arrow):
        """arrow_table property returns the internal Arrow table."""
        assert cached_data.arrow_table.equals(sample_arrow)

    def test_cached_data_arrow_not_loaded_raises(self):
        """Accessing arrow_table when not loaded raises ValueError."""
        cached = CachedData(
            cache_key="test",
            session_id="test",
            table_name="test",
            source_id="test",
            source_path="test",
            connector_id="test",
            connector_type="test",
        )
        with pytest.raises(ValueError, match="Arrow table not loaded"):
            _ = cached.arrow_table

    def test_to_llm_summary_inline(self, cached_data, sample_arrow):
        """INLINE tier returns full data with data_available: true."""
        summary = cached_data.to_llm_summary(ResponseTier.INLINE)

        assert summary["success"] is True
        assert summary["data_available"] is True  # LLM has the data
        assert summary["cached"] is False
        assert summary["table"] == "namespaces"
        assert summary["count"] == 6
        assert sorted(summary["columns"]) == sorted(sample_arrow.column_names)
        assert summary["schema"]["entity_type"] == "Namespace"
        assert summary["schema"]["identifier"] == "uid"
        assert summary["schema"]["display_name"] == "name"
        assert len(summary["data"]) == 6
        assert "Retrieved 6 Namespace" in summary["message"]
        # No action_required for INLINE since LLM has all data
        assert "action_required" not in summary

    def test_to_llm_summary_cached(self, cached_data):
        """CACHED tier returns metadata only with action_required signal."""
        summary = cached_data.to_llm_summary(ResponseTier.CACHED)

        assert summary["success"] is True
        assert summary["cached"] is True
        assert summary["table"] == "namespaces"
        assert summary["row_count"] == 6
        assert summary["columns"] == ["name", "uid", "phase"]
        # Critical action signals for LLM
        assert summary["data_available"] is False
        assert summary["action_required"] == "reduce_data"
        assert "next_step" in summary
        assert summary["next_step"]["tool"] == "reduce_data"
        assert "SELECT name FROM namespaces" in summary["next_step"]["example_sql"]
        # No data in CACHED tier - forces LLM to use SQL
        assert "data" not in summary
        # Message makes it clear LLM doesn't have data
        assert "Data cached but NOT returned" in summary["message"]
        assert "MUST call reduce_data" in summary["message"]

    def test_to_llm_summary_without_entity_type(self, sample_arrow):
        """LLM summary uses 'items' when entity_type is None."""
        cached = CachedData(
            cache_key="test",
            session_id="test",
            table_name="data",
            source_id="test",
            source_path="test",
            connector_id="test",
            connector_type="rest",
            columns=sample_arrow.column_names,
            row_count=sample_arrow.num_rows,
        )
        cached._df = sample_arrow

        summary = cached.to_llm_summary(ResponseTier.INLINE)
        assert "Retrieved 6 items" in summary["message"]

    def test_to_summary_backwards_compatible(self, cached_data):
        """to_summary() returns dict compatible with CachedTable.to_summary()."""
        summary = cached_data.to_summary()

        assert "table" in summary
        assert "connector_id" in summary
        assert "columns" in summary
        assert "row_count" in summary
        assert "cached_at" in summary
        # New fields
        assert "entity_type" in summary
        assert "identifier_field" in summary
        assert "display_name_field" in summary
        assert "connector_type" in summary

    def test_cached_data_default_values(self):
        """CachedData has sensible defaults for optional fields."""
        cached = CachedData(
            cache_key="test",
            session_id="test",
            table_name="test",
            source_id="test",
            source_path="test",
            connector_id="test",
            connector_type="rest",
        )

        assert cached.entity_type is None
        assert cached.identifier_field is None
        assert cached.display_name_field is None
        assert cached.columns == []
        assert cached.row_count == 0
        assert cached.estimated_tokens == 0
        assert isinstance(cached.cached_at, datetime)


class TestOperationDefinitionExtension:
    """Tests for extended OperationDefinition with response schema fields."""

    def test_operation_definition_backwards_compatible(self):
        """Existing OperationDefinition usage still works without new fields."""
        op = OperationDefinition(
            operation_id="list_pods",
            name="List Pods",
            description="List all pods in a namespace",
            category="core",
        )

        assert op.operation_id == "list_pods"
        assert op.name == "List Pods"
        assert op.description == "List all pods in a namespace"
        assert op.category == "core"
        assert op.parameters == []
        assert op.example is None
        # New fields default to None
        assert op.response_entity_type is None
        assert op.response_identifier_field is None
        assert op.response_display_name_field is None

    def test_operation_definition_with_response_schema(self):
        """OperationDefinition can include response schema fields."""
        op = OperationDefinition(
            operation_id="list_namespaces",
            name="List Namespaces",
            description="List all namespaces in the cluster",
            category="core",
            parameters=[],
            example="list_namespaces()",
            response_entity_type="Namespace",
            response_identifier_field="uid",
            response_display_name_field="name",
        )

        assert op.response_entity_type == "Namespace"
        assert op.response_identifier_field == "uid"
        assert op.response_display_name_field == "name"

    def test_operation_definition_partial_response_schema(self):
        """OperationDefinition works with only some response schema fields."""
        op = OperationDefinition(
            operation_id="get_cluster_info",
            name="Get Cluster Info",
            description="Get cluster information",
            category="cluster",
            response_entity_type="Cluster",
            # Only entity_type, no identifier or display_name
        )

        assert op.response_entity_type == "Cluster"
        assert op.response_identifier_field is None
        assert op.response_display_name_field is None

    def test_operation_definition_serialization(self):
        """OperationDefinition serializes correctly with new fields."""
        op = OperationDefinition(
            operation_id="list_vms",
            name="List VMs",
            description="List virtual machines",
            category="compute",
            response_entity_type="VirtualMachine",
            response_identifier_field="moref_id",
            response_display_name_field="name",
        )

        data = op.model_dump()
        assert data["response_entity_type"] == "VirtualMachine"
        assert data["response_identifier_field"] == "moref_id"
        assert data["response_display_name_field"] == "name"

    def test_operation_definition_from_dict(self):
        """OperationDefinition can be created from dict with new fields."""
        data = {
            "operation_id": "list_pvcs",
            "name": "List PVCs",
            "description": "List persistent volume claims",
            "category": "storage",
            "response_entity_type": "PersistentVolumeClaim",
            "response_identifier_field": "uid",
            "response_display_name_field": "name",
        }

        op = OperationDefinition(**data)
        assert op.response_entity_type == "PersistentVolumeClaim"


class TestExistingCacheStructures:
    """Verify existing cache structures are unchanged."""

    def test_cached_table_unchanged(self):
        """CachedTable still works as before."""
        table = CachedTable(
            table_name="test",
            operation_id="test_op",
            connector_id="conn1",
            columns=["a", "b"],
            row_count=10,
        )

        summary = table.to_summary()
        assert "table" in summary
        assert "operation" in summary
        assert "columns" in summary
        assert "row_count" in summary

    def test_cached_response_unchanged(self):
        """CachedResponse still works as before."""
        schema = SchemaSummary(
            identifier_field="id",
            display_name_field="name",
            entity_type="Resource",
        )

        response = CachedResponse(
            cache_key="test",
            session_id="test",
            endpoint_id="ep1",
            endpoint_path="/api/resources",
            connector_id="conn1",
            schema_summary=schema,
            response_schema={},
            data=[{"id": "1", "name": "test"}],
            count=1,
        )

        summary = response.summarize_for_brain()
        assert "cache_key" in summary
        assert "schema" in summary
        assert summary["schema"]["identifier"] == "id"
