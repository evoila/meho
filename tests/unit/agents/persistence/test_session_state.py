# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""Tests for OrchestratorSessionState serialization and methods."""

from datetime import datetime

from meho_app.modules.agents.persistence import (
    ConnectorMemory,
    OrchestratorSessionState,
)


class TestConnectorMemory:
    """Test ConnectorMemory dataclass."""

    def test_to_dict_and_from_dict(self):
        """Test roundtrip serialization."""
        mem = ConnectorMemory(
            connector_id="conn-123",
            connector_name="K8s Prod",
            connector_type="kubernetes",
            last_used=datetime(2026, 2, 1, 12, 0, 0),  # noqa: DTZ001 -- naive datetime for test compatibility
            used_endpoints={"/api/v1/pods": "ep-456"},
            last_query="list pods",
            last_status="success",
        )

        data = mem.to_dict()
        restored = ConnectorMemory.from_dict(data)

        assert restored.connector_id == mem.connector_id
        assert restored.connector_name == mem.connector_name
        assert restored.connector_type == mem.connector_type
        assert restored.last_used == mem.last_used
        assert restored.used_endpoints == mem.used_endpoints
        assert restored.last_query == mem.last_query
        assert restored.last_status == mem.last_status

    def test_from_dict_handles_missing_optional_fields(self):
        """Test that from_dict handles missing optional fields."""
        data = {
            "connector_id": "conn-123",
            "connector_name": "K8s Prod",
            "connector_type": "kubernetes",
            "last_used": "2026-02-01T12:00:00",
        }

        mem = ConnectorMemory.from_dict(data)

        assert mem.connector_id == "conn-123"
        assert mem.used_endpoints == {}
        assert mem.last_query is None
        assert mem.last_status == "unknown"


class TestOrchestratorSessionState:
    """Test OrchestratorSessionState."""

    def test_remember_connector_creates_new(self):
        """Test that remember_connector creates a new connector memory."""
        state = OrchestratorSessionState()

        state.remember_connector(
            connector_id="conn-123",
            connector_name="K8s Prod",
            connector_type="kubernetes",
            query="list pods",
            status="success",
        )

        assert "conn-123" in state.connectors
        assert state.primary_connector_id == "conn-123"
        assert state.connectors["conn-123"].last_query == "list pods"
        assert state.connectors["conn-123"].last_status == "success"

    def test_remember_connector_updates_existing(self):
        """Test that remember_connector updates an existing connector."""
        state = OrchestratorSessionState()

        # First call
        state.remember_connector(
            connector_id="conn-123",
            connector_name="K8s Prod",
            connector_type="kubernetes",
            query="list pods",
            status="success",
        )

        first_time = state.connectors["conn-123"].last_used

        # Second call
        state.remember_connector(
            connector_id="conn-123",
            connector_name="K8s Prod",
            connector_type="kubernetes",
            query="list deployments",
            status="success",
        )

        assert len(state.connectors) == 1
        assert state.connectors["conn-123"].last_query == "list deployments"
        assert state.connectors["conn-123"].last_used >= first_time

    def test_remember_connector_failed_does_not_update_primary(self):
        """Test that a failed connector doesn't become primary."""
        state = OrchestratorSessionState()

        # Successful connector
        state.remember_connector("c1", "Conn1", "rest", status="success")
        assert state.primary_connector_id == "c1"

        # Failed connector
        state.remember_connector("c2", "Conn2", "kubernetes", status="failed")
        assert state.primary_connector_id == "c1"  # Still c1

    def test_get_primary_connector_returns_none_when_empty(self):
        """Test that get_primary_connector returns None when no connectors."""
        state = OrchestratorSessionState()
        assert state.get_primary_connector() is None

    def test_get_primary_connector_returns_primary(self):
        """Test that get_primary_connector returns the primary connector."""
        state = OrchestratorSessionState()
        state.remember_connector("c1", "Conn1", "rest", status="success")
        state.remember_connector("c2", "Conn2", "kubernetes", status="success")

        primary = state.get_primary_connector()
        assert primary is not None
        assert primary.connector_id == "c2"  # Last successful

    def test_get_primary_connector_fallback_to_recent_successful(self):
        """Test fallback to most recent successful connector."""
        state = OrchestratorSessionState()

        state.remember_connector("c1", "Conn1", "rest", status="success")
        state.primary_connector_id = None  # Clear primary

        primary = state.get_primary_connector()
        assert primary is not None
        assert primary.connector_id == "c1"

    def test_get_primary_connector_ignores_failed(self):
        """Test that failed connectors are not returned as primary."""
        state = OrchestratorSessionState()

        state.remember_connector("c1", "Conn1", "rest", status="failed")
        state.primary_connector_id = None

        assert state.get_primary_connector() is None

    def test_set_operation_context(self):
        """Test setting operation context."""
        state = OrchestratorSessionState()

        state.set_operation_context("Investigating pod restarts", ["nginx-pod", "api-pod"])

        assert state.current_operation == "Investigating pod restarts"
        assert state.operation_entities == ["nginx-pod", "api-pod"]

    def test_set_operation_context_without_entities(self):
        """Test setting operation context without entities."""
        state = OrchestratorSessionState()

        state.set_operation_context("Checking cluster health")

        assert state.current_operation == "Checking cluster health"
        assert state.operation_entities == []

    def test_register_cached_data(self):
        """Test registering cached data."""
        state = OrchestratorSessionState()

        state.register_cached_data("pods", "c1", 50)

        assert "pods" in state.cached_tables
        assert state.cached_tables["pods"]["connector_id"] == "c1"
        assert state.cached_tables["pods"]["row_count"] == 50
        assert "cached_at" in state.cached_tables["pods"]

    def test_get_available_tables(self):
        """Test getting available tables."""
        state = OrchestratorSessionState()

        state.register_cached_data("pods", "c1", 50)
        state.register_cached_data("nodes", "c1", 10)

        tables = state.get_available_tables()
        assert set(tables) == {"pods", "nodes"}

    def test_get_available_tables_empty(self):
        """Test getting available tables when empty."""
        state = OrchestratorSessionState()
        assert state.get_available_tables() == []

    def test_record_error(self):
        """Test recording an error."""
        state = OrchestratorSessionState()

        state.record_error("c1", "timeout", "Request timed out")

        assert len(state.recent_errors) == 1
        assert state.recent_errors[0]["connector_id"] == "c1"
        assert state.recent_errors[0]["error_type"] == "timeout"
        assert state.recent_errors[0]["message"] == "Request timed out"
        assert "timestamp" in state.recent_errors[0]

    def test_record_error_keeps_last_10(self):
        """Test that record_error keeps only the last 10 errors."""
        state = OrchestratorSessionState()

        for i in range(15):
            state.record_error("c1", "error", f"Error {i}")

        assert len(state.recent_errors) == 10
        # Should have errors 5-14
        assert state.recent_errors[0]["message"] == "Error 5"
        assert state.recent_errors[9]["message"] == "Error 14"

    def test_has_recent_error_true(self):
        """Test has_recent_error returns True when error exists."""
        state = OrchestratorSessionState()

        state.record_error("c1", "timeout", "Request timed out")

        assert state.has_recent_error("c1", "timeout") is True

    def test_has_recent_error_false_different_connector(self):
        """Test has_recent_error returns False for different connector."""
        state = OrchestratorSessionState()

        state.record_error("c1", "timeout", "Request timed out")

        assert state.has_recent_error("c2", "timeout") is False

    def test_has_recent_error_false_different_type(self):
        """Test has_recent_error returns False for different error type."""
        state = OrchestratorSessionState()

        state.record_error("c1", "timeout", "Request timed out")

        assert state.has_recent_error("c1", "auth_error") is False

    def test_has_recent_error_false_empty(self):
        """Test has_recent_error returns False when no errors."""
        state = OrchestratorSessionState()
        assert state.has_recent_error("c1", "timeout") is False

    def test_serialization_roundtrip(self):
        """Test full state serialization roundtrip."""
        state = OrchestratorSessionState()
        state.remember_connector("c1", "K8s", "kubernetes", "list pods", "success")
        state.set_operation_context("Investigating pod restarts", ["nginx-pod", "api-pod"])
        state.register_cached_data("pods", "c1", 50)
        state.record_error("c2", "timeout", "Request timed out")
        state.turn_count = 5

        data = state.to_dict()
        restored = OrchestratorSessionState.from_dict(data)

        assert len(restored.connectors) == 1
        assert "c1" in restored.connectors
        assert restored.connectors["c1"].connector_name == "K8s"
        assert restored.primary_connector_id == "c1"
        assert restored.current_operation == "Investigating pod restarts"
        assert restored.operation_entities == ["nginx-pod", "api-pod"]
        assert "pods" in restored.cached_tables
        assert len(restored.recent_errors) == 1
        assert restored.turn_count == 5

    def test_from_dict_handles_empty_data(self):
        """Test that from_dict handles empty data."""
        data: dict = {}
        state = OrchestratorSessionState.from_dict(data)

        assert state.connectors == {}
        assert state.primary_connector_id is None
        assert state.current_operation is None
        assert state.operation_entities == []
        assert state.cached_tables == {}
        assert state.recent_errors == []
        assert state.turn_count == 0

    def test_context_summary_empty(self):
        """Test context summary for empty state."""
        state = OrchestratorSessionState()
        assert state.get_context_summary() == "New conversation"

    def test_context_summary_with_connectors(self):
        """Test context summary with connectors."""
        state = OrchestratorSessionState()
        state.remember_connector("c1", "K8s Prod", "kubernetes", status="success")

        summary = state.get_context_summary()
        assert "K8s Prod" in summary

    def test_context_summary_with_operation(self):
        """Test context summary with operation."""
        state = OrchestratorSessionState()
        state.set_operation_context("Debug pod crashes")

        summary = state.get_context_summary()
        assert "Debug pod crashes" in summary

    def test_context_summary_with_entities(self):
        """Test context summary with entities."""
        state = OrchestratorSessionState()
        state.set_operation_context("Debug", ["nginx-pod", "api-pod"])

        summary = state.get_context_summary()
        assert "nginx-pod" in summary
        assert "api-pod" in summary

    def test_context_summary_with_cached_tables(self):
        """Test context summary with cached tables."""
        state = OrchestratorSessionState()
        state.register_cached_data("pods", "c1", 50)

        summary = state.get_context_summary()
        assert "pods" in summary

    def test_context_summary_combined(self):
        """Test context summary with all components."""
        state = OrchestratorSessionState()
        state.remember_connector("c1", "K8s Prod", "kubernetes", status="success")
        state.set_operation_context("Debug pod crashes", ["nginx-pod"])
        state.register_cached_data("pods", "c1", 50)

        summary = state.get_context_summary()
        assert "K8s Prod" in summary
        assert "Debug pod crashes" in summary
        assert "nginx-pod" in summary
        assert "pods" in summary
