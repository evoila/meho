# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""Integration tests for orchestrator conversation history flow.

Tests for:
- Conversation history is extracted from context and passed to state
- History context is included in routing prompts
- History context is included in synthesis prompts
- Follow-up queries receive proper context
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from meho_app.modules.agents.orchestrator import OrchestratorAgent
from meho_app.modules.agents.orchestrator.routing import build_routing_prompt
from meho_app.modules.agents.orchestrator.state import OrchestratorState
from meho_app.modules.agents.orchestrator.synthesis import build_synthesis_prompt

# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def mock_dependencies():
    """Create mock MEHODependencies with connector repo."""
    deps = MagicMock()
    deps.user_context = MagicMock()
    deps.user_context.tenant_id = "test-tenant"
    deps.user_context.user_id = "test-user"

    # Mock connector repository with test connectors
    mock_connectors = [
        MagicMock(
            id="k8s-prod",
            name="K8s Production",
            description="Kubernetes production cluster",
            routing_description="Kubernetes pods, deployments, namespaces",
            connector_type="kubernetes",
            is_active=True,
        ),
    ]
    deps.connector_repo = MagicMock()
    deps.connector_repo.list_connectors = AsyncMock(return_value=mock_connectors)

    return deps


@pytest.fixture
def orchestrator_agent(mock_dependencies):
    """Create OrchestratorAgent with mocked config."""
    with patch.object(OrchestratorAgent, "_load_config") as mock_config:
        mock_config.return_value = MagicMock(
            raw={"orchestrator": {"max_iterations": 3}},
            model=MagicMock(name="openai:gpt-4.1-mini"),
        )
        agent = OrchestratorAgent(dependencies=mock_dependencies)
        return agent


# =============================================================================
# Conversation History Context Tests
# =============================================================================


class TestConversationHistoryExtraction:
    """Tests for extracting conversation history from context."""

    @pytest.mark.asyncio
    async def test_history_extracted_from_context(
        self, orchestrator_agent: OrchestratorAgent
    ) -> None:
        """Verify history is extracted from context and stored in state."""
        # Track state initialization
        captured_state: OrchestratorState | None = None

        async def capture_decision(state: OrchestratorState):
            nonlocal captured_state
            captured_state = state
            # Return respond immediately to stop the loop
            return {"action": "respond"}

        with (
            patch.object(orchestrator_agent, "_decide_next_action", side_effect=capture_decision),
            patch.object(orchestrator_agent, "_synthesize", return_value="Test response"),
        ):
            # Provide history in context (as adapter would)
            context = {"history": "USER: List all namespaces\nASSISTANT: Found 30 namespaces"}

            events = []
            async for event in orchestrator_agent.run_streaming(
                user_message="Show the other 15",
                session_id="test-session",
                context=context,
            ):
                events.append(event)

            # Verify state has conversation_history
            assert captured_state is not None
            assert captured_state.conversation_history is not None
            assert len(captured_state.conversation_history) == 2
            assert captured_state.conversation_history[0]["role"] == "user"
            assert captured_state.conversation_history[1]["role"] == "assistant"

    @pytest.mark.asyncio
    async def test_empty_context_results_in_none_history(
        self, orchestrator_agent: OrchestratorAgent
    ) -> None:
        """Verify empty context results in None conversation_history."""
        captured_state: OrchestratorState | None = None

        async def capture_decision(state: OrchestratorState):
            nonlocal captured_state
            captured_state = state
            return {"action": "respond"}

        with (
            patch.object(orchestrator_agent, "_decide_next_action", side_effect=capture_decision),
            patch.object(orchestrator_agent, "_synthesize", return_value="Test response"),
        ):
            events = []
            async for event in orchestrator_agent.run_streaming(
                user_message="List all pods",
                session_id="test-session",
                context=None,  # No context
            ):
                events.append(event)

            assert captured_state is not None
            assert captured_state.conversation_history is None


class TestRoutingPromptIncludesHistory:
    """Tests for history inclusion in routing prompts."""

    def test_routing_prompt_contains_history(self) -> None:
        """Verify routing prompt includes conversation history."""
        # Create state with history
        state = OrchestratorState(
            user_goal="Show the other 15",
            session_id="test-session",
            conversation_history=[
                {"role": "user", "content": "List all namespaces"},
                {"role": "assistant", "content": "Found 30 namespaces..."},
            ],
        )

        connectors = [
            {
                "id": "k8s-prod",
                "name": "K8s Production",
                "routing_description": "Kubernetes",
                "connector_type": "kubernetes",
            }
        ]

        # Build the routing prompt using module-level function
        prompt = build_routing_prompt(state, connectors, already_queried=set())

        # Verify history is included
        assert "Recent Conversation" in prompt
        assert "List all namespaces" in prompt
        assert "30 namespaces" in prompt

    def test_routing_prompt_without_history(self) -> None:
        """Verify routing prompt handles missing history gracefully."""
        state = OrchestratorState(
            user_goal="List all pods",
            session_id="test-session",
            conversation_history=None,
        )

        connectors = [
            {
                "id": "k8s-prod",
                "name": "K8s Production",
                "routing_description": "Kubernetes",
                "connector_type": "kubernetes",
            }
        ]

        prompt = build_routing_prompt(state, connectors, already_queried=set())

        # Should have placeholder for no history
        assert "No previous conversation" in prompt


class TestSynthesisPromptIncludesHistory:
    """Tests for history inclusion in synthesis prompts."""

    def test_synthesis_prompt_contains_history(self) -> None:
        """Verify synthesis prompt includes conversation history."""
        from meho_app.modules.agents.orchestrator.contracts import SubgraphOutput

        state = OrchestratorState(
            user_goal="Show the other 15",
            session_id="test-session",
            conversation_history=[
                {"role": "user", "content": "List all namespaces"},
                {"role": "assistant", "content": "Found 30 namespaces..."},
            ],
        )
        state.all_findings = [
            SubgraphOutput(
                connector_id="k8s-prod",
                connector_name="K8s Production",
                findings="Remaining 15 namespaces: ns16, ns17...",
                status="success",
            )
        ]

        prompt = build_synthesis_prompt(state)

        # Verify history is included
        assert "Recent Conversation" in prompt
        assert "List all namespaces" in prompt


class TestFollowUpQueryFlow:
    """Tests for complete follow-up query flow with history."""

    @pytest.mark.asyncio
    async def test_followup_query_receives_history_context(
        self, orchestrator_agent: OrchestratorAgent
    ) -> None:
        """Test that follow-up queries receive proper conversation context."""
        routing_prompt_captured: str | None = None
        synthesis_prompt_captured: str | None = None

        original_call_llm = orchestrator_agent._call_llm

        async def capture_llm_call(prompt: str) -> str:
            nonlocal routing_prompt_captured, synthesis_prompt_captured
            if "Connector Routing" in prompt:
                routing_prompt_captured = prompt
                # Return JSON to skip querying
                return '{"action": "respond"}'
            elif "Synthesis" in prompt or "Synthesize" in prompt:
                synthesis_prompt_captured = prompt
                return "Here are the remaining 15 namespaces..."
            return await original_call_llm(prompt)

        with patch.object(orchestrator_agent, "_call_llm", side_effect=capture_llm_call):
            # Simulate follow-up query with history
            context = {
                "history": (
                    "USER: List all namespaces in k8s\n"
                    "ASSISTANT: Found 30 namespaces. Here are the first 15..."
                )
            }

            events = []
            async for event in orchestrator_agent.run_streaming(
                user_message="Show the other 15",
                session_id="test-session",
                context=context,
            ):
                events.append(event)

            # Verify routing prompt received history
            assert routing_prompt_captured is not None
            assert "Recent Conversation" in routing_prompt_captured
            assert "30 namespaces" in routing_prompt_captured

    @pytest.mark.asyncio
    async def test_first_query_has_no_history(self, orchestrator_agent: OrchestratorAgent) -> None:
        """Test that first query correctly shows no previous conversation."""
        routing_prompt_captured: str | None = None

        async def capture_llm_call(prompt: str) -> str:
            nonlocal routing_prompt_captured
            if "Connector Routing" in prompt:
                routing_prompt_captured = prompt
                return '{"action": "respond"}'
            return "Response"

        with patch.object(orchestrator_agent, "_call_llm", side_effect=capture_llm_call):
            # First query - no history
            events = []
            async for event in orchestrator_agent.run_streaming(
                user_message="List all namespaces",
                session_id="test-session",
                context=None,  # No history for first query
            ):
                events.append(event)

            assert routing_prompt_captured is not None
            assert "No previous conversation" in routing_prompt_captured
