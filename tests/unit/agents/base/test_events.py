# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""Tests for AgentEvent and event factory functions.

These tests verify:
1. AgentEvent dataclass works correctly
2. to_sse() produces valid SSE format
3. to_dict() serializes correctly
4. All factory functions create correct event types
"""

from __future__ import annotations

import json
from datetime import datetime

from meho_app.modules.agents.base.events import (
    AgentEvent,
    action_event,
    agent_complete_event,
    agent_start_event,
    approval_required_event,
    error_event,
    final_answer_event,
    node_enter_event,
    node_exit_event,
    observation_event,
    progress_event,
    thought_event,
    tool_complete_event,
    tool_start_event,
)


class TestAgentEvent:
    """Tests for the AgentEvent dataclass."""

    def test_create_event_with_required_fields(self) -> None:
        """Event should be created with required fields."""
        event = AgentEvent(type="thought", agent="react", data={"content": "test"})
        assert event.type == "thought"
        assert event.agent == "react"
        assert event.data == {"content": "test"}
        assert isinstance(event.timestamp, datetime)

    def test_create_event_with_all_fields(self) -> None:
        """Event should accept all optional fields."""
        ts = datetime(2024, 1, 15, 12, 0, 0)  # noqa: DTZ001 -- naive datetime for test compatibility
        event = AgentEvent(
            type="action",
            agent="react",
            data={"tool": "search"},
            timestamp=ts,
            session_id="sess-123",
            step=5,
            node="reason",
        )
        assert event.timestamp == ts
        assert event.session_id == "sess-123"
        assert event.step == 5
        assert event.node == "reason"

    def test_event_default_data_is_empty_dict(self) -> None:
        """Event data should default to empty dict."""
        event = AgentEvent(type="agent_start", agent="react")
        assert event.data == {}


class TestAgentEventSerialization:
    """Tests for AgentEvent serialization methods."""

    def test_to_sse_format(self) -> None:
        """to_sse should produce valid SSE format."""
        event = AgentEvent(
            type="thought",
            agent="react",
            data={"content": "Thinking..."},
        )
        sse = event.to_sse()

        # Should start with "data: "
        assert sse.startswith("data: ")
        # Should end with double newline
        assert sse.endswith("\n\n")

        # Should be valid JSON
        json_str = sse[6:-2]  # Remove "data: " and "\n\n"
        parsed = json.loads(json_str)

        assert parsed["type"] == "thought"
        assert parsed["agent"] == "react"
        assert parsed["data"]["content"] == "Thinking..."
        assert "timestamp" in parsed

    def test_to_sse_includes_optional_fields(self) -> None:
        """to_sse should include optional fields when set."""
        event = AgentEvent(
            type="action",
            agent="react",
            data={"tool": "search"},
            session_id="sess-123",
            step=3,
            node="reason",
        )
        sse = event.to_sse()
        json_str = sse[6:-2]
        parsed = json.loads(json_str)

        assert parsed["session_id"] == "sess-123"
        assert parsed["step"] == 3
        assert parsed["node"] == "reason"

    def test_to_sse_omits_none_optional_fields(self) -> None:
        """to_sse should not include optional fields when None."""
        event = AgentEvent(type="thought", agent="react", data={})
        sse = event.to_sse()
        json_str = sse[6:-2]
        parsed = json.loads(json_str)

        assert "session_id" not in parsed
        assert "step" not in parsed
        assert "node" not in parsed

    def test_to_dict(self) -> None:
        """to_dict should return dictionary representation."""
        event = AgentEvent(
            type="error",
            agent="react",
            data={"message": "Something failed"},
            session_id="sess-456",
        )
        d = event.to_dict()

        assert d["type"] == "error"
        assert d["agent"] == "react"
        assert d["data"]["message"] == "Something failed"
        assert d["session_id"] == "sess-456"
        # Timestamp should be ISO string
        assert isinstance(d["timestamp"], str)


class TestEventFactories:
    """Tests for event factory functions."""

    def test_thought_event(self) -> None:
        """thought_event should create correct event."""
        event = thought_event("react", "I need to search for data")
        assert event.type == "thought"
        assert event.agent == "react"
        assert event.data["content"] == "I need to search for data"

    def test_action_event(self) -> None:
        """action_event should create correct event."""
        event = action_event(
            "react",
            "search_operations",
            {"connector_id": "abc", "query": "list vms"},
        )
        assert event.type == "action"
        assert event.data["tool"] == "search_operations"
        assert event.data["args"]["connector_id"] == "abc"

    def test_observation_event(self) -> None:
        """observation_event should create correct event."""
        event = observation_event(
            "react",
            "search_operations",
            [{"id": 1, "name": "vm1"}],
        )
        assert event.type == "observation"
        assert event.data["tool"] == "search_operations"
        assert event.data["result"] == [{"id": 1, "name": "vm1"}]

    def test_final_answer_event(self) -> None:
        """final_answer_event should create correct event."""
        event = final_answer_event("react", "Here are the results:\n- VM1\n- VM2")
        assert event.type == "final_answer"
        assert "VM1" in event.data["content"]

    def test_error_event_without_details(self) -> None:
        """error_event should work without details."""
        event = error_event("react", "Connection failed")
        assert event.type == "error"
        assert event.data["message"] == "Connection failed"
        assert "details" not in event.data

    def test_error_event_with_details(self) -> None:
        """error_event should include details when provided."""
        event = error_event(
            "react",
            "Connection failed",
            details={"host": "192.168.1.1", "port": 443},
        )
        assert event.data["details"]["host"] == "192.168.1.1"

    def test_approval_required_event(self) -> None:
        """approval_required_event should create correct event."""
        event = approval_required_event(
            agent="react",
            tool="call_operation",
            args={"operation_id": "delete_vm"},
            danger_level="critical",
            description="Delete VM 'web-01'",
        )
        assert event.type == "approval_required"
        assert event.data["tool"] == "call_operation"
        assert event.data["danger_level"] == "critical"
        assert event.data["description"] == "Delete VM 'web-01'"

    def test_tool_start_event(self) -> None:
        """tool_start_event should create correct event."""
        event = tool_start_event("react", "list_connectors")
        assert event.type == "tool_start"
        assert event.data["tool"] == "list_connectors"

    def test_tool_complete_event(self) -> None:
        """tool_complete_event should create correct event."""
        event = tool_complete_event("react", "list_connectors", success=True)
        assert event.type == "tool_complete"
        assert event.data["success"] is True

    def test_tool_complete_event_failure(self) -> None:
        """tool_complete_event should handle failure."""
        event = tool_complete_event("react", "call_operation", success=False)
        assert event.data["success"] is False

    def test_node_enter_event(self) -> None:
        """node_enter_event should create correct event."""
        event = node_enter_event("react", "reason")
        assert event.type == "node_enter"
        assert event.data["node"] == "reason"
        assert event.node == "reason"  # Also set in metadata

    def test_node_exit_event(self) -> None:
        """node_exit_event should create correct event."""
        event = node_exit_event("react", "reason", next_node="tool_dispatch")
        assert event.type == "node_exit"
        assert event.data["node"] == "reason"
        assert event.data["next_node"] == "tool_dispatch"

    def test_node_exit_event_terminal(self) -> None:
        """node_exit_event should handle terminal node."""
        event = node_exit_event("react", "final", next_node=None)
        assert event.data["next_node"] is None

    def test_agent_start_event(self) -> None:
        """agent_start_event should create correct event."""
        event = agent_start_event("react", "List all VMs")
        assert event.type == "agent_start"
        assert event.data["user_message"] == "List all VMs"

    def test_agent_complete_event(self) -> None:
        """agent_complete_event should create correct event."""
        event = agent_complete_event("react", success=True)
        assert event.type == "agent_complete"
        assert event.data["success"] is True

    def test_progress_event(self) -> None:
        """progress_event should create correct event."""
        event = progress_event("react", "Loading connectors...")
        assert event.type == "progress"
        assert event.data["message"] == "Loading connectors..."
        assert "percentage" not in event.data

    def test_progress_event_with_percentage(self) -> None:
        """progress_event should include percentage when provided."""
        event = progress_event("react", "Processing...", percentage=50)
        assert event.data["percentage"] == 50


class TestFactoryKwargs:
    """Tests for factory function kwargs passthrough."""

    def test_factory_accepts_session_id(self) -> None:
        """Factory functions should accept session_id."""
        event = thought_event("react", "Test", session_id="sess-123")
        assert event.session_id == "sess-123"

    def test_factory_accepts_step(self) -> None:
        """Factory functions should accept step."""
        event = action_event("react", "search", {}, step=5)
        assert event.step == 5

    def test_factory_accepts_node(self) -> None:
        """Factory functions should accept node."""
        event = observation_event("react", "search", [], node="tool_dispatch")
        assert event.node == "tool_dispatch"

    def test_factory_accepts_timestamp(self) -> None:
        """Factory functions should accept custom timestamp."""
        ts = datetime(2024, 1, 15, 12, 0, 0)  # noqa: DTZ001 -- naive datetime for test compatibility
        event = final_answer_event("react", "Done", timestamp=ts)
        assert event.timestamp == ts
