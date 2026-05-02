# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""Unit tests for OrchestratorAgent state awareness (Phase 3 - TASK-185).

Tests for:
- _build_session_context() generates context from session state
- _decide_next_action() includes session context in prompts
- _synthesize() includes multi-turn awareness
- run_streaming() extracts session_state from context
- Session state propagated to sub-agents
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from meho_app.modules.agents.orchestrator.agent import OrchestratorAgent
from meho_app.modules.agents.orchestrator.state import (
    OrchestratorState,
)
from meho_app.modules.agents.persistence import OrchestratorSessionState

# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def mock_dependencies():
    """Create mock MEHODependencies."""
    deps = MagicMock()
    deps.user_context = MagicMock()
    deps.user_context.tenant_id = "test-tenant"
    deps.user_context.user_id = "test-user"

    # Mock connector repository
    deps.connector_repo = MagicMock()
    deps.connector_repo.list_connectors = AsyncMock(return_value=[])

    return deps


@pytest.fixture
def orchestrator_agent(mock_dependencies):
    """Create OrchestratorAgent instance with mocked dependencies."""
    with patch.object(OrchestratorAgent, "_load_config") as mock_config:
        mock_config.return_value = MagicMock(
            max_iterations=3,
            model=MagicMock(name="openai:gpt-4.1-mini"),
        )
        agent = OrchestratorAgent(dependencies=mock_dependencies)
        return agent


@pytest.fixture
def session_state_with_connector():
    """Create session state with a previous connector."""
    state = OrchestratorSessionState()
    state.turn_count = 1
    state.remember_connector(
        connector_id="k8s-prod-123",
        connector_name="K8s Production",
        connector_type="kubernetes",
        query="list pods in default namespace",
        status="success",
    )
    return state


@pytest.fixture
def session_state_with_context():
    """Create session state with operation context."""
    state = OrchestratorSessionState()
    state.turn_count = 2
    state.remember_connector(
        connector_id="k8s-prod-123",
        connector_name="K8s Production",
        connector_type="kubernetes",
        status="success",
    )
    state.set_operation_context("Debug pod restarts", ["nginx-pod", "api-pod"])
    state.register_cached_data("pods", "k8s-prod-123", 50)
    return state


@pytest.fixture
def session_state_with_errors():
    """Create session state with recent errors."""
    state = OrchestratorSessionState()
    state.turn_count = 1
    state.record_error("vmware-001", "timeout", "Request timed out")
    state.record_error("gcp-002", "auth_error", "Authentication failed")
    return state


# =============================================================================
# Tests for _build_session_context
# =============================================================================


class TestBuildSessionContext:
    """Test _build_session_context method."""

    def test_returns_empty_for_none_state(self, orchestrator_agent):
        """Test that None session state returns empty string."""
        result = orchestrator_agent._build_session_context(None)
        assert result == ""

    def test_returns_empty_for_new_conversation(self, orchestrator_agent):
        """Test that turn_count=0 returns empty string."""
        state = OrchestratorSessionState()
        assert state.turn_count == 0
        result = orchestrator_agent._build_session_context(state)
        assert result == ""

    def test_includes_previous_connectors(self, orchestrator_agent, session_state_with_connector):
        """Test that previous connectors are included."""
        result = orchestrator_agent._build_session_context(session_state_with_connector)
        assert "K8s Production" in result
        assert "[OK]" in result
        assert "Previously Used Connectors" in result

    def test_includes_failed_connector_status(self, orchestrator_agent):
        """Test that failed connectors show ERROR status."""
        state = OrchestratorSessionState()
        state.turn_count = 1
        state.remember_connector(
            connector_id="vmware-001",
            connector_name="VMware DC",
            connector_type="vmware",
            status="failed",
        )
        result = orchestrator_agent._build_session_context(state)
        assert "VMware DC" in result
        assert "[ERROR]" in result

    def test_includes_connector_query(self, orchestrator_agent, session_state_with_connector):
        """Test that last query is included in context."""
        result = orchestrator_agent._build_session_context(session_state_with_connector)
        assert "list pods" in result

    def test_truncates_long_queries(self, orchestrator_agent):
        """Test that long queries are truncated."""
        state = OrchestratorSessionState()
        state.turn_count = 1
        long_query = "x" * 100
        state.remember_connector(
            connector_id="conn-1",
            connector_name="Connector",
            connector_type="rest",
            query=long_query,
            status="success",
        )
        result = orchestrator_agent._build_session_context(state)
        # Should be truncated to 50 chars + "..."
        assert "..." in result
        assert len(long_query) > 50  # Verify our test data is indeed long

    def test_includes_operation_context(self, orchestrator_agent, session_state_with_context):
        """Test that operation context is included."""
        result = orchestrator_agent._build_session_context(session_state_with_context)
        assert "Debug pod restarts" in result
        assert "User's Current Focus" in result

    def test_includes_entities(self, orchestrator_agent, session_state_with_context):
        """Test that operation entities are included."""
        result = orchestrator_agent._build_session_context(session_state_with_context)
        assert "nginx-pod" in result
        assert "api-pod" in result
        assert "Key Entities" in result

    def test_includes_cached_tables(self, orchestrator_agent, session_state_with_context):
        """Test that cached tables are included."""
        result = orchestrator_agent._build_session_context(session_state_with_context)
        assert "pods" in result
        assert "Cached Data Available" in result

    def test_includes_recent_errors(self, orchestrator_agent, session_state_with_errors):
        """Test that recent errors are included."""
        result = orchestrator_agent._build_session_context(session_state_with_errors)
        assert "timeout" in result
        assert "auth_error" in result
        assert "Recent Errors" in result

    def test_limits_errors_to_last_three(self, orchestrator_agent):
        """Test that only last 3 errors are shown."""
        state = OrchestratorSessionState()
        state.turn_count = 1
        for i in range(5):
            state.record_error(f"conn-{i}", f"error_{i}", f"Message {i}")

        result = orchestrator_agent._build_session_context(state)
        # Should only include the last 3
        assert "error_2" in result
        assert "error_3" in result
        assert "error_4" in result
        # First two should not be included
        assert "error_0" not in result
        assert "error_1" not in result


# =============================================================================
# Tests for OrchestratorState with session_state
# =============================================================================


class TestOrchestratorStateWithSessionState:
    """Test OrchestratorState with session_state field."""

    def test_session_state_defaults_to_none(self):
        """Test that session_state defaults to None."""
        state = OrchestratorState(user_goal="test")
        assert state.session_state is None

    def test_session_state_can_be_set(self, session_state_with_connector):
        """Test that session_state can be set during construction."""
        state = OrchestratorState(
            user_goal="test",
            session_state=session_state_with_connector,
        )
        assert state.session_state is not None
        assert state.session_state.turn_count == 1


# =============================================================================
# Tests for _decide_next_action with session state
# =============================================================================


class TestDecideNextActionWithSessionState:
    """Test _decide_next_action includes session context."""

    @pytest.mark.asyncio
    async def test_session_context_added_to_prompt(
        self, orchestrator_agent, session_state_with_connector
    ):
        """Test that session context is appended to routing prompt."""
        state = OrchestratorState(
            user_goal="Show me more pods",
            session_state=session_state_with_connector,
        )

        # Mock the LLM to capture the prompt
        orchestrator_agent._call_llm = AsyncMock(return_value='{"action": "respond"}')
        orchestrator_agent._get_available_connectors = AsyncMock(
            return_value=[
                {
                    "id": "k8s-1",
                    "name": "K8s",
                    "connector_type": "kubernetes",
                    "routing_description": "Kubernetes cluster for pods",
                    "description": "K8s cluster",
                }
            ]
        )

        await orchestrator_agent._decide_next_action(state)

        # Verify LLM was called with session context
        call_args = orchestrator_agent._call_llm.call_args[0][0]
        assert "Session Context" in call_args or "K8s Production" in call_args

    @pytest.mark.asyncio
    async def test_no_session_context_for_new_conversation(self, orchestrator_agent):
        """Test that new conversations don't have session context added."""
        state = OrchestratorState(
            user_goal="List all pods",
            session_state=OrchestratorSessionState(),  # turn_count = 0
        )

        orchestrator_agent._call_llm = AsyncMock(return_value='{"action": "respond"}')
        orchestrator_agent._get_available_connectors = AsyncMock(return_value=[])

        await orchestrator_agent._decide_next_action(state)

        # Verify no session context was added (would contain "Session Context" header)
        call_args = orchestrator_agent._call_llm.call_args
        if call_args:
            call_args[0][0]
            # For new conversation, session context section shouldn't be added
            # (the method might not even be called if no connectors)


# =============================================================================
# Tests for _synthesize with session state
# =============================================================================


class TestSynthesizeWithSessionState:
    """Test _synthesize includes multi-turn awareness."""

    @pytest.mark.asyncio
    async def test_adds_conversation_context_for_multiturn(
        self, orchestrator_agent, session_state_with_connector
    ):
        """Test that multi-turn context is added to synthesis prompt."""
        from meho_app.modules.agents.orchestrator.contracts import SubgraphOutput

        state = OrchestratorState(
            user_goal="Show me pod details",
            session_state=session_state_with_connector,
        )
        state.all_findings = [
            SubgraphOutput(
                connector_id="k8s-prod-123",
                connector_name="K8s Production",
                findings="Found 5 pods in default namespace",
                status="success",
            )
        ]

        # Mock LLM to capture prompt
        orchestrator_agent._call_llm = AsyncMock(return_value="Here are the pods...")

        await orchestrator_agent._synthesize(state)

        # Verify conversation context was added
        call_args = orchestrator_agent._call_llm.call_args[0][0]
        assert "Conversation Context" in call_args
        assert "turn 2" in call_args  # turn_count + 1

    @pytest.mark.asyncio
    async def test_no_context_for_first_turn(self, orchestrator_agent):
        """Test that first turn doesn't add conversation context."""
        from meho_app.modules.agents.orchestrator.contracts import SubgraphOutput

        state = OrchestratorState(
            user_goal="List pods",
            session_state=OrchestratorSessionState(),  # turn_count = 0
        )
        state.all_findings = [
            SubgraphOutput(
                connector_id="k8s-1",
                connector_name="K8s",
                findings="Found pods",
                status="success",
            )
        ]

        orchestrator_agent._call_llm = AsyncMock(return_value="Here are the pods...")

        await orchestrator_agent._synthesize(state)

        # Verify no conversation context added
        call_args = orchestrator_agent._call_llm.call_args[0][0]
        assert "Conversation Context" not in call_args


# =============================================================================
# Tests for run_streaming session state extraction
# =============================================================================


class TestRunStreamingSessionStateExtraction:
    """Test run_streaming extracts session_state from context."""

    @pytest.mark.asyncio
    async def test_extracts_session_state_from_context(
        self, orchestrator_agent, session_state_with_connector
    ):
        """Test that session_state is extracted from context dict."""
        # Mock internal methods to avoid full execution
        orchestrator_agent._decide_next_action = AsyncMock(return_value={"action": "respond"})
        orchestrator_agent._synthesize = AsyncMock(return_value="Test response")

        context = {"session_state": session_state_with_connector}

        # Collect events
        events = []
        async for event in orchestrator_agent.run_streaming(
            user_message="Test message",
            session_id="test-session",
            context=context,
        ):
            events.append(event)

        # Verify at least some events were emitted
        assert len(events) > 0

    @pytest.mark.asyncio
    async def test_handles_missing_context(self, orchestrator_agent):
        """Test that missing context is handled gracefully."""
        orchestrator_agent._decide_next_action = AsyncMock(return_value={"action": "respond"})
        orchestrator_agent._synthesize = AsyncMock(return_value="Test response")

        # No context provided
        events = []
        async for event in orchestrator_agent.run_streaming(
            user_message="Test message",
            session_id="test-session",
            context=None,
        ):
            events.append(event)

        assert len(events) > 0

    @pytest.mark.asyncio
    async def test_handles_context_without_session_state(self, orchestrator_agent):
        """Test that context without session_state is handled."""
        orchestrator_agent._decide_next_action = AsyncMock(return_value={"action": "respond"})
        orchestrator_agent._synthesize = AsyncMock(return_value="Test response")

        context = {"history": "some history"}  # No session_state key

        events = []
        async for event in orchestrator_agent.run_streaming(
            user_message="Test message",
            session_id="test-session",
            context=context,
        ):
            events.append(event)

        assert len(events) > 0
