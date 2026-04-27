# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Integration tests for adapter state persistence flow (TASK-185 Phase 2).

These tests verify that run_orchestrator_streaming properly integrates
with AgentStateStore to persist state across conversation turns.

Tests can run with mocked Redis (unit-level) or real Redis (integration-level).
"""

from unittest.mock import AsyncMock

import pytest

from meho_app.modules.agents.adapter import (
    _update_state_from_event,
    run_orchestrator_streaming,
)
from meho_app.modules.agents.persistence import (
    AgentStateStore,
    OrchestratorSessionState,
)

pytestmark = pytest.mark.asyncio


class FakeAgentEvent:
    """Fake AgentEvent for testing."""

    def __init__(self, event_type: str, data: dict):
        self.type = event_type
        self.data = data


class TestUpdateStateFromEvent:
    """Test the _update_state_from_event helper function."""

    def test_connector_complete_updates_state(self):
        """Test that connector_complete events update the session state."""
        state = OrchestratorSessionState()
        event = FakeAgentEvent(
            "connector_complete",
            {
                "connector_id": "k8s-prod-123",
                "connector_name": "K8s Production",
                "connector_type": "kubernetes",
                "status": "success",
                "query": "list pods in default namespace",
            },
        )

        _update_state_from_event(state, event)

        assert "k8s-prod-123" in state.connectors
        assert state.connectors["k8s-prod-123"].connector_name == "K8s Production"
        assert state.connectors["k8s-prod-123"].connector_type == "kubernetes"
        assert state.connectors["k8s-prod-123"].last_status == "success"
        assert state.connectors["k8s-prod-123"].last_query == "list pods in default namespace"
        assert state.primary_connector_id == "k8s-prod-123"

    def test_error_event_updates_state(self):
        """Test that error events are recorded in session state."""
        state = OrchestratorSessionState()
        event = FakeAgentEvent(
            "error",
            {
                "connector_id": "vmware-001",
                "error_type": "timeout",
                "message": "Request timed out after 30s",
            },
        )

        _update_state_from_event(state, event)

        assert len(state.recent_errors) == 1
        assert state.has_recent_error("vmware-001", "timeout") is True

    def test_error_without_connector_id_is_ignored(self):
        """Test that errors without connector_id don't crash."""
        state = OrchestratorSessionState()
        event = FakeAgentEvent(
            "error",
            {
                "message": "General error without connector",
            },
        )

        _update_state_from_event(state, event)

        # Should not crash, and no error should be recorded
        assert len(state.recent_errors) == 0

    def test_other_events_are_ignored(self):
        """Test that non-tracked events don't affect state."""
        state = OrchestratorSessionState()
        state.to_dict()

        for event_type in ["thought", "action", "observation", "iteration_start"]:
            event = FakeAgentEvent(event_type, {"content": "test"})
            _update_state_from_event(state, event)

        # State should be unchanged (except created_at/last_updated are same)
        assert len(state.connectors) == 0
        assert len(state.recent_errors) == 0


class TestRunOrchestratorStreamingWithMockedStore:
    """Test run_orchestrator_streaming with mocked dependencies."""

    async def test_state_loaded_at_start(self):
        """Test that state is loaded when session_id and state_store provided."""
        # Create mock state store
        mock_store = AsyncMock(spec=AgentStateStore)
        existing_state = OrchestratorSessionState()
        existing_state.turn_count = 2
        existing_state.remember_connector("k8s-123", "K8s Prod", "kubernetes", status="success")
        mock_store.load_state.return_value = existing_state
        mock_store.save_state.return_value = True

        # Create mock agent that yields one event
        mock_agent = AsyncMock()

        async def mock_stream(*args, **kwargs):
            yield FakeAgentEvent("final_answer", {"content": "Done"})

        mock_agent.run_streaming = mock_stream

        # Run the streaming
        events = []
        async for event in run_orchestrator_streaming(
            agent=mock_agent,
            user_message="test message",
            session_id="session-123",
            conversation_history=[],
            state_store=mock_store,
        ):
            events.append(event)

        # Verify state was loaded
        mock_store.load_state.assert_called_once_with("session-123")
        # Verify state was saved
        mock_store.save_state.assert_called_once()

    async def test_new_state_created_when_not_found(self):
        """Test that new state is created when load_state returns None."""
        mock_store = AsyncMock(spec=AgentStateStore)
        mock_store.load_state.return_value = None
        mock_store.save_state.return_value = True

        mock_agent = AsyncMock()

        async def mock_stream(*args, **kwargs):
            yield FakeAgentEvent("final_answer", {"content": "Done"})

        mock_agent.run_streaming = mock_stream

        events = []
        async for event in run_orchestrator_streaming(
            agent=mock_agent,
            user_message="test message",
            session_id="new-session",
            conversation_history=[],
            state_store=mock_store,
        ):
            events.append(event)

        # Verify state was saved (new state created internally)
        mock_store.save_state.assert_called_once()
        saved_state = mock_store.save_state.call_args[0][1]
        assert isinstance(saved_state, OrchestratorSessionState)

    async def test_no_persistence_without_session_id(self):
        """Test that persistence is skipped when session_id is None."""
        mock_store = AsyncMock(spec=AgentStateStore)

        mock_agent = AsyncMock()

        async def mock_stream(*args, **kwargs):
            yield FakeAgentEvent("final_answer", {"content": "Done"})

        mock_agent.run_streaming = mock_stream

        events = []
        async for event in run_orchestrator_streaming(
            agent=mock_agent,
            user_message="test message",
            session_id=None,
            conversation_history=[],
            state_store=mock_store,
        ):
            events.append(event)

        # No load or save should happen
        mock_store.load_state.assert_not_called()
        mock_store.save_state.assert_not_called()

    async def test_no_persistence_without_state_store(self):
        """Test that streaming works without state_store."""
        mock_agent = AsyncMock()

        async def mock_stream(*args, **kwargs):
            yield FakeAgentEvent("final_answer", {"content": "Done"})

        mock_agent.run_streaming = mock_stream

        events = []
        async for event in run_orchestrator_streaming(
            agent=mock_agent,
            user_message="test message",
            session_id="session-123",
            conversation_history=[],
            state_store=None,  # No store
        ):
            events.append(event)

        # Should complete without error
        assert len(events) == 1

    async def test_state_saved_even_on_error(self):
        """Test that state is saved in finally block even if agent errors."""
        mock_store = AsyncMock(spec=AgentStateStore)
        mock_store.load_state.return_value = None
        mock_store.save_state.return_value = True

        mock_agent = AsyncMock()

        async def mock_stream(*args, **kwargs):
            yield FakeAgentEvent("thought", {"content": "Thinking..."})
            raise RuntimeError("Agent crashed!")

        mock_agent.run_streaming = mock_stream

        events = []
        with pytest.raises(RuntimeError, match="Agent crashed!"):  # noqa: PT012 -- multi-statement raises block is intentional
            async for event in run_orchestrator_streaming(
                agent=mock_agent,
                user_message="test message",
                session_id="session-123",
                conversation_history=[],
                state_store=mock_store,
            ):
                events.append(event)

        # State should still be saved
        mock_store.save_state.assert_called_once()

    async def test_connector_events_update_state(self):
        """Test that connector_complete events update the persisted state."""
        mock_store = AsyncMock(spec=AgentStateStore)
        mock_store.load_state.return_value = None
        mock_store.save_state.return_value = True

        mock_agent = AsyncMock()

        async def mock_stream(*args, **kwargs):
            yield FakeAgentEvent(
                "connector_complete",
                {
                    "connector_id": "k8s-123",
                    "connector_name": "K8s Production",
                    "connector_type": "kubernetes",
                    "status": "success",
                },
            )
            yield FakeAgentEvent("final_answer", {"content": "Found 10 pods"})

        mock_agent.run_streaming = mock_stream

        events = []
        async for event in run_orchestrator_streaming(
            agent=mock_agent,
            user_message="list pods",
            session_id="session-123",
            conversation_history=[],
            state_store=mock_store,
        ):
            events.append(event)

        # Check the saved state
        saved_state = mock_store.save_state.call_args[0][1]
        assert "k8s-123" in saved_state.connectors
        assert saved_state.connectors["k8s-123"].connector_name == "K8s Production"

    async def test_context_summary_added_to_history(self):
        """Test that session context is prepended to history for follow-up turns."""
        mock_store = AsyncMock(spec=AgentStateStore)
        existing_state = OrchestratorSessionState()
        existing_state.turn_count = 1  # Previous turn exists
        existing_state.remember_connector("k8s-123", "K8s Prod", "kubernetes", status="success")
        existing_state.set_operation_context("Debugging pod crashes", ["nginx-pod"])
        mock_store.load_state.return_value = existing_state
        mock_store.save_state.return_value = True

        captured_context = {}
        mock_agent = AsyncMock()

        async def mock_stream(user_message, session_id, context):
            # Capture the context passed to the agent
            captured_context.update(context)
            yield FakeAgentEvent("final_answer", {"content": "Done"})

        mock_agent.run_streaming = mock_stream

        async for _event in run_orchestrator_streaming(
            agent=mock_agent,
            user_message="show me more",
            session_id="session-123",
            conversation_history=[],
            state_store=mock_store,
        ):
            pass  # Context manager runs the agent; side effects are asserted below

        # Check that context was injected
        history = captured_context.get("history", "")
        assert "[Session context:" in history
        assert "K8s Prod" in history

    async def test_graceful_degradation_on_load_failure(self):
        """Test that streaming continues if state load fails."""
        mock_store = AsyncMock(spec=AgentStateStore)
        mock_store.load_state.side_effect = Exception("Redis connection failed")
        mock_store.save_state.return_value = True

        mock_agent = AsyncMock()

        async def mock_stream(*args, **kwargs):
            yield FakeAgentEvent("final_answer", {"content": "Done"})

        mock_agent.run_streaming = mock_stream

        events = []
        async for event in run_orchestrator_streaming(
            agent=mock_agent,
            user_message="test message",
            session_id="session-123",
            conversation_history=[],
            state_store=mock_store,
        ):
            events.append(event)

        # Should complete without raising
        assert len(events) == 1
        # State should still be saved
        mock_store.save_state.assert_called_once()

    async def test_graceful_degradation_on_save_failure(self):
        """Test that streaming completes even if state save fails."""
        mock_store = AsyncMock(spec=AgentStateStore)
        mock_store.load_state.return_value = None
        mock_store.save_state.side_effect = Exception("Redis connection failed")

        mock_agent = AsyncMock()

        async def mock_stream(*args, **kwargs):
            yield FakeAgentEvent("final_answer", {"content": "Done"})

        mock_agent.run_streaming = mock_stream

        events = []
        async for event in run_orchestrator_streaming(
            agent=mock_agent,
            user_message="test message",
            session_id="session-123",
            conversation_history=[],
            state_store=mock_store,
        ):
            events.append(event)

        # Should complete without raising
        assert len(events) == 1


class TestMultiTurnScenarios:
    """Test multi-turn conversation scenarios."""

    async def test_three_turn_conversation_flow(self):
        """Simulate a 3-turn conversation with state persistence."""
        mock_store = AsyncMock(spec=AgentStateStore)
        saved_states = []

        def capture_save(session_id, state):
            # Deep copy to capture state at save time
            saved_states.append(OrchestratorSessionState.from_dict(state.to_dict()))
            return True

        mock_store.save_state.side_effect = capture_save

        # Turn 1: No prior state
        mock_store.load_state.return_value = None

        mock_agent = AsyncMock()

        async def turn1_stream(*args, **kwargs):
            yield FakeAgentEvent(
                "connector_complete",
                {
                    "connector_id": "k8s-123",
                    "connector_name": "K8s Production",
                    "connector_type": "kubernetes",
                    "status": "success",
                },
            )
            yield FakeAgentEvent("final_answer", {"content": "Found 50 pods"})

        mock_agent.run_streaming = turn1_stream

        async for _ in run_orchestrator_streaming(
            agent=mock_agent,
            user_message="list all pods",
            session_id="session-abc",
            conversation_history=[],
            state_store=mock_store,
        ):
            pass  # Context manager runs the agent; side effects are asserted below

        # Verify turn 1 state
        assert len(saved_states) == 1
        assert "k8s-123" in saved_states[0].connectors

        # Turn 2: Load previous state
        mock_store.load_state.return_value = saved_states[0]

        async def turn2_stream(*args, **kwargs):
            yield FakeAgentEvent(
                "connector_complete",
                {
                    "connector_id": "k8s-123",
                    "connector_name": "K8s Production",
                    "connector_type": "kubernetes",
                    "status": "success",
                },
            )
            yield FakeAgentEvent("final_answer", {"content": "5 pods unhealthy"})

        mock_agent.run_streaming = turn2_stream

        async for _ in run_orchestrator_streaming(
            agent=mock_agent,
            user_message="filter to unhealthy pods",
            session_id="session-abc",
            conversation_history=[{"role": "user", "content": "list all pods"}],
            state_store=mock_store,
        ):
            pass  # Context manager runs the agent; side effects are asserted below

        # Verify turn 2 state
        assert len(saved_states) == 2
        # Connector should still be there
        assert "k8s-123" in saved_states[1].connectors

        # Turn 3: Error occurs
        mock_store.load_state.return_value = saved_states[1]

        async def turn3_stream(*args, **kwargs):
            yield FakeAgentEvent(
                "error",
                {
                    "connector_id": "k8s-123",
                    "error_type": "timeout",
                    "message": "Connection timed out",
                },
            )
            yield FakeAgentEvent("final_answer", {"content": "Failed to fetch logs"})

        mock_agent.run_streaming = turn3_stream

        async for _ in run_orchestrator_streaming(
            agent=mock_agent,
            user_message="show logs for nginx-pod",
            session_id="session-abc",
            conversation_history=[
                {"role": "user", "content": "list all pods"},
                {"role": "assistant", "content": "Found 50 pods"},
            ],
            state_store=mock_store,
        ):
            pass  # Context manager runs the agent; side effects are asserted below

        # Verify turn 3 state
        assert len(saved_states) == 3
        assert saved_states[2].has_recent_error("k8s-123", "timeout") is True
