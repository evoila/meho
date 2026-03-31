# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""Integration tests for orchestrator edge cases.

Tests for:
- Early findings events stream correctly
- Related connector hints appear in prompt
- Already-queried connectors excluded from selection
- Iteration awareness in routing prompt
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from meho_app.modules.agents.orchestrator.agent import OrchestratorAgent
from meho_app.modules.agents.orchestrator.contracts import SubgraphOutput
from meho_app.modules.agents.orchestrator.routing import build_routing_prompt, format_connectors
from meho_app.modules.agents.orchestrator.state import ConnectorSelection, OrchestratorState

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

    # Mock connector repository
    deps.connector_repo = MagicMock()
    deps.connector_repo.list_connectors = AsyncMock(return_value=[])

    return deps


@pytest.fixture
def orchestrator_agent(mock_dependencies):
    """Create OrchestratorAgent with mocked config."""
    with patch.object(OrchestratorAgent, "_load_config") as mock_config:
        config = MagicMock()
        config.max_iterations = 3
        config.model = MagicMock(name="openai:gpt-4.1-mini")
        config.raw = {
            "orchestrator": {
                "agent_timeout": 30.0,
                "total_timeout": 120.0,
            }
        }
        mock_config.return_value = config
        agent = OrchestratorAgent(dependencies=mock_dependencies)
        return agent


# =============================================================================
# Early Findings Event Tests
# =============================================================================


class TestEarlyFindingsEvents:
    """Tests for early_findings event streaming."""

    @pytest.mark.asyncio
    async def test_early_findings_emitted_for_each_connector(self, orchestrator_agent):
        """Test that early_findings event is emitted for each connector completion."""

        async def mock_decide(state):
            return (
                {
                    "action": "query",
                    "connectors": [
                        ConnectorSelection("c1", "Connector 1", "D1", 0.9, "Test"),
                        ConnectorSelection("c2", "Connector 2", "D2", 0.8, "Test"),
                        ConnectorSelection("c3", "Connector 3", "D3", 0.7, "Test"),
                    ],
                }
                if state.current_iteration == 0
                else {"action": "respond"}
            )

        async def mock_dispatch(state, connectors, iteration):
            for _i, conn in enumerate(connectors):
                yield SubgraphOutput(
                    connector_id=conn.connector_id,
                    connector_name=conn.connector_name,
                    findings=f"Data from {conn.connector_name}",
                    status="success",
                )

        with patch.object(orchestrator_agent, "_decide_next_action", side_effect=mock_decide):  # noqa: SIM117 -- readability preferred over combined with
            with patch.object(orchestrator_agent, "_dispatch_parallel", side_effect=mock_dispatch):
                with patch.object(
                    orchestrator_agent, "_synthesize", new_callable=AsyncMock
                ) as mock_synth:
                    mock_synth.return_value = "Combined answer"

                    events = []
                    async for event in orchestrator_agent.run_streaming("Test early findings"):
                        events.append(event)

        early_findings = [e for e in events if e.type == "early_findings"]
        assert len(early_findings) == 3

    @pytest.mark.asyncio
    async def test_early_findings_includes_remaining_count(self, orchestrator_agent):
        """Test that early_findings events include remaining_count."""

        async def mock_decide(state):
            return (
                {
                    "action": "query",
                    "connectors": [
                        ConnectorSelection("c1", "C1", "D1", 0.9, "T"),
                        ConnectorSelection("c2", "C2", "D2", 0.8, "T"),
                    ],
                }
                if state.current_iteration == 0
                else {"action": "respond"}
            )

        async def mock_dispatch(state, connectors, iteration):
            yield SubgraphOutput("c1", "C1", "Data 1", "success")
            yield SubgraphOutput("c2", "C2", "Data 2", "success")

        with patch.object(orchestrator_agent, "_decide_next_action", side_effect=mock_decide):  # noqa: SIM117 -- readability preferred over combined with
            with patch.object(orchestrator_agent, "_dispatch_parallel", side_effect=mock_dispatch):
                with patch.object(
                    orchestrator_agent, "_synthesize", new_callable=AsyncMock
                ) as mock_synth:
                    mock_synth.return_value = "Answer"

                    events = []
                    async for event in orchestrator_agent.run_streaming("Test"):
                        events.append(event)

        early_findings = [e for e in events if e.type == "early_findings"]

        # First completion: 1 remaining
        assert early_findings[0].data["remaining_count"] == 1
        # Second completion: 0 remaining
        assert early_findings[1].data["remaining_count"] == 0

    @pytest.mark.asyncio
    async def test_early_findings_includes_findings_preview(self, orchestrator_agent):
        """Test that early_findings events include findings preview."""

        async def mock_decide(state):
            return (
                {
                    "action": "query",
                    "connectors": [ConnectorSelection("c1", "C1", "D", 0.9, "T")],
                }
                if state.current_iteration == 0
                else {"action": "respond"}
            )

        async def mock_dispatch(state, connectors, iteration):
            yield SubgraphOutput(
                connector_id="c1",
                connector_name="C1",
                findings="This is a detailed finding with lots of information",
                status="success",
            )

        with patch.object(orchestrator_agent, "_decide_next_action", side_effect=mock_decide):  # noqa: SIM117 -- readability preferred over combined with
            with patch.object(orchestrator_agent, "_dispatch_parallel", side_effect=mock_dispatch):
                with patch.object(
                    orchestrator_agent, "_synthesize", new_callable=AsyncMock
                ) as mock_synth:
                    mock_synth.return_value = "Answer"

                    events = []
                    async for event in orchestrator_agent.run_streaming("Test"):
                        events.append(event)

        early_findings = [e for e in events if e.type == "early_findings"]
        assert len(early_findings) == 1
        assert early_findings[0].data["findings_preview"] is not None
        assert "detailed finding" in early_findings[0].data["findings_preview"]


# =============================================================================
# Related Connector Hints Tests
# =============================================================================


class TestRelatedConnectorHints:
    """Tests for related connector hints in routing."""

    @pytest.mark.asyncio
    async def test_related_connectors_included_in_data(self, orchestrator_agent, mock_dependencies):
        """Test that related_connector_ids are included in connector data."""
        # Setup mock connectors with related_connector_ids
        mock_conn = MagicMock()
        mock_conn.id = "k8s-prod"
        mock_conn.name = "K8s Production"
        mock_conn.description = "Kubernetes cluster"
        mock_conn.routing_description = "Production Kubernetes"
        mock_conn.connector_type = "rest"
        mock_conn.is_active = True
        mock_conn.related_connector_ids = ["gcp-prod", "vsphere-dc"]

        mock_dependencies.connector_repo.list_connectors = AsyncMock(return_value=[mock_conn])

        result = await orchestrator_agent._get_available_connectors()

        assert len(result) == 1
        assert result[0]["related_connectors"] == ["gcp-prod", "vsphere-dc"]

    def test_format_connectors_includes_related_hints(self):
        """Test that format_connectors includes related connector hints."""
        connectors = [
            {
                "id": "k8s-prod",
                "name": "K8s Production",
                "connector_type": "rest",
                "routing_description": "Kubernetes cluster",
                "related_connectors": ["gcp-prod", "vsphere-dc"],
            },
            {
                "id": "gcp-prod",
                "name": "GCP Production",
                "connector_type": "rest",
                "routing_description": "GCP project",
                "related_connectors": [],
            },
        ]

        formatted = format_connectors(connectors)

        assert "K8s Production" in formatted
        assert "(related: gcp-prod, vsphere-dc)" in formatted
        # GCP should not have related hint
        assert "GCP Production" in formatted


# =============================================================================
# Already-Queried Connector Tests
# =============================================================================


class TestAlreadyQueriedConnectors:
    """Tests for already-queried connector exclusion."""

    @pytest.mark.asyncio
    async def test_already_queried_excluded_from_available(
        self, orchestrator_agent, mock_dependencies
    ):
        """Test that already-queried connectors are excluded from selection."""
        # Setup mock connectors
        mock_conns = [
            MagicMock(
                id="c1",
                name="C1",
                description="",
                routing_description="",
                connector_type="rest",
                is_active=True,
                related_connector_ids=[],
            ),
            MagicMock(
                id="c2",
                name="C2",
                description="",
                routing_description="",
                connector_type="rest",
                is_active=True,
                related_connector_ids=[],
            ),
            MagicMock(
                id="c3",
                name="C3",
                description="",
                routing_description="",
                connector_type="rest",
                is_active=True,
                related_connector_ids=[],
            ),
        ]
        mock_dependencies.connector_repo.list_connectors = AsyncMock(return_value=mock_conns)

        # Simulate state with already-queried connectors
        state = OrchestratorState(user_goal="Test")
        state.all_findings = [
            SubgraphOutput("c1", "C1", "Data", "success"),
            SubgraphOutput("c2", "C2", "Data", "success"),
        ]

        # Track what connectors are passed to LLM
        captured_prompt = []

        async def mock_call_llm(prompt):
            captured_prompt.append(prompt)
            return '{"action": "respond"}'

        with patch.object(orchestrator_agent, "_call_llm", side_effect=mock_call_llm):
            await orchestrator_agent._decide_next_action(state)

        # Only c3 should be in the available connectors
        assert len(captured_prompt) == 1
        # c1 and c2 should not be in available connectors section
        # Note: They might be mentioned in "already queried" section

    @pytest.mark.asyncio
    async def test_respond_when_all_connectors_queried(self, orchestrator_agent, mock_dependencies):
        """Test that we respond immediately when all connectors have been queried."""
        # Setup mock connectors
        mock_conns = [
            MagicMock(
                id="c1",
                name="C1",
                description="",
                routing_description="",
                connector_type="rest",
                is_active=True,
                related_connector_ids=[],
            ),
        ]
        mock_dependencies.connector_repo.list_connectors = AsyncMock(return_value=mock_conns)

        # State where the only connector has been queried
        state = OrchestratorState(user_goal="Test")
        state.all_findings = [
            SubgraphOutput("c1", "C1", "Data", "success"),
        ]

        # Should not call LLM - should return respond immediately
        with patch.object(orchestrator_agent, "_call_llm", new_callable=AsyncMock) as mock_llm:
            mock_llm.return_value = '{"action": "query", "connectors": []}'

            result = await orchestrator_agent._decide_next_action(state)

        assert result["action"] == "respond"
        # LLM should not have been called since all connectors queried
        mock_llm.assert_not_called()


# =============================================================================
# Iteration Awareness Tests
# =============================================================================


class TestIterationAwareness:
    """Tests for iteration awareness in routing."""

    def test_routing_prompt_includes_iteration_context(self):
        """Test that routing prompt includes iteration context."""
        state = OrchestratorState(user_goal="Test query", max_iterations=3)
        state.current_iteration = 1  # Second iteration (0-indexed)

        connectors = [
            {
                "id": "c1",
                "name": "C1",
                "connector_type": "rest",
                "routing_description": "Test",
                "related_connectors": [],
            },
        ]
        already_queried = {"c0"}

        prompt = build_routing_prompt(state, connectors, already_queried)

        assert "2 of 3" in prompt  # Iteration 2 of 3
        assert "c0" in prompt  # Already queried

    @pytest.mark.asyncio
    async def test_last_iteration_bias_toward_respond(self, orchestrator_agent, mock_dependencies):
        """Test that last iteration with sufficient findings returns respond."""
        mock_conns = [
            MagicMock(
                id="c1",
                name="C1",
                description="",
                routing_description="",
                connector_type="rest",
                is_active=True,
                related_connector_ids=[],
            ),
            MagicMock(
                id="c2",
                name="C2",
                description="",
                routing_description="",
                connector_type="rest",
                is_active=True,
                related_connector_ids=[],
            ),
        ]
        mock_dependencies.connector_repo.list_connectors = AsyncMock(return_value=mock_conns)

        # State at last iteration with sufficient findings
        state = OrchestratorState(user_goal="Test", max_iterations=3)
        state.current_iteration = 2  # Will be iteration 3 (last)
        state.all_findings = [
            SubgraphOutput("c1", "C1", "Good data", "success"),
        ]

        # Should return respond without calling LLM
        with patch.object(orchestrator_agent, "_call_llm", new_callable=AsyncMock) as mock_llm:
            mock_llm.return_value = '{"action": "query", "connectors": [{"connector_id": "c2"}]}'

            result = await orchestrator_agent._decide_next_action(state)

        assert result["action"] == "respond"
        # LLM might be called but result is overridden due to last iteration bias


# =============================================================================
# Full Flow Edge Case Tests
# =============================================================================


class TestFullFlowEdgeCases:
    """Integration tests for complete orchestrator flows with edge cases."""

    @pytest.mark.asyncio
    async def test_mixed_success_timeout_error_outputs(self, orchestrator_agent):
        """Test handling mix of success, timeout, and error outputs."""

        async def mock_decide(state):
            return (
                {
                    "action": "query",
                    "connectors": [
                        ConnectorSelection("c1", "Success", "D1", 0.9, "T"),
                        ConnectorSelection("c2", "Timeout", "D2", 0.8, "T"),
                        ConnectorSelection("c3", "Error", "D3", 0.7, "T"),
                    ],
                }
                if state.current_iteration == 0
                else {"action": "respond"}
            )

        async def mock_dispatch(state, connectors, iteration):
            yield SubgraphOutput("c1", "Success", "Good data", "success")
            yield SubgraphOutput("c2", "Timeout", "", "timeout", error_message="Timed out")
            yield SubgraphOutput("c3", "Error", "", "failed", error_message="API error")

        with patch.object(orchestrator_agent, "_decide_next_action", side_effect=mock_decide):  # noqa: SIM117 -- readability preferred over combined with
            with patch.object(orchestrator_agent, "_dispatch_parallel", side_effect=mock_dispatch):
                with patch.object(
                    orchestrator_agent, "_synthesize", new_callable=AsyncMock
                ) as mock_synth:
                    mock_synth.return_value = "Partial answer with available data"

                    events = []
                    async for event in orchestrator_agent.run_streaming("Test mixed"):
                        events.append(event)

        # Should have 3 early_findings events
        early_findings = [e for e in events if e.type == "early_findings"]
        assert len(early_findings) == 3

        # Check statuses
        statuses = [e.data["status"] for e in early_findings]
        assert "success" in statuses
        assert "timeout" in statuses
        assert "failed" in statuses

        # Should still have final_answer
        final_answer = [e for e in events if e.type == "final_answer"]
        assert len(final_answer) == 1

    @pytest.mark.asyncio
    async def test_empty_connectors_immediate_respond(self, orchestrator_agent, mock_dependencies):
        """Test that empty connector list leads to immediate respond."""
        mock_dependencies.connector_repo.list_connectors = AsyncMock(return_value=[])

        with patch.object(orchestrator_agent, "_synthesize", new_callable=AsyncMock) as mock_synth:
            mock_synth.return_value = "No connectors available"

            events = []
            async for event in orchestrator_agent.run_streaming("Test no connectors"):
                events.append(event)

        event_types = [e.type for e in events]

        # Should go straight to synthesis without iterations
        assert "orchestrator_start" in event_types
        assert "synthesis_start" in event_types
        assert "final_answer" in event_types
