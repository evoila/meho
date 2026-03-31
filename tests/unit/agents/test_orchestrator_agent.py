# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""Unit tests for OrchestratorAgent.

Tests for:
- OrchestratorAgent initialization
- _decide_next_action() returns "respond" when sufficient info
- _decide_next_action() returns "query" with connector list
- _parse_decision() handles valid/invalid JSON
- _synthesize() combines findings correctly
- Max iterations limit is respected

Phase 84: OrchestratorAgent was completely rewritten with topology-driven routing
and ReAct loop. Methods like _parse_decision, _synthesize, _format_connectors no
longer exist. All tests in this module are skipped.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

pytestmark = pytest.mark.skip(reason="Phase 84: OrchestratorAgent rewritten with topology-driven routing, _parse_decision/_synthesize methods removed")

from meho_app.modules.agents.orchestrator.agent import OrchestratorAgent
from meho_app.modules.agents.orchestrator.contracts import SubgraphOutput
from meho_app.modules.agents.orchestrator.state import (
    ConnectorSelection,
    OrchestratorState,
)

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
def mock_connectors():
    """Sample connectors for testing."""
    return [
        {
            "id": "k8s-prod-123",
            "name": "Production Kubernetes",
            "description": "Kubernetes cluster for production workloads",
            "routing_description": "Kubernetes cluster hosting production pods and deployments",
            "connector_type": "rest",
        },
        {
            "id": "gcp-prod-456",
            "name": "Production GCP",
            "description": "GCP project for production infrastructure",
            "routing_description": "GCP VMs and cloud resources",
            "connector_type": "rest",
        },
        {
            "id": "vsphere-dc-789",
            "name": "vSphere Datacenter",
            "description": "VMware vSphere datacenter",
            "routing_description": "VMware virtual machines and ESXi hosts",
            "connector_type": "vmware",
        },
    ]


# =============================================================================
# Initialization Tests
# =============================================================================


class TestOrchestratorAgentInit:
    """Tests for OrchestratorAgent initialization."""

    def test_agent_name_is_orchestrator(self, orchestrator_agent):
        """Test that agent_name is set correctly."""
        assert orchestrator_agent.agent_name == "orchestrator"

    def test_has_dependencies(self, orchestrator_agent, mock_dependencies):
        """Test that dependencies are stored."""
        assert orchestrator_agent.dependencies is mock_dependencies

    def test_build_flow_returns_loop(self, orchestrator_agent):
        """Test build_flow returns orchestrator_loop."""
        assert orchestrator_agent.build_flow() == "orchestrator_loop"


# =============================================================================
# Decision Logic Tests
# =============================================================================


class TestDecisionLogic:
    """Tests for _decide_next_action and _parse_decision."""

    def test_parse_decision_valid_query_json(self, orchestrator_agent, mock_connectors):
        """Test parsing valid query decision JSON."""
        response = """
        Based on the user's question about pod status, I should query K8s.

        {
            "action": "query",
            "connectors": [
                {"connector_id": "k8s-prod-123", "connector_name": "Production Kubernetes", "reason": "User asked about pods"}
            ]
        }
        """

        decision = orchestrator_agent._parse_decision(response, mock_connectors)

        assert decision["action"] == "query"
        assert len(decision["connectors"]) == 1
        assert isinstance(decision["connectors"][0], ConnectorSelection)
        assert decision["connectors"][0].connector_id == "k8s-prod-123"

    def test_parse_decision_valid_respond_json(self, orchestrator_agent, mock_connectors):
        """Test parsing valid respond decision JSON."""
        response = """
        I have enough information to answer the question.

        {"action": "respond"}
        """

        decision = orchestrator_agent._parse_decision(response, mock_connectors)

        assert decision["action"] == "respond"

    def test_parse_decision_invalid_json_defaults_to_respond(
        self, orchestrator_agent, mock_connectors
    ):
        """Test that invalid JSON defaults to respond (safe fallback)."""
        response = "This is not valid JSON at all, just free text."

        decision = orchestrator_agent._parse_decision(response, mock_connectors)

        assert decision["action"] == "respond"

    def test_parse_decision_malformed_json_defaults_to_respond(
        self, orchestrator_agent, mock_connectors
    ):
        """Test that malformed JSON defaults to respond."""
        response = '{"action": "query", "connectors": [invalid json here]}'

        decision = orchestrator_agent._parse_decision(response, mock_connectors)

        assert decision["action"] == "respond"

    def test_parse_decision_unknown_connector_id_filtered(
        self, orchestrator_agent, mock_connectors
    ):
        """Test that unknown connector IDs are filtered out."""
        response = """
        {
            "action": "query",
            "connectors": [
                {"connector_id": "unknown-connector-999", "reason": "Testing"},
                {"connector_id": "k8s-prod-123", "reason": "Valid"}
            ]
        }
        """

        decision = orchestrator_agent._parse_decision(response, mock_connectors)

        assert decision["action"] == "query"
        assert len(decision["connectors"]) == 1
        assert decision["connectors"][0].connector_id == "k8s-prod-123"

    def test_parse_decision_extracts_json_from_middle_of_text(
        self, orchestrator_agent, mock_connectors
    ):
        """Test JSON extraction from middle of LLM response."""
        response = """
        Let me think about this...

        The user is asking about VMs, so I should query vSphere.

        {"action": "query", "connectors": [{"connector_id": "vsphere-dc-789", "reason": "VM question"}]}

        That should get us the information we need.
        """

        decision = orchestrator_agent._parse_decision(response, mock_connectors)

        assert decision["action"] == "query"
        assert len(decision["connectors"]) == 1
        assert decision["connectors"][0].connector_id == "vsphere-dc-789"

    def test_find_connector_returns_correct_connector(self, orchestrator_agent, mock_connectors):
        """Test _find_connector helper method."""
        result = orchestrator_agent._find_connector(mock_connectors, "gcp-prod-456")

        assert result is not None
        assert result["name"] == "Production GCP"

    def test_find_connector_returns_none_for_unknown(self, orchestrator_agent, mock_connectors):
        """Test _find_connector returns None for unknown ID."""
        result = orchestrator_agent._find_connector(mock_connectors, "unknown-id")

        assert result is None


# =============================================================================
# Prompt Building Tests
# =============================================================================


class TestPromptBuilding:
    """Tests for prompt building methods."""

    def test_format_connectors(self, orchestrator_agent, mock_connectors):
        """Test connector formatting for prompts."""
        formatted = orchestrator_agent._format_connectors(mock_connectors)

        assert "Production Kubernetes" in formatted
        assert "k8s-prod-123" in formatted
        assert "Kubernetes cluster hosting production pods" in formatted
        assert "Production GCP" in formatted
        assert "vSphere Datacenter" in formatted

    def test_build_agent_prompt_includes_goal(self, orchestrator_agent):
        """Test agent prompt includes user goal."""
        state = OrchestratorState(user_goal="Why is my application slow?")
        connector = ConnectorSelection(
            connector_id="k8s-prod",
            connector_name="K8s Production",
            routing_description="Kubernetes cluster",
            relevance_score=0.9,
            reason="Test",
        )

        prompt = orchestrator_agent._build_agent_prompt(state, connector)

        assert "Why is my application slow?" in prompt
        assert "K8s Production" in prompt

    def test_build_agent_prompt_includes_prior_findings(self, orchestrator_agent):
        """Test agent prompt includes prior findings from other connectors."""
        state = OrchestratorState(user_goal="Check system health")
        state.all_findings = [
            SubgraphOutput(
                connector_id="gcp-prod",
                connector_name="GCP Production",
                findings="Found 3 VMs with high CPU",
                status="success",
            ),
        ]

        connector = ConnectorSelection(
            connector_id="k8s-prod",
            connector_name="K8s Production",
            routing_description="Kubernetes cluster",
            relevance_score=0.9,
            reason="Test",
        )

        prompt = orchestrator_agent._build_agent_prompt(state, connector)

        assert "Found 3 VMs with high CPU" in prompt
        assert "GCP Production" in prompt


# =============================================================================
# Get Available Connectors Tests
# =============================================================================


class TestGetAvailableConnectors:
    """Tests for _get_available_connectors method."""

    @pytest.mark.asyncio
    async def test_returns_active_connectors(self, orchestrator_agent, mock_dependencies):
        """Test that only active connectors are returned."""
        # Create mocks with spec'd attributes to avoid MagicMock issues
        active_conn = MagicMock()
        active_conn.id = "conn-1"
        active_conn.name = "Active Connector"
        active_conn.description = "Active"
        active_conn.routing_description = "Active connector description"
        active_conn.connector_type = "rest"
        active_conn.is_active = True

        inactive_conn = MagicMock()
        inactive_conn.id = "conn-2"
        inactive_conn.name = "Inactive Connector"
        inactive_conn.description = "Inactive"
        inactive_conn.routing_description = "Inactive"
        inactive_conn.connector_type = "rest"
        inactive_conn.is_active = False

        mock_dependencies.connector_repo.list_connectors = AsyncMock(
            return_value=[active_conn, inactive_conn]
        )

        result = await orchestrator_agent._get_available_connectors()

        assert len(result) == 1
        assert result[0]["name"] == "Active Connector"

    @pytest.mark.asyncio
    async def test_handles_repo_error_gracefully(self, orchestrator_agent, mock_dependencies):
        """Test that repo errors return empty list."""
        mock_dependencies.connector_repo.list_connectors = AsyncMock(
            side_effect=Exception("Database error")
        )

        result = await orchestrator_agent._get_available_connectors()

        assert result == []


# =============================================================================
# Synthesis Tests
# =============================================================================


class TestSynthesis:
    """Tests for _synthesize method."""

    @pytest.mark.asyncio
    async def test_synthesize_with_no_findings(self, orchestrator_agent):
        """Test synthesis with no findings."""
        state = OrchestratorState(user_goal="Test question")

        result = await orchestrator_agent._synthesize(state)

        assert "unable to gather" in result.lower() or "no" in result.lower()

    @pytest.mark.asyncio
    async def test_synthesize_calls_llm(self, orchestrator_agent):
        """Test that synthesis calls LLM with correct prompt."""
        state = OrchestratorState(user_goal="Why is the app slow?")
        state.all_findings = [
            SubgraphOutput(
                connector_id="k8s-prod",
                connector_name="K8s Prod",
                findings="Found pods with high memory usage",
                status="success",
            ),
        ]

        with patch.object(orchestrator_agent, "_call_llm", new_callable=AsyncMock) as mock_llm:
            mock_llm.return_value = "Based on the findings, high memory usage is the cause."

            await orchestrator_agent._synthesize(state)

            mock_llm.assert_called_once()
            call_args = mock_llm.call_args[0][0]
            assert "Why is the app slow?" in call_args or "app slow" in call_args.lower()
            assert "memory" in call_args.lower() or "K8s Prod" in call_args


# =============================================================================
# Max Iterations Tests
# =============================================================================


class TestMaxIterations:
    """Tests for max iterations handling."""

    @pytest.mark.asyncio
    async def test_respects_max_iterations(self, orchestrator_agent, mock_dependencies):
        """Test that agent respects max_iterations limit."""
        # Setup: make _decide_next_action always return query
        with patch.object(
            orchestrator_agent, "_decide_next_action", new_callable=AsyncMock
        ) as mock_decide:
            mock_decide.return_value = {
                "action": "query",
                "connectors": [
                    ConnectorSelection(
                        connector_id="test",
                        connector_name="Test",
                        routing_description="Test",
                        relevance_score=0.8,
                        reason="Test",
                    ),
                ],
            }

            # Mock _dispatch_parallel to return immediately
            with patch.object(orchestrator_agent, "_dispatch_parallel") as mock_dispatch:

                async def mock_dispatch_gen(*args, **kwargs):
                    yield SubgraphOutput(
                        connector_id="test",
                        connector_name="Test",
                        findings="Test finding",
                        status="success",
                    )

                mock_dispatch.return_value = mock_dispatch_gen()

                # Mock _synthesize
                with patch.object(
                    orchestrator_agent, "_synthesize", new_callable=AsyncMock
                ) as mock_synth:
                    mock_synth.return_value = "Final answer"

                    events = []
                    async for event in orchestrator_agent.run_streaming("Test"):
                        events.append(event)

                    # Should have max_iterations iteration_start events
                    iteration_starts = [e for e in events if e.type == "iteration_start"]
                    # The actual number depends on the mock config's max_iterations
                    # Since we mocked _load_config with max_iterations=3
                    assert len(iteration_starts) <= 3


# =============================================================================
# LLM Call Tests
# =============================================================================


class TestLLMCall:
    """Tests for _call_llm method."""

    @pytest.mark.asyncio
    async def test_call_llm_uses_pydantic_ai(self, orchestrator_agent):
        """Test that _call_llm uses pydantic_ai.Agent."""
        with patch("pydantic_ai.Agent") as mock_agent_class:
            mock_agent = MagicMock()
            mock_result = MagicMock()
            mock_result.data = "LLM response"
            mock_agent.run = AsyncMock(return_value=mock_result)
            mock_agent_class.return_value = mock_agent

            result = await orchestrator_agent._call_llm("Test prompt")

            assert result == "LLM response"
            mock_agent_class.assert_called_once()
            mock_agent.run.assert_called_once_with("Test prompt")

    @pytest.mark.asyncio
    async def test_call_llm_raises_on_error(self, orchestrator_agent):
        """Test that _call_llm raises on LLM error."""
        with patch("pydantic_ai.Agent") as mock_agent_class:
            mock_agent = MagicMock()
            mock_agent.run = AsyncMock(side_effect=Exception("LLM error"))
            mock_agent_class.return_value = mock_agent

            with pytest.raises(Exception, match="LLM error"):
                await orchestrator_agent._call_llm("Test prompt")


# =============================================================================
# State Management Tests
# =============================================================================


class TestStateManagement:
    """Tests for orchestrator state management during run_streaming."""

    @pytest.mark.asyncio
    async def test_emits_orchestrator_start_event(self, orchestrator_agent, mock_dependencies):
        """Test that orchestrator_start event is emitted."""
        # Make it respond immediately
        with patch.object(
            orchestrator_agent, "_decide_next_action", new_callable=AsyncMock
        ) as mock_decide:
            mock_decide.return_value = {"action": "respond"}

            with patch.object(
                orchestrator_agent, "_synthesize", new_callable=AsyncMock
            ) as mock_synth:
                mock_synth.return_value = "Answer"

                events = []
                async for event in orchestrator_agent.run_streaming("Test"):
                    events.append(event)

                event_types = [e.type for e in events]
                assert "orchestrator_start" in event_types

    @pytest.mark.asyncio
    async def test_emits_orchestrator_complete_event(self, orchestrator_agent, mock_dependencies):
        """Test that orchestrator_complete event is emitted."""
        with patch.object(
            orchestrator_agent, "_decide_next_action", new_callable=AsyncMock
        ) as mock_decide:
            mock_decide.return_value = {"action": "respond"}

            with patch.object(
                orchestrator_agent, "_synthesize", new_callable=AsyncMock
            ) as mock_synth:
                mock_synth.return_value = "Answer"

                events = []
                async for event in orchestrator_agent.run_streaming("Test"):
                    events.append(event)

                event_types = [e.type for e in events]
                assert "orchestrator_complete" in event_types

    @pytest.mark.asyncio
    async def test_emits_final_answer_event(self, orchestrator_agent, mock_dependencies):
        """Test that final_answer event is emitted with content."""
        with patch.object(
            orchestrator_agent, "_decide_next_action", new_callable=AsyncMock
        ) as mock_decide:
            mock_decide.return_value = {"action": "respond"}

            with patch.object(
                orchestrator_agent, "_synthesize", new_callable=AsyncMock
            ) as mock_synth:
                mock_synth.return_value = "This is the final answer."

                events = []
                async for event in orchestrator_agent.run_streaming("Test"):
                    events.append(event)

                final_answer_events = [e for e in events if e.type == "final_answer"]
                assert len(final_answer_events) == 1
                assert final_answer_events[0].data["content"] == "This is the final answer."
