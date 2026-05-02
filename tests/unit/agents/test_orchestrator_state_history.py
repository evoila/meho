# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""Unit tests for OrchestratorState with conversation_history field.

Tests the conversation_history field added for multi-turn context awareness.
"""

from meho_app.modules.agents.orchestrator.state import OrchestratorState


class TestOrchestratorStateHistory:
    """Tests for OrchestratorState.conversation_history field."""

    def test_default_conversation_history_is_none(self) -> None:
        """conversation_history should default to None."""
        state = OrchestratorState(user_goal="List all pods")
        assert state.conversation_history is None

    def test_state_with_empty_history(self) -> None:
        """State can be created with empty history list."""
        state = OrchestratorState(
            user_goal="Show more details",
            conversation_history=[],
        )
        assert state.conversation_history == []

    def test_state_with_single_message(self) -> None:
        """State can be created with single message in history."""
        history = [{"role": "user", "content": "List all namespaces"}]
        state = OrchestratorState(
            user_goal="Show the other 15",
            conversation_history=history,
        )
        assert state.conversation_history == history
        assert len(state.conversation_history) == 1

    def test_state_with_multiple_messages(self) -> None:
        """State can be created with multiple messages in history."""
        history = [
            {"role": "user", "content": "List all namespaces in k8s"},
            {"role": "assistant", "content": "Found 30 namespaces..."},
            {"role": "user", "content": "Show the other 15"},
        ]
        state = OrchestratorState(
            user_goal="Actually, filter by kube-",
            session_id="test-session",
            conversation_history=history,
        )
        assert state.conversation_history == history
        assert len(state.conversation_history) == 3
        assert state.session_id == "test-session"

    def test_state_with_all_fields(self) -> None:
        """State can be created with all fields including conversation_history."""
        history = [
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "Hi there!"},
        ]
        state = OrchestratorState(
            user_goal="What can you do?",
            session_id="session-123",
            conversation_history=history,
            max_iterations=5,
        )
        assert state.user_goal == "What can you do?"
        assert state.session_id == "session-123"
        assert state.conversation_history == history
        assert state.max_iterations == 5
        assert state.current_iteration == 0

    def test_history_does_not_affect_other_methods(self) -> None:
        """conversation_history should not affect existing state methods."""
        history = [{"role": "user", "content": "Previous message"}]
        state = OrchestratorState(
            user_goal="New query",
            conversation_history=history,
        )

        # Existing methods should still work
        assert state.get_findings_summary() == ""
        assert state.get_queried_connector_ids() == set()
        assert state.has_sufficient_findings() is False
        assert state.is_last_iteration() is False

    def test_history_message_format(self) -> None:
        """History messages should have role and content keys."""
        history = [
            {"role": "user", "content": "Query A"},
            {"role": "assistant", "content": "Response A"},
        ]
        state = OrchestratorState(
            user_goal="Query B",
            conversation_history=history,
        )

        for msg in state.conversation_history:
            assert "role" in msg
            assert "content" in msg
            assert msg["role"] in ("user", "assistant")
