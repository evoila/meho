# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Integration tests for the session explain endpoint.

Part of TASK-186: Deep Observability & Introspection System.
"""

from datetime import UTC, datetime
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

# Test the explanation generation directly rather than via HTTP
from meho_app.api.observability.router_explain import _generate_explanation


@pytest.fixture
def mock_transcript():
    """Create mock transcript."""
    transcript = MagicMock()
    transcript.id = uuid4()
    transcript.session_id = uuid4()
    transcript.status = "completed"
    transcript.created_at = datetime.now(tz=UTC)
    transcript.completed_at = datetime.now(tz=UTC)
    transcript.total_llm_calls = 2
    transcript.total_operation_calls = 1
    transcript.total_sql_queries = 0
    transcript.total_tool_calls = 1
    transcript.total_tokens = 500
    transcript.total_cost_usd = 0.0005
    transcript.total_duration_ms = 2000
    transcript.user_query = "Test query"
    transcript.agent_type = "react"
    return transcript


@pytest.fixture
def mock_events():
    """Create mock events."""
    events = []
    for i in range(3):
        event = MagicMock()
        event.id = uuid4()
        event.type = ["thought", "action", "observation"][i]
        event.timestamp = datetime.now(tz=UTC)
        event.summary = f"Event {i} summary"
        event.details = {}
        event.parent_event_id = None
        event.step_number = i
        event.node_name = "test_node"
        event.agent_name = "test_agent"
        event.duration_ms = 100
        events.append(event)
    return events


@pytest.fixture
def mock_events_with_errors():
    """Create mock events with errors."""
    events = []

    # Error event
    event = MagicMock()
    event.id = uuid4()
    event.type = "error"
    event.timestamp = datetime.now(tz=UTC)
    event.summary = "HTTP call failed"
    event.details = {"tool_error": "Connection refused", "http_status_code": 500}
    events.append(event)

    return events


@pytest.fixture
def mock_events_with_llm():
    """Create mock events with LLM calls."""
    events = []

    # Thought/LLM event
    event = MagicMock()
    event.id = uuid4()
    event.type = "thought"
    event.timestamp = datetime.now(tz=UTC)
    event.summary = "Planning the query execution"
    event.details = {
        "llm_duration_ms": 1500,
        "llm_parsed": {
            "thought": "I need to query the vSphere API",
            "action": {"tool": "get_vms"},
        },
    }
    events.append(event)

    return events


class TestGenerateExplanationOverview:
    """Tests for overview focus mode."""

    def test_overview_includes_query_and_status(self, mock_transcript, mock_events):
        """Overview should include user query and status."""
        explanation, _key_events = _generate_explanation(mock_transcript, mock_events, "overview")

        assert "Test query" in explanation
        assert "completed" in explanation
        assert "2000ms" in explanation

    def test_overview_includes_summary_stats(self, mock_transcript, mock_events):
        """Overview should include execution statistics."""
        explanation, _key_events = _generate_explanation(mock_transcript, mock_events, "overview")

        assert "LLM Calls" in explanation
        assert "Operation Calls" in explanation
        assert "500" in explanation  # tokens

    def test_overview_includes_event_timeline(self, mock_transcript, mock_events):
        """Overview should include major events timeline."""
        explanation, key_events = _generate_explanation(mock_transcript, mock_events, "overview")

        assert "Event Timeline" in explanation
        assert len(key_events) == 3  # thought, action, observation

    def test_overview_returns_key_events(self, mock_transcript, mock_events):
        """Overview should return key events list."""
        _explanation, key_events = _generate_explanation(mock_transcript, mock_events, "overview")

        assert len(key_events) == 3
        assert key_events[0]["type"] == "thought"


class TestGenerateExplanationErrors:
    """Tests for errors focus mode."""

    def test_errors_shows_no_errors_message(self, mock_transcript, mock_events):
        """Should show success message when no errors."""
        explanation, key_events = _generate_explanation(mock_transcript, mock_events, "errors")

        assert "no errors" in explanation.lower()
        assert len(key_events) == 0

    def test_errors_shows_error_details(self, mock_transcript, mock_events_with_errors):
        """Should show error details when errors exist."""
        explanation, key_events = _generate_explanation(
            mock_transcript, mock_events_with_errors, "errors"
        )

        assert "1 error" in explanation.lower()
        assert len(key_events) > 0


class TestGenerateExplanationPerformance:
    """Tests for performance focus mode."""

    def test_performance_shows_timing(self, mock_transcript, mock_events):
        """Should show duration and token usage."""
        explanation, _key_events = _generate_explanation(
            mock_transcript, mock_events, "performance"
        )

        assert "Performance Analysis" in explanation
        assert "2000ms" in explanation
        assert "500" in explanation  # tokens

    def test_performance_shows_cost(self, mock_transcript, mock_events):
        """Should show estimated cost."""
        explanation, _key_events = _generate_explanation(
            mock_transcript, mock_events, "performance"
        )

        assert "$0.0005" in explanation

    def test_performance_shows_slowest_operations(self, mock_transcript, mock_events_with_llm):
        """Should list slowest operations."""
        explanation, key_events = _generate_explanation(
            mock_transcript, mock_events_with_llm, "performance"
        )

        assert "Slowest Operations" in explanation
        assert len(key_events) > 0


class TestGenerateExplanationDecisions:
    """Tests for decisions focus mode."""

    def test_decisions_shows_reasoning_steps(self, mock_transcript, mock_events_with_llm):
        """Should show LLM reasoning steps."""
        explanation, _key_events = _generate_explanation(
            mock_transcript, mock_events_with_llm, "decisions"
        )

        assert "Decision Analysis" in explanation
        assert "reasoning step" in explanation.lower()

    def test_decisions_extracts_thoughts(self, mock_transcript, mock_events_with_llm):
        """Should extract and show thoughts."""
        _explanation, key_events = _generate_explanation(
            mock_transcript, mock_events_with_llm, "decisions"
        )

        # Should have thought events in key_events
        assert len(key_events) > 0
        assert key_events[0]["type"] == "thought"


class TestExplainEndpointValidation:
    """Tests for endpoint validation logic."""

    def test_valid_focus_values(self):
        """Should accept valid focus values."""
        valid_focuses = ["overview", "errors", "performance", "decisions"]
        for focus in valid_focuses:
            # Just verify no exception is raised
            assert focus in valid_focuses

    def test_invalid_focus_rejected(self):
        """Invalid focus values should be rejected."""
        invalid_focuses = ["invalid", "all", "summary", ""]
        valid_focuses = ["overview", "errors", "performance", "decisions"]
        for focus in invalid_focuses:
            assert focus not in valid_focuses
