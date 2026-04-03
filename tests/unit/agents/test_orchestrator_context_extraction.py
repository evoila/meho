# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""Unit tests for OrchestratorAgent operation context extraction (Phase 5 - TASK-185).

Tests for:
- _extract_operation_context() extracts operation and entities from LLM
- _format_findings_for_context() truncates long findings
- _parse_context_extraction() handles JSON parsing and edge cases
- Context extraction is called after synthesis in run_streaming()
- Session state is updated with extracted context
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from meho_app.modules.agents.orchestrator.agent import OrchestratorAgent
from meho_app.modules.agents.orchestrator.contracts import SubgraphOutput
from meho_app.modules.agents.orchestrator.state import OrchestratorState
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
def sample_findings():
    """Create sample SubgraphOutput list for testing."""
    return [
        SubgraphOutput(
            connector_id="k8s-prod-123",
            connector_name="K8s Production",
            findings="Found 15 pods in default namespace: nginx-pod, api-pod, db-pod...",
            status="success",
        ),
        SubgraphOutput(
            connector_id="vmware-001",
            connector_name="VMware DC",
            findings="Found 5 VMs on host esxi-05: web-server-01, db-master...",
            status="success",
        ),
    ]


@pytest.fixture
def session_state():
    """Create empty session state for testing."""
    return OrchestratorSessionState()


# =============================================================================
# Tests for _parse_context_extraction
# =============================================================================


class TestParseContextExtraction:
    """Test _parse_context_extraction method."""

    def test_parses_valid_json(self, orchestrator_agent):
        """Test parsing valid JSON response."""
        response = (
            '{"operation": "Listing pods in namespace", "entities": ["nginx-pod", "default"]}'
        )
        operation, entities = orchestrator_agent._parse_context_extraction(response)

        assert operation == "Listing pods in namespace"
        assert entities == ["nginx-pod", "default"]

    def test_extracts_json_from_markdown(self, orchestrator_agent):
        """Test extracting JSON from markdown code block."""
        response = """Here's the extracted context:
```json
{"operation": "Debug pod restarts", "entities": ["api-pod"]}
```
"""
        operation, entities = orchestrator_agent._parse_context_extraction(response)

        assert operation == "Debug pod restarts"
        assert entities == ["api-pod"]

    def test_handles_json_with_surrounding_text(self, orchestrator_agent):
        """Test extracting JSON from response with extra text."""
        response = 'Based on the conversation: {"operation": "Checking VM status", "entities": ["web-01"]} Done.'
        operation, entities = orchestrator_agent._parse_context_extraction(response)

        assert operation == "Checking VM status"
        assert entities == ["web-01"]

    def test_handles_empty_entities(self, orchestrator_agent):
        """Test handling response with no entities."""
        response = '{"operation": "General health check", "entities": []}'
        operation, entities = orchestrator_agent._parse_context_extraction(response)

        assert operation == "General health check"
        assert entities == []

    def test_handles_missing_entities_key(self, orchestrator_agent):
        """Test handling response missing entities key."""
        response = '{"operation": "Some operation"}'
        operation, entities = orchestrator_agent._parse_context_extraction(response)

        assert operation == "Some operation"
        assert entities == []

    def test_handles_null_operation(self, orchestrator_agent):
        """Test handling null operation."""
        response = '{"operation": null, "entities": ["test"]}'
        operation, entities = orchestrator_agent._parse_context_extraction(response)

        assert operation is None
        assert entities == ["test"]

    def test_handles_malformed_json(self, orchestrator_agent):
        """Test graceful handling of malformed JSON."""
        response = '{"operation": "incomplete json'
        operation, entities = orchestrator_agent._parse_context_extraction(response)

        assert operation is None
        assert entities == []

    def test_handles_no_json_in_response(self, orchestrator_agent):
        """Test handling response with no JSON."""
        response = "I couldn't extract any meaningful context from this query."
        operation, entities = orchestrator_agent._parse_context_extraction(response)

        assert operation is None
        assert entities == []

    def test_truncates_long_operation(self, orchestrator_agent):
        """Test that very long operation descriptions are truncated."""
        long_operation = "x" * 300
        response = f'{{"operation": "{long_operation}", "entities": []}}'
        operation, _entities = orchestrator_agent._parse_context_extraction(response)

        assert operation is not None
        assert len(operation) <= 203  # 200 chars + "..."
        assert operation.endswith("...")

    def test_filters_non_string_entities(self, orchestrator_agent):
        """Test that non-string entities are converted or filtered."""
        response = '{"operation": "Test", "entities": ["valid", 123, null, "also-valid"]}'
        operation, entities = orchestrator_agent._parse_context_extraction(response)

        assert operation == "Test"
        assert "valid" in entities
        assert "also-valid" in entities
        assert "123" in entities  # Converted to string
        assert len(entities) == 3  # null filtered out


# =============================================================================
# Tests for _format_findings_for_context
# =============================================================================


class TestFormatFindingsForContext:
    """Test _format_findings_for_context method."""

    def test_formats_findings(self, orchestrator_agent, sample_findings):
        """Test basic findings formatting."""
        result = orchestrator_agent._format_findings_for_context(sample_findings)

        assert "[K8s Production]" in result
        assert "[VMware DC]" in result
        assert "nginx-pod" in result

    def test_handles_empty_findings(self, orchestrator_agent):
        """Test handling empty findings list."""
        result = orchestrator_agent._format_findings_for_context([])

        assert result == "No findings from connectors."

    def test_skips_failed_findings(self, orchestrator_agent):
        """Test that failed findings are skipped."""
        findings = [
            SubgraphOutput(
                connector_id="c1",
                connector_name="Failed Connector",
                findings="",
                status="failed",
                error_message="Connection error",
            ),
            SubgraphOutput(
                connector_id="c2",
                connector_name="Good Connector",
                findings="Valid findings here",
                status="success",
            ),
        ]

        result = orchestrator_agent._format_findings_for_context(findings)

        assert "Failed Connector" not in result
        assert "Good Connector" in result
        assert "Valid findings" in result

    def test_truncates_long_individual_finding(self, orchestrator_agent):
        """Test that individual long findings are truncated."""
        long_finding = "x" * 500
        findings = [
            SubgraphOutput(
                connector_id="c1",
                connector_name="Connector",
                findings=long_finding,
                status="success",
            )
        ]

        result = orchestrator_agent._format_findings_for_context(findings)

        # Should truncate to 300 chars + "..."
        assert len(result) < len(long_finding) + 50
        assert "..." in result

    def test_respects_max_chars_limit(self, orchestrator_agent):
        """Test that total output respects max_chars limit."""
        findings = [
            SubgraphOutput(
                connector_id=f"c{i}",
                connector_name=f"Connector {i}",
                findings="A" * 200,
                status="success",
            )
            for i in range(10)
        ]

        result = orchestrator_agent._format_findings_for_context(findings, max_chars=500)

        # Should be under limit (with some tolerance for truncation)
        assert len(result) < 600

    def test_returns_no_successful_findings_message(self, orchestrator_agent):
        """Test message when all findings failed."""
        findings = [
            SubgraphOutput(
                connector_id="c1",
                connector_name="Connector",
                findings="",
                status="timeout",
            )
        ]

        result = orchestrator_agent._format_findings_for_context(findings)

        assert result == "No successful findings."


# =============================================================================
# Tests for _extract_operation_context
# =============================================================================


class TestExtractOperationContext:
    """Test _extract_operation_context method."""

    @pytest.mark.asyncio
    async def test_extracts_context_from_llm(self, orchestrator_agent, sample_findings):
        """Test successful context extraction."""
        orchestrator_agent._call_llm = AsyncMock(
            return_value='{"operation": "Listing pods in K8s", "entities": ["nginx-pod", "default"]}'
        )

        operation, entities = await orchestrator_agent._extract_operation_context(
            user_message="List all pods in the default namespace",
            findings=sample_findings,
        )

        assert operation == "Listing pods in K8s"
        assert "nginx-pod" in entities
        assert "default" in entities

    @pytest.mark.asyncio
    async def test_handles_llm_failure(self, orchestrator_agent, sample_findings):
        """Test graceful handling of LLM call failure."""
        orchestrator_agent._call_llm = AsyncMock(side_effect=Exception("LLM API error"))

        operation, entities = await orchestrator_agent._extract_operation_context(
            user_message="Test message",
            findings=sample_findings,
        )

        assert operation is None
        assert entities == []

    @pytest.mark.asyncio
    async def test_handles_empty_findings(self, orchestrator_agent):
        """Test extraction with empty findings."""
        orchestrator_agent._call_llm = AsyncMock(
            return_value='{"operation": "General query", "entities": []}'
        )

        operation, entities = await orchestrator_agent._extract_operation_context(
            user_message="Hello",
            findings=[],
        )

        assert operation == "General query"
        assert entities == []

    @pytest.mark.asyncio
    async def test_prompt_includes_user_message(self, orchestrator_agent, sample_findings):
        """Test that user message is included in prompt."""
        orchestrator_agent._call_llm = AsyncMock(
            return_value='{"operation": "Test", "entities": []}'
        )

        await orchestrator_agent._extract_operation_context(
            user_message="Show me pods in production",
            findings=sample_findings,
        )

        # Verify the prompt contains the user message
        call_args = orchestrator_agent._call_llm.call_args[0][0]
        assert "Show me pods in production" in call_args

    @pytest.mark.asyncio
    async def test_prompt_includes_findings(self, orchestrator_agent, sample_findings):
        """Test that findings are included in prompt."""
        orchestrator_agent._call_llm = AsyncMock(
            return_value='{"operation": "Test", "entities": []}'
        )

        await orchestrator_agent._extract_operation_context(
            user_message="Test",
            findings=sample_findings,
        )

        # Verify the prompt contains findings info
        call_args = orchestrator_agent._call_llm.call_args[0][0]
        assert "K8s Production" in call_args


# =============================================================================
# Tests for run_streaming context extraction integration
# =============================================================================


class TestRunStreamingContextExtraction:
    """Test context extraction integration in run_streaming."""

    @pytest.mark.asyncio
    async def test_context_extracted_after_synthesis(self, orchestrator_agent, session_state):
        """Test that context is extracted after synthesis."""
        # Track if extraction was called
        extraction_called = False

        def mock_extract(*args, **kwargs):
            nonlocal extraction_called
            extraction_called = True
            return "Test operation", ["entity1"]

        orchestrator_agent._extract_operation_context = mock_extract
        orchestrator_agent._decide_next_action = AsyncMock(return_value={"action": "respond"})
        orchestrator_agent._synthesize = AsyncMock(return_value="Final answer")
        orchestrator_agent._get_available_connectors = AsyncMock(return_value=[])

        # Collect events
        events = []
        async for event in orchestrator_agent.run_streaming(
            user_message="List pods",
            session_id="test-session",
            context={"session_state": session_state},
        ):
            events.append(event)

        # Verify extraction was NOT called (no findings = no extraction)
        # because _decide_next_action returned "respond" without querying
        assert not extraction_called

    @pytest.mark.asyncio
    async def test_session_state_updated_with_context(self, orchestrator_agent, session_state):
        """Test that session state is updated with extracted context."""
        from meho_app.modules.agents.orchestrator.contracts import SubgraphOutput

        # Mock to simulate findings
        orchestrator_agent._decide_next_action = AsyncMock(
            side_effect=[
                {
                    "action": "query",
                    "connectors": [
                        MagicMock(
                            connector_id="k8s-1",
                            connector_name="K8s",
                            connector_type="kubernetes",
                            routing_description="K8s cluster",
                        )
                    ],
                },
                {"action": "respond"},
            ]
        )
        orchestrator_agent._synthesize = AsyncMock(return_value="Found pods")
        orchestrator_agent._extract_operation_context = AsyncMock(
            return_value=("Listing K8s pods", ["nginx", "default"])
        )
        orchestrator_agent._get_available_connectors = AsyncMock(return_value=[])

        # Mock dispatch to return findings
        async def mock_dispatch(state, connectors, iteration):
            yield SubgraphOutput(
                connector_id="k8s-1",
                connector_name="K8s",
                findings="Found pods",
                status="success",
            )

        orchestrator_agent._dispatch_parallel = mock_dispatch

        # Run streaming
        events = []
        async for event in orchestrator_agent.run_streaming(
            user_message="List pods",
            session_id="test-session",
            context={"session_state": session_state},
        ):
            events.append(event)

        # Verify session state was updated
        assert session_state.current_operation == "Listing K8s pods"
        assert "nginx" in session_state.operation_entities
        assert "default" in session_state.operation_entities

    @pytest.mark.asyncio
    async def test_context_not_extracted_without_session_state(self, orchestrator_agent):
        """Test that context extraction is skipped without session state."""
        orchestrator_agent._decide_next_action = AsyncMock(return_value={"action": "respond"})
        orchestrator_agent._synthesize = AsyncMock(return_value="Answer")
        orchestrator_agent._extract_operation_context = AsyncMock()

        # No session_state in context
        events = []
        async for event in orchestrator_agent.run_streaming(
            user_message="Test",
            session_id="test",
            context={"history": ""},
        ):
            events.append(event)

        # Extraction should not be called
        orchestrator_agent._extract_operation_context.assert_not_called()

    @pytest.mark.asyncio
    async def test_extraction_failure_does_not_break_streaming(
        self, orchestrator_agent, session_state
    ):
        """Test that extraction failure doesn't break the response."""
        from meho_app.modules.agents.orchestrator.contracts import SubgraphOutput

        orchestrator_agent._decide_next_action = AsyncMock(
            side_effect=[
                {
                    "action": "query",
                    "connectors": [
                        MagicMock(
                            connector_id="k8s-1",
                            connector_name="K8s",
                            connector_type="kubernetes",
                            routing_description="",
                        )
                    ],
                },
                {"action": "respond"},
            ]
        )
        orchestrator_agent._synthesize = AsyncMock(return_value="Success")
        orchestrator_agent._extract_operation_context = AsyncMock(
            side_effect=Exception("Extraction failed")
        )
        orchestrator_agent._get_available_connectors = AsyncMock(return_value=[])

        async def mock_dispatch(state, connectors, iteration):
            yield SubgraphOutput(
                connector_id="k8s-1",
                connector_name="K8s",
                findings="Data",
                status="success",
            )

        orchestrator_agent._dispatch_parallel = mock_dispatch

        # Should complete without error
        events = []
        async for event in orchestrator_agent.run_streaming(
            user_message="Test",
            session_id="test",
            context={"session_state": session_state},
        ):
            events.append(event)

        # Should have completed successfully
        event_types = [e.type for e in events]
        assert "final_answer" in event_types
        assert "orchestrator_complete" in event_types


# =============================================================================
# Tests for context usage in subsequent turns
# =============================================================================


class TestContextUsageInDecisions:
    """Test that extracted context is used in subsequent turn decisions."""

    @pytest.mark.asyncio
    async def test_context_appears_in_routing_prompt(self, orchestrator_agent):
        """Test that saved context appears in routing prompt for next turn."""
        # Create session state with prior context
        session_state = OrchestratorSessionState()
        session_state.turn_count = 1
        session_state.set_operation_context(
            "Debugging nginx pod restarts", ["nginx-pod", "production"]
        )

        state = OrchestratorState(
            user_goal="Show me more details",
            session_state=session_state,
        )

        orchestrator_agent._call_llm = AsyncMock(return_value='{"action": "respond"}')
        # Need at least one connector for LLM to be called
        orchestrator_agent._get_available_connectors = AsyncMock(
            return_value=[
                {
                    "id": "k8s-1",
                    "name": "K8s Prod",
                    "connector_type": "kubernetes",
                    "routing_description": "Production Kubernetes cluster",
                    "description": "K8s cluster",
                }
            ]
        )

        await orchestrator_agent._decide_next_action(state)

        # Verify context was included in prompt
        call_args = orchestrator_agent._call_llm.call_args[0][0]
        assert "Debugging nginx pod restarts" in call_args
        assert "nginx-pod" in call_args

    def test_session_context_includes_operation(self, orchestrator_agent):
        """Test that _build_session_context includes operation."""
        session_state = OrchestratorSessionState()
        session_state.turn_count = 1
        session_state.set_operation_context("Investigating pod crashes", ["api-pod"])

        result = orchestrator_agent._build_session_context(session_state)

        assert "Investigating pod crashes" in result
        assert "api-pod" in result
        assert "User's Current Focus" in result
        assert "Key Entities" in result
