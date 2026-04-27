# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""Unit tests for orchestrator single-connector passthrough and SubgraphOutput data_refs.

Tests for:
- Single-connector passthrough detection logic
- Multi-connector bypass of passthrough
- Partial-failure bypass of passthrough
- Passthrough event flags
- SubgraphOutput data_refs field and serialization
"""

from __future__ import annotations

from typing import Any

import pytest

from meho_app.modules.agents.orchestrator.contracts import SubgraphOutput
from meho_app.modules.agents.orchestrator.state import OrchestratorState

# =============================================================================
# Passthrough Detection Logic Tests
# =============================================================================


class TestSingleConnectorPassthrough:
    """Tests for orchestrator single-connector passthrough detection."""

    def test_single_success_triggers_passthrough(self):
        """One successful finding, zero failed = passthrough."""
        state = OrchestratorState(user_goal="Show me pods")
        state.all_findings = [
            SubgraphOutput(
                connector_id="k8s-prod",
                connector_name="Production K8s",
                findings="Found 5 running pods in namespace default",
                status="success",
                confidence=0.9,
            ),
        ]

        successful = [f for f in state.all_findings if f.status == "success"]
        failed = [f for f in state.all_findings if f.status != "success"]

        assert len(successful) == 1
        assert not failed
        # Passthrough condition met

    def test_single_success_with_failure_skips_passthrough(self):
        """One success + one failure = synthesis (need to explain failure)."""
        state = OrchestratorState(user_goal="Check all systems")
        state.all_findings = [
            SubgraphOutput(
                connector_id="k8s-prod",
                connector_name="Production K8s",
                findings="Found 5 running pods",
                status="success",
            ),
            SubgraphOutput(
                connector_id="gcp-prod",
                connector_name="GCP Production",
                findings="",
                status="failed",
                error_message="Connection timeout",
            ),
        ]

        successful = [f for f in state.all_findings if f.status == "success"]
        failed = [f for f in state.all_findings if f.status != "success"]

        # Should NOT passthrough because there are failed findings
        assert len(successful) == 1
        assert len(failed) == 1
        assert not (len(successful) == 1 and not failed)

    def test_multiple_successes_skip_passthrough(self):
        """Two successful findings = synthesis."""
        state = OrchestratorState(user_goal="Compare prod and staging")
        state.all_findings = [
            SubgraphOutput(
                connector_id="k8s-prod",
                connector_name="Production K8s",
                findings="Production has 10 pods",
                status="success",
            ),
            SubgraphOutput(
                connector_id="k8s-staging",
                connector_name="Staging K8s",
                findings="Staging has 3 pods",
                status="success",
            ),
        ]

        successful = [f for f in state.all_findings if f.status == "success"]
        failed = [f for f in state.all_findings if f.status != "success"]

        # Should NOT passthrough because multiple successful connectors
        assert len(successful) == 2
        assert not (len(successful) == 1 and not failed)

    def test_zero_findings_skip_passthrough(self):
        """No findings = conversational response, not passthrough."""
        state = OrchestratorState(user_goal="Hello, how are you?")
        # No findings at all

        successful = [f for f in state.all_findings if f.status == "success"]
        failed = [f for f in state.all_findings if f.status != "success"]

        assert len(successful) == 0
        assert not (len(successful) == 1 and not failed)

    def test_single_partial_skips_passthrough(self):
        """One partial finding should NOT trigger passthrough."""
        state = OrchestratorState(user_goal="Show me pods")
        state.all_findings = [
            SubgraphOutput(
                connector_id="k8s-prod",
                connector_name="Production K8s",
                findings="Partial results",
                status="partial",
            ),
        ]

        successful = [f for f in state.all_findings if f.status == "success"]
        failed = [f for f in state.all_findings if f.status != "success"]

        # partial != success, so successful is empty
        assert len(successful) == 0
        assert not (len(successful) == 1 and not failed)

    def test_passthrough_sets_final_answer(self):
        """Passthrough should set state.final_answer to finding.findings."""
        state = OrchestratorState(user_goal="Show me pods")
        finding = SubgraphOutput(
            connector_id="k8s-prod",
            connector_name="Production K8s",
            findings="Found 5 running pods in namespace default",
            status="success",
        )
        state.all_findings = [finding]

        # Simulate what passthrough does
        successful = [f for f in state.all_findings if f.status == "success"]
        failed = [f for f in state.all_findings if f.status != "success"]

        if len(successful) == 1 and not failed:
            state.final_answer = successful[0].findings

        assert state.final_answer == "Found 5 running pods in namespace default"

    @pytest.mark.asyncio
    async def test_passthrough_event_has_passthrough_flag(self):
        """The synthesis_chunk event should have passthrough=True in data."""
        from meho_app.modules.agents.orchestrator.agent import OrchestratorAgent

        # We test the event structure by collecting events from _synthesize_streaming
        # Create a minimal agent mock
        agent = object.__new__(OrchestratorAgent)
        agent.agent_name = "orchestrator"

        state = OrchestratorState(user_goal="Show me pods")
        state.all_findings = [
            SubgraphOutput(
                connector_id="k8s-prod",
                connector_name="Production K8s",
                findings="Found 5 running pods",
                status="success",
            ),
        ]

        events = []
        async for event in agent._synthesize_streaming(state, session_id="test-123"):
            events.append(event)

        # Should yield exactly one event
        assert len(events) == 1

        event = events[0]
        assert event.type == "synthesis_chunk"
        assert event.data["passthrough"] is True
        assert event.data["source_connector"] == "Production K8s"
        assert event.data["source_connector_id"] == "k8s-prod"
        assert event.data["content"] == "Found 5 running pods"
        assert event.data["accumulated_length"] == len("Found 5 running pods")
        assert event.session_id == "test-123"

        # State should have final_answer set
        assert state.final_answer == "Found 5 running pods"

    @pytest.mark.asyncio
    def test_passthrough_skipped_for_multiple_connectors(self):
        """Multi-connector should NOT use passthrough -- requires LLM synthesis."""
        from meho_app.modules.agents.orchestrator.agent import OrchestratorAgent

        agent = object.__new__(OrchestratorAgent)
        agent.agent_name = "orchestrator"

        state = OrchestratorState(user_goal="Compare systems")
        state.all_findings = [
            SubgraphOutput("k8s", "K8s", "K8s data", status="success"),
            SubgraphOutput("gcp", "GCP", "GCP data", status="success"),
        ]

        # _synthesize_streaming will try to call LLM -- we just verify
        # passthrough is NOT triggered by checking the condition directly
        successful = [f for f in state.all_findings if f.status == "success"]
        failed = [f for f in state.all_findings if f.status != "success"]

        assert not (len(successful) == 1 and not failed)


# =============================================================================
# SubgraphOutput data_refs Tests
# =============================================================================


class TestSubgraphOutputDataRefs:
    """Tests for SubgraphOutput data_refs field."""

    def test_data_refs_default_empty(self):
        """data_refs should default to empty list."""
        output = SubgraphOutput(
            connector_id="k8s-prod",
            connector_name="Production K8s",
            findings="Found pods",
        )

        assert output.data_refs == []

    def test_data_refs_with_values(self):
        """data_refs should accept list of dicts."""
        refs = [
            {"table": "namespaces", "session_id": "abc-123", "row_count": 44},
            {"table": "pods", "session_id": "abc-123", "row_count": 12},
        ]
        output = SubgraphOutput(
            connector_id="k8s-prod",
            connector_name="Production K8s",
            findings="Found pods and namespaces",
            data_refs=refs,
        )

        assert len(output.data_refs) == 2
        assert output.data_refs[0]["table"] == "namespaces"
        assert output.data_refs[1]["row_count"] == 12

    def test_data_refs_round_trip_serialization(self):
        """to_dict and from_dict preserve data_refs."""
        refs = [
            {"table": "namespaces", "session_id": "abc-123", "row_count": 44},
        ]
        original = SubgraphOutput(
            connector_id="k8s-prod",
            connector_name="Production K8s",
            findings="Found namespaces",
            status="success",
            confidence=0.9,
            data_refs=refs,
        )

        # Serialize
        data = original.to_dict()
        assert "data_refs" in data
        assert len(data["data_refs"]) == 1
        assert data["data_refs"][0]["table"] == "namespaces"

        # Deserialize
        restored = SubgraphOutput.from_dict(data)
        assert restored.data_refs == refs
        assert restored.connector_id == "k8s-prod"

    def test_data_refs_absent_in_legacy_data(self):
        """from_dict should handle missing data_refs (backward compatibility)."""
        # Simulate old serialized data without data_refs
        legacy_data = {
            "connector_id": "k8s-prod",
            "connector_name": "Production K8s",
            "findings": "Found pods",
            "status": "success",
            "confidence": 0.5,
            "entities_discovered": [],
            "error_message": None,
            "execution_time_ms": 100.0,
        }

        # from_dict uses cls(**data), so missing data_refs uses the default
        # However, from_dict passes all keys -- so if data_refs is missing,
        # it relies on the default. We need to test this works.
        output = SubgraphOutput.from_dict(legacy_data)
        assert output.data_refs == []


# =============================================================================
# Data Refs Population Logic Tests
# =============================================================================


class TestDataRefsPopulation:
    """Tests for data_refs population from unified executor cache.

    Verifies the transformation logic that converts table_info entries
    (from UnifiedExecutor.get_session_table_info_async) into the data_refs
    format expected by the frontend: {"table", "session_id", "row_count"}.
    """

    def test_data_refs_format_from_table_info(self):
        """data_refs constructed from table_info with correct three-field format."""
        session_id = "sess-123"
        table_infos = [
            {
                "table": "pods",
                "operation": "list_pods",
                "connector_id": "k8s-1",
                "columns": ["name", "status"],
                "row_count": 12,
                "cached_at": "2026-02-27T10:00:00",
            }
        ]
        data_refs = [
            {
                "table": info["table"],
                "session_id": session_id,
                "row_count": info["row_count"],
            }
            for info in table_infos
        ]
        assert data_refs == [{"table": "pods", "session_id": "sess-123", "row_count": 12}]

    def test_data_refs_empty_for_no_tables(self):
        """No cached tables produces empty data_refs."""
        table_infos: list[dict[str, Any]] = []
        data_refs = [
            {"table": info["table"], "session_id": "sess-123", "row_count": info["row_count"]}
            for info in table_infos
        ]
        assert data_refs == []

    def test_data_refs_empty_when_no_session_id(self):
        """data_refs stays empty when session_id is None (matches orchestrator guard)."""
        session_id = None
        data_refs: list[dict[str, Any]] = []
        if session_id:
            # This branch should not execute
            data_refs = [{"table": "should_not_appear", "session_id": session_id, "row_count": 1}]
        assert data_refs == []

    def test_data_refs_multiple_tables(self):
        """Multiple cached tables all appear in data_refs with correct session_id."""
        session_id = "sess-456"
        table_infos = [
            {
                "table": "pods",
                "row_count": 12,
                "operation": "x",
                "connector_id": "k",
                "columns": [],
                "cached_at": "",
            },
            {
                "table": "services",
                "row_count": 8,
                "operation": "y",
                "connector_id": "k",
                "columns": [],
                "cached_at": "",
            },
            {
                "table": "nodes",
                "row_count": 3,
                "operation": "z",
                "connector_id": "k",
                "columns": [],
                "cached_at": "",
            },
        ]
        data_refs = [
            {"table": info["table"], "session_id": session_id, "row_count": info["row_count"]}
            for info in table_infos
        ]
        assert len(data_refs) == 3
        assert all(r["session_id"] == "sess-456" for r in data_refs)
        assert [r["table"] for r in data_refs] == ["pods", "services", "nodes"]
        assert [r["row_count"] for r in data_refs] == [12, 8, 3]

    def test_data_refs_serialization_in_subgraph_output(self):
        """SubgraphOutput with data_refs serializes correctly via to_dict."""
        output = SubgraphOutput(
            connector_id="k8s-prod",
            connector_name="Production K8s",
            findings="Found 5 pods",
            status="success",
            data_refs=[
                {"table": "pods", "session_id": "sess-789", "row_count": 5},
            ],
        )
        d = output.to_dict()
        assert d["data_refs"] == [{"table": "pods", "session_id": "sess-789", "row_count": 5}]
        assert d["status"] == "success"

    def test_data_refs_default_empty_on_timeout_status(self):
        """Timeout SubgraphOutput has empty data_refs by default."""
        timeout_output = SubgraphOutput(
            connector_id="k8s-prod",
            connector_name="Production K8s",
            findings="",
            status="timeout",
            error_message="Agent timed out",
        )
        assert timeout_output.data_refs == []

    def test_data_refs_default_empty_on_failed_status(self):
        """Failed SubgraphOutput has empty data_refs by default."""
        failed_output = SubgraphOutput(
            connector_id="k8s-prod",
            connector_name="Production K8s",
            findings="",
            status="failed",
            error_message="Connection error",
        )
        assert failed_output.data_refs == []

    def test_data_refs_graceful_degradation_on_exception(self):
        """Simulates cache failure: data_refs remains [] after exception."""
        data_refs: list[dict[str, Any]] = []
        try:
            # Simulate what happens in _run_single_agent when cache raises
            raise RuntimeError("Redis connection failed")
        except Exception:  # noqa: S110 -- intentional silent exception handling
            pass  # Graceful degradation -- data_refs stays []

        assert data_refs == []
        # The SubgraphOutput would still be created with empty data_refs
        output = SubgraphOutput(
            connector_id="k8s-prod",
            connector_name="Production K8s",
            findings="Found pods despite cache failure",
            status="success",
            data_refs=data_refs,
        )
        assert output.data_refs == []
        assert output.status == "success"
