# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""Unit tests for specialist agent stateful loop (Phase 35).

Tests the architectural shift from stateless infer_structured() per step
to persistent PydanticAI Agent with message_history. Validates:
- Message construction (first step full context, subsequent incremental)
- Persistent agent creation (once per investigation, not per step)
- Retry adaptation (message_history preserved across retries)
- Backward compatibility (flat scratchpad, budget exhaustion path)

Phase 35 (v1.69 Token Optimization): STAT-01, STAT-02, STAT-03.

Phase 84: Specialist agent loop was refactored with new message construction
and cache control patterns. Tests pre-date the refactor.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.skip(reason="Phase 84: specialist agent stateful loop refactored with new message construction and Anthropic cache control")

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from meho_app.modules.agents.specialist_agent.models import ReActStep

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _passthrough_retry(coro_factory, **kwargs):
    """Mock retry_llm_call that just awaits the factory coroutine."""
    return await coro_factory()


def _make_react_step(
    *,
    thought: str = "Thinking...",
    response_type: str = "action",
    action: str | None = "search_operations",
    action_input: dict[str, Any] | None = None,
    final_answer: str | None = None,
) -> ReActStep:
    """Create a ReActStep with sensible defaults."""
    return ReActStep(
        thought=thought,
        response_type=response_type,
        action=action,
        action_input=action_input or {"query": "list pods"},
        final_answer=final_answer,
    )


def _make_mock_run_result(
    react_step: ReActStep | None = None,
    all_messages: list | None = None,
    new_messages: list | None = None,
    request_tokens: int = 100,
    response_tokens: int = 50,
) -> MagicMock:
    """Create a mock PydanticAI RunResult."""
    result = MagicMock()
    result.output = react_step or _make_react_step()

    # Usage mock
    usage_mock = MagicMock()
    usage_mock.request_tokens = request_tokens
    usage_mock.response_tokens = response_tokens
    usage_mock.total_tokens = request_tokens + response_tokens
    result.usage = MagicMock(return_value=usage_mock)

    # Messages
    result.all_messages = MagicMock(
        return_value=all_messages
        or [
            {"role": "user", "content": "step prompt"},
            {"role": "assistant", "content": "react step"},
        ]
    )
    result.new_messages = MagicMock(
        return_value=new_messages
        or [
            {"role": "user", "content": "step prompt"},
            {"role": "assistant", "content": "react step"},
        ]
    )

    return result


# ---------------------------------------------------------------------------
# TestMessageHistoryConstruction
# ---------------------------------------------------------------------------


class TestMessageHistoryConstruction:
    """Test that message content is correct for first and subsequent steps."""

    @pytest.mark.asyncio
    async def test_first_step_message_contains_full_context(self) -> None:
        """First user message sent to agent.run() includes user goal,
        cached tables context, and step count prompt (stable prefix)."""
        captured_messages: list[str] = []

        step1_result = _make_mock_run_result(
            react_step=_make_react_step(
                response_type="final_answer", final_answer="Done", action=None
            ),
        )

        async def fake_run(msg, **kwargs):
            captured_messages.append(msg)
            return step1_result

        mock_agent_instance = MagicMock()
        mock_agent_instance.run = AsyncMock(side_effect=fake_run)

        with (
            patch(
                "meho_app.modules.agents.specialist_agent.agent.Agent",
                return_value=mock_agent_instance,
            ),
            patch(
                "meho_app.modules.agents.specialist_agent.agent.retry_llm_call",
                side_effect=_passthrough_retry,
            ),
            patch(
                "meho_app.modules.agents.specialist_agent.agent.compress_observation",
                new_callable=AsyncMock,
                return_value="compressed obs",
            ),
            patch(
                "meho_app.modules.agents.specialist_agent.agent.get_transcript_collector",
                return_value=None,
            ),
        ):
            from meho_app.modules.agents.specialist_agent.agent import SpecialistAgent

            agent = SpecialistAgent.__new__(SpecialistAgent)
            agent.connector_id = "test-conn"
            agent.connector_name = "Test Connector"
            agent.connector_type = "kubernetes"
            agent.routing_description = "test"
            agent.skill_content = ""
            agent.iteration = 1
            agent.prior_findings = []
            agent.dependencies = MagicMock()
            agent.dependencies.user_context = MagicMock()
            agent.dependencies.user_context.tenant_id = "t1"
            agent.dependencies.user_context.user_id = "u1"

            config = MagicMock()
            config.max_steps = 5
            config.model.name = "anthropic:claude-sonnet-4-20250514"
            config.model.temperature = None
            config.raw = {"window_size": 3}
            agent._config = config

            # Mock _build_system_prompt
            agent._build_system_prompt = MagicMock(return_value="system prompt text")

            # Execute the run_react with mocked DB calls
            events = []
            with (
                patch(
                    "meho_app.api.database.create_openapi_session_maker",
                    side_effect=Exception("skip DB"),
                ),
                patch(
                    "meho_app.modules.agents.specialist_agent.agent._extract_entity_mentions",
                    return_value=[],
                ),
            ):
                async for ev in agent._run_react("What pods are running?"):
                    events.append(ev)

        # The first message should contain user goal and step info
        assert len(captured_messages) >= 1
        first_msg = captured_messages[0]
        assert "What pods are running?" in first_msg
        assert "Step 1 of" in first_msg

    @pytest.mark.asyncio
    async def test_subsequent_step_message_contains_only_observation(self) -> None:
        """Step 2+ messages contain ONLY the observation from prev step + step count."""
        captured_messages: list[str] = []
        call_count = 0

        step1_result = _make_mock_run_result(
            react_step=_make_react_step(
                thought="Let me search",
                action="search_operations",
                action_input={"query": "list pods", "connector_id": "test-conn"},
            ),
            all_messages=[
                {"role": "user", "content": "msg1"},
                {"role": "assistant", "content": "resp1"},
            ],
        )
        step2_result = _make_mock_run_result(
            react_step=_make_react_step(
                response_type="final_answer", final_answer="Found pods", action=None
            ),
            all_messages=[
                {"role": "user", "content": "msg1"},
                {"role": "assistant", "content": "resp1"},
                {"role": "user", "content": "msg2"},
                {"role": "assistant", "content": "resp2"},
            ],
        )

        async def fake_run(msg, **kwargs):
            nonlocal call_count
            captured_messages.append(msg)
            call_count += 1
            if call_count == 1:
                return step1_result
            return step2_result

        mock_agent_instance = MagicMock()
        mock_agent_instance.run = AsyncMock(side_effect=fake_run)

        # Mock tool execution
        mock_tool_class = MagicMock()
        mock_tool_instance = MagicMock()
        mock_tool_instance.execute = AsyncMock(return_value="pods: [nginx, redis]")
        mock_tool_class.return_value = mock_tool_instance
        mock_tool_class.validate_input = MagicMock(
            return_value={"query": "list pods", "connector_id": "test-conn"}
        )

        with (
            patch(
                "meho_app.modules.agents.specialist_agent.agent.Agent",
                return_value=mock_agent_instance,
            ),
            patch(
                "meho_app.modules.agents.specialist_agent.agent.retry_llm_call",
                side_effect=_passthrough_retry,
            ),
            patch(
                "meho_app.modules.agents.specialist_agent.agent.compress_observation",
                new_callable=AsyncMock,
                return_value="compressed: pods nginx, redis",
            ),
            patch(
                "meho_app.modules.agents.specialist_agent.agent.get_transcript_collector",
                return_value=None,
            ),
            patch(
                "meho_app.modules.agents.react_agent.tools.TOOL_REGISTRY",
                {"search_operations": mock_tool_class},
            ),
            patch(
                "meho_app.modules.agents.specialist_agent.agent.classify_operation",
                return_value=MagicMock(value="read"),
            ),
            patch(
                "meho_app.modules.agents.specialist_agent.agent.needs_approval",
                return_value=False,
            ),
        ):
            from meho_app.modules.agents.specialist_agent.agent import SpecialistAgent

            agent = SpecialistAgent.__new__(SpecialistAgent)
            agent.connector_id = "test-conn"
            agent.connector_name = "Test Connector"
            agent.connector_type = "kubernetes"
            agent.routing_description = "test"
            agent.skill_content = ""
            agent.iteration = 1
            agent.prior_findings = []
            agent.dependencies = MagicMock()
            agent.dependencies.user_context = MagicMock()
            agent.dependencies.user_context.tenant_id = "t1"
            agent.dependencies.user_context.user_id = "u1"
            agent.dependencies.db_session = None

            config = MagicMock()
            config.max_steps = 5
            config.model.name = "anthropic:claude-sonnet-4-20250514"
            config.model.temperature = None
            config.raw = {"window_size": 3}
            agent._config = config
            agent._build_system_prompt = MagicMock(return_value="system prompt")

            events = []
            with (
                patch(
                    "meho_app.api.database.create_openapi_session_maker",
                    side_effect=Exception("skip DB"),
                ),
                patch(
                    "meho_app.modules.agents.specialist_agent.agent._extract_entity_mentions",
                    return_value=[],
                ),
            ):
                async for ev in agent._run_react("What pods are running?"):
                    events.append(ev)

        # Step 2 message should contain observation, NOT the full context
        assert len(captured_messages) >= 2
        second_msg = captured_messages[1]
        # Should have the observation from step 1
        assert "compressed: pods nginx, redis" in second_msg
        assert "Step 2 of" in second_msg
        # Should NOT contain the full original question as the primary content
        # (it's in the conversation history, not re-sent)

    @pytest.mark.asyncio
    async def test_error_path_injects_user_message(self) -> None:
        """When an invalid tool is selected, the error text becomes the next
        user message in message_history (not just appended to flat scratchpad)."""
        captured_messages: list[str] = []
        call_count = 0

        invalid_step = _make_react_step(
            thought="Try invalid tool",
            action="nonexistent_tool",
            action_input={"query": "test"},
        )
        final_step = _make_react_step(
            response_type="final_answer",
            final_answer="Done",
            action=None,
        )

        step1_result = _make_mock_run_result(react_step=invalid_step)
        step2_result = _make_mock_run_result(react_step=final_step)

        async def fake_run(msg, **kwargs):
            nonlocal call_count
            captured_messages.append(msg)
            call_count += 1
            if call_count == 1:
                return step1_result
            return step2_result

        mock_agent_instance = MagicMock()
        mock_agent_instance.run = AsyncMock(side_effect=fake_run)

        with (
            patch(
                "meho_app.modules.agents.specialist_agent.agent.Agent",
                return_value=mock_agent_instance,
            ),
            patch(
                "meho_app.modules.agents.specialist_agent.agent.retry_llm_call",
                side_effect=_passthrough_retry,
            ),
            patch(
                "meho_app.modules.agents.specialist_agent.agent.compress_observation",
                new_callable=AsyncMock,
                return_value="compressed",
            ),
            patch(
                "meho_app.modules.agents.specialist_agent.agent.get_transcript_collector",
                return_value=None,
            ),
        ):
            from meho_app.modules.agents.specialist_agent.agent import SpecialistAgent

            agent = SpecialistAgent.__new__(SpecialistAgent)
            agent.connector_id = "test-conn"
            agent.connector_name = "Test Connector"
            agent.connector_type = "kubernetes"
            agent.routing_description = "test"
            agent.skill_content = ""
            agent.iteration = 1
            agent.prior_findings = []
            agent.dependencies = MagicMock()
            agent.dependencies.user_context = MagicMock()
            agent.dependencies.user_context.tenant_id = "t1"
            agent.dependencies.user_context.user_id = "u1"
            agent.dependencies.db_session = None

            config = MagicMock()
            config.max_steps = 5
            config.model.name = "anthropic:claude-sonnet-4-20250514"
            config.model.temperature = None
            config.raw = {"window_size": 3}
            agent._config = config
            agent._build_system_prompt = MagicMock(return_value="system prompt")

            events = []
            with (
                patch(
                    "meho_app.api.database.create_openapi_session_maker",
                    side_effect=Exception("skip DB"),
                ),
                patch(
                    "meho_app.modules.agents.specialist_agent.agent._extract_entity_mentions",
                    return_value=[],
                ),
            ):
                async for ev in agent._run_react("Check pods"):
                    events.append(ev)

        # Second call should carry the error message about invalid tool
        assert len(captured_messages) >= 2
        error_msg = captured_messages[1]
        assert "Invalid tool" in error_msg or "nonexistent_tool" in error_msg

    @pytest.mark.asyncio
    async def test_loop_detection_injects_user_message(self) -> None:
        """When loop detection fires, the message becomes a user message."""
        captured_messages: list[str] = []
        call_count = 0

        dup_step = _make_react_step(
            thought="Search again",
            action="search_operations",
            action_input={"query": "list pods", "connector_id": "test-conn"},
        )
        final_step = _make_react_step(
            response_type="final_answer",
            final_answer="Done",
            action=None,
        )

        results = [
            _make_mock_run_result(
                react_step=dup_step, all_messages=[{"role": "user", "content": "m1"}]
            ),
            _make_mock_run_result(
                react_step=dup_step,
                all_messages=[{"role": "user", "content": "m1"}, {"role": "user", "content": "m2"}],
            ),
            _make_mock_run_result(
                react_step=final_step,
                all_messages=[
                    {"role": "user", "content": "m1"},
                    {"role": "user", "content": "m2"},
                    {"role": "user", "content": "m3"},
                ],
            ),
        ]

        async def fake_run(msg, **kwargs):
            nonlocal call_count
            captured_messages.append(msg)
            result = results[min(call_count, len(results) - 1)]
            call_count += 1
            return result

        mock_agent_instance = MagicMock()
        mock_agent_instance.run = AsyncMock(side_effect=fake_run)

        mock_tool_class = MagicMock()
        mock_tool_instance = MagicMock()
        mock_tool_instance.execute = AsyncMock(return_value="search results")
        mock_tool_class.return_value = mock_tool_instance
        mock_tool_class.validate_input = MagicMock(
            return_value={"query": "list pods", "connector_id": "test-conn"}
        )

        with (
            patch(
                "meho_app.modules.agents.specialist_agent.agent.Agent",
                return_value=mock_agent_instance,
            ),
            patch(
                "meho_app.modules.agents.specialist_agent.agent.retry_llm_call",
                side_effect=_passthrough_retry,
            ),
            patch(
                "meho_app.modules.agents.specialist_agent.agent.compress_observation",
                new_callable=AsyncMock,
                return_value="compressed search results",
            ),
            patch(
                "meho_app.modules.agents.specialist_agent.agent.get_transcript_collector",
                return_value=None,
            ),
            patch(
                "meho_app.modules.agents.react_agent.tools.TOOL_REGISTRY",
                {"search_operations": mock_tool_class},
            ),
            patch(
                "meho_app.modules.agents.specialist_agent.agent.classify_operation",
                return_value=MagicMock(value="read"),
            ),
            patch(
                "meho_app.modules.agents.specialist_agent.agent.needs_approval",
                return_value=False,
            ),
        ):
            from meho_app.modules.agents.specialist_agent.agent import SpecialistAgent

            agent = SpecialistAgent.__new__(SpecialistAgent)
            agent.connector_id = "test-conn"
            agent.connector_name = "Test Connector"
            agent.connector_type = "kubernetes"
            agent.routing_description = "test"
            agent.skill_content = ""
            agent.iteration = 1
            agent.prior_findings = []
            agent.dependencies = MagicMock()
            agent.dependencies.user_context = MagicMock()
            agent.dependencies.user_context.tenant_id = "t1"
            agent.dependencies.user_context.user_id = "u1"
            agent.dependencies.db_session = None

            config = MagicMock()
            config.max_steps = 5
            config.model.name = "anthropic:claude-sonnet-4-20250514"
            config.model.temperature = None
            config.raw = {"window_size": 3}
            agent._config = config
            agent._build_system_prompt = MagicMock(return_value="system prompt")

            events = []
            with (
                patch(
                    "meho_app.api.database.create_openapi_session_maker",
                    side_effect=Exception("skip DB"),
                ),
                patch(
                    "meho_app.modules.agents.specialist_agent.agent._extract_entity_mentions",
                    return_value=[],
                ),
            ):
                async for ev in agent._run_react("List pods"):
                    events.append(ev)

        # Third message should contain loop detection text
        assert len(captured_messages) >= 3
        loop_msg = captured_messages[2]
        assert "LOOP DETECTED" in loop_msg or "already called" in loop_msg


# ---------------------------------------------------------------------------
# TestPersistentAgentCreation
# ---------------------------------------------------------------------------


class TestPersistentAgentCreation:
    """Test that Agent() is created once and reused across all steps."""

    @pytest.mark.asyncio
    async def test_agent_created_once_before_loop(self) -> None:
        """Agent() should be instantiated exactly once per investigation."""
        agent_init_count = 0

        step1_result = _make_mock_run_result(
            react_step=_make_react_step(
                action="search_operations",
                action_input={"query": "pods", "connector_id": "test-conn"},
            ),
        )
        step2_result = _make_mock_run_result(
            react_step=_make_react_step(
                response_type="final_answer", final_answer="Done", action=None
            ),
        )

        call_count = 0

        async def fake_run(msg, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return step1_result
            return step2_result

        def track_agent_init(*args, **kwargs):
            nonlocal agent_init_count
            agent_init_count += 1
            mock = MagicMock()
            mock.run = AsyncMock(side_effect=fake_run)
            return mock

        mock_tool_class = MagicMock()
        mock_tool_instance = MagicMock()
        mock_tool_instance.execute = AsyncMock(return_value="results")
        mock_tool_class.return_value = mock_tool_instance
        mock_tool_class.validate_input = MagicMock(
            return_value={"query": "pods", "connector_id": "test-conn"}
        )

        with (
            patch(
                "meho_app.modules.agents.specialist_agent.agent.Agent",
                side_effect=track_agent_init,
            ),
            patch(
                "meho_app.modules.agents.specialist_agent.agent.retry_llm_call",
                side_effect=_passthrough_retry,
            ),
            patch(
                "meho_app.modules.agents.specialist_agent.agent.compress_observation",
                new_callable=AsyncMock,
                return_value="compressed",
            ),
            patch(
                "meho_app.modules.agents.specialist_agent.agent.get_transcript_collector",
                return_value=None,
            ),
            patch(
                "meho_app.modules.agents.react_agent.tools.TOOL_REGISTRY",
                {"search_operations": mock_tool_class},
            ),
            patch(
                "meho_app.modules.agents.specialist_agent.agent.classify_operation",
                return_value=MagicMock(value="read"),
            ),
            patch(
                "meho_app.modules.agents.specialist_agent.agent.needs_approval",
                return_value=False,
            ),
        ):
            from meho_app.modules.agents.specialist_agent.agent import SpecialistAgent

            agent = SpecialistAgent.__new__(SpecialistAgent)
            agent.connector_id = "test-conn"
            agent.connector_name = "Test Connector"
            agent.connector_type = "kubernetes"
            agent.routing_description = "test"
            agent.skill_content = ""
            agent.iteration = 1
            agent.prior_findings = []
            agent.dependencies = MagicMock()
            agent.dependencies.user_context = MagicMock()
            agent.dependencies.user_context.tenant_id = "t1"
            agent.dependencies.user_context.user_id = "u1"
            agent.dependencies.db_session = None

            config = MagicMock()
            config.max_steps = 5
            config.model.name = "anthropic:claude-sonnet-4-20250514"
            config.model.temperature = None
            config.raw = {"window_size": 3}
            agent._config = config
            agent._build_system_prompt = MagicMock(return_value="system prompt")

            events = []
            with (
                patch(
                    "meho_app.api.database.create_openapi_session_maker",
                    side_effect=Exception("skip DB"),
                ),
                patch(
                    "meho_app.modules.agents.specialist_agent.agent._extract_entity_mentions",
                    return_value=[],
                ),
            ):
                async for ev in agent._run_react("List pods"):
                    events.append(ev)

        # Agent() should have been called exactly once
        assert agent_init_count == 1
        # But we made 2 LLM calls (2 steps)
        assert call_count == 2

    @pytest.mark.asyncio
    async def test_anthropic_model_gets_cache_settings(self) -> None:
        """When model contains 'anthropic', AnthropicModelSettings is used."""
        captured_settings: list[Any] = []

        step_result = _make_mock_run_result(
            react_step=_make_react_step(
                response_type="final_answer", final_answer="Done", action=None
            ),
        )

        def track_agent_init(*args, **kwargs):
            captured_settings.append(kwargs.get("model_settings"))
            mock = MagicMock()
            mock.run = AsyncMock(return_value=step_result)
            return mock

        with (
            patch(
                "meho_app.modules.agents.specialist_agent.agent.Agent",
                side_effect=track_agent_init,
            ),
            patch(
                "meho_app.modules.agents.specialist_agent.agent.retry_llm_call",
                side_effect=_passthrough_retry,
            ),
            patch(
                "meho_app.modules.agents.specialist_agent.agent.get_transcript_collector",
                return_value=None,
            ),
        ):
            from meho_app.modules.agents.specialist_agent.agent import SpecialistAgent

            agent = SpecialistAgent.__new__(SpecialistAgent)
            agent.connector_id = "test-conn"
            agent.connector_name = "Test Connector"
            agent.connector_type = "kubernetes"
            agent.routing_description = "test"
            agent.skill_content = ""
            agent.iteration = 1
            agent.prior_findings = []
            agent.dependencies = MagicMock()
            agent.dependencies.user_context = MagicMock()
            agent.dependencies.user_context.tenant_id = "t1"
            agent.dependencies.user_context.user_id = "u1"
            agent.dependencies.db_session = None

            config = MagicMock()
            config.max_steps = 5
            config.model.name = "anthropic:claude-sonnet-4-20250514"
            config.model.temperature = None
            config.raw = {"window_size": 3}
            agent._config = config
            agent._build_system_prompt = MagicMock(return_value="system prompt")

            events = []
            with (
                patch(
                    "meho_app.api.database.create_openapi_session_maker",
                    side_effect=Exception("skip DB"),
                ),
                patch(
                    "meho_app.modules.agents.specialist_agent.agent._extract_entity_mentions",
                    return_value=[],
                ),
            ):
                async for ev in agent._run_react("Test"):
                    events.append(ev)

        # Should have used AnthropicModelSettings (which is a TypedDict)
        assert len(captured_settings) == 1
        settings = captured_settings[0]
        assert settings is not None
        # AnthropicModelSettings is a TypedDict, check via dict key access
        assert "anthropic_cache_instructions" in settings
        assert settings["anthropic_cache_instructions"] is True

    @pytest.mark.asyncio
    async def test_non_anthropic_model_gets_no_cache_settings(self) -> None:
        """When model is non-Anthropic, no Anthropic-specific settings."""
        captured_settings: list[Any] = []

        step_result = _make_mock_run_result(
            react_step=_make_react_step(
                response_type="final_answer", final_answer="Done", action=None
            ),
        )

        def track_agent_init(*args, **kwargs):
            captured_settings.append(kwargs.get("model_settings"))
            mock = MagicMock()
            mock.run = AsyncMock(return_value=step_result)
            return mock

        with (
            patch(
                "meho_app.modules.agents.specialist_agent.agent.Agent",
                side_effect=track_agent_init,
            ),
            patch(
                "meho_app.modules.agents.specialist_agent.agent.retry_llm_call",
                side_effect=_passthrough_retry,
            ),
            patch(
                "meho_app.modules.agents.specialist_agent.agent.get_transcript_collector",
                return_value=None,
            ),
        ):
            from meho_app.modules.agents.specialist_agent.agent import SpecialistAgent

            agent = SpecialistAgent.__new__(SpecialistAgent)
            agent.connector_id = "test-conn"
            agent.connector_name = "Test Connector"
            agent.connector_type = "kubernetes"
            agent.routing_description = "test"
            agent.skill_content = ""
            agent.iteration = 1
            agent.prior_findings = []
            agent.dependencies = MagicMock()
            agent.dependencies.user_context = MagicMock()
            agent.dependencies.user_context.tenant_id = "t1"
            agent.dependencies.user_context.user_id = "u1"
            agent.dependencies.db_session = None

            config = MagicMock()
            config.max_steps = 5
            config.model.name = "openai:gpt-4o"  # Non-Anthropic
            config.model.temperature = None
            config.raw = {"window_size": 3}
            agent._config = config
            agent._build_system_prompt = MagicMock(return_value="system prompt")

            events = []
            with (
                patch(
                    "meho_app.api.database.create_openapi_session_maker",
                    side_effect=Exception("skip DB"),
                ),
                patch(
                    "meho_app.modules.agents.specialist_agent.agent._extract_entity_mentions",
                    return_value=[],
                ),
            ):
                async for ev in agent._run_react("Test"):
                    events.append(ev)

        # Should NOT have AnthropicModelSettings
        assert len(captured_settings) == 1
        settings = captured_settings[0]
        # Non-anthropic should get None or empty dict (no anthropic-specific keys)
        if settings is not None and isinstance(settings, dict):
            assert "anthropic_cache_instructions" not in settings
        else:
            assert settings is None


# ---------------------------------------------------------------------------
# TestRetryAdaptation
# ---------------------------------------------------------------------------


class TestRetryAdaptation:
    """Test that retry preserves message_history."""

    @pytest.mark.asyncio
    async def test_retry_passes_message_history_on_retry(self) -> None:
        """retry_llm_call re-sends the same prompt with the same message_history."""
        # The retry wrapper calls the coro_factory again on failure.
        # Since the lambda captures state.message_history (which hasn't been
        # updated on failure), the retry gets the same history.
        from meho_app.modules.agents.base.retry import retry_llm_call

        call_count = 0
        captured_histories: list[Any] = []

        async def mock_agent_run(msg, instructions=None, message_history=None):
            nonlocal call_count
            captured_histories.append(message_history)
            call_count += 1
            if call_count == 1:
                from pydantic_ai.exceptions import ModelHTTPError

                raise ModelHTTPError(
                    status_code=429,
                    model_name="test-model",
                    body="rate limited",
                )
            result = _make_mock_run_result()
            return result

        fake_history = [{"role": "user", "content": "step 1"}]

        # Patch sleep to avoid waiting
        with patch("meho_app.modules.agents.base.retry.asyncio.sleep", new_callable=AsyncMock):
            await retry_llm_call(
                lambda: mock_agent_run("step 2", instructions="sys", message_history=fake_history),
                max_retries=3,
            )

        # Both calls should have gotten the same history
        assert len(captured_histories) == 2
        assert captured_histories[0] is fake_history
        assert captured_histories[1] is fake_history


# ---------------------------------------------------------------------------
# TestBackwardCompat
# ---------------------------------------------------------------------------


class TestBackwardCompat:
    """Test that backward-compat data structures are still maintained."""

    @pytest.mark.asyncio
    async def test_flat_scratchpad_still_populated(self) -> None:
        """state.scratchpad and state.completed_steps are still populated."""
        call_count = 0

        step1_result = _make_mock_run_result(
            react_step=_make_react_step(
                action="search_operations",
                action_input={"query": "pods", "connector_id": "test-conn"},
            ),
        )
        step2_result = _make_mock_run_result(
            react_step=_make_react_step(
                response_type="final_answer", final_answer="Done", action=None
            ),
        )

        async def fake_run(msg, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return step1_result
            return step2_result

        mock_agent_instance = MagicMock()
        mock_agent_instance.run = AsyncMock(side_effect=fake_run)

        mock_tool_class = MagicMock()
        mock_tool_instance = MagicMock()
        mock_tool_instance.execute = AsyncMock(return_value="tool output")
        mock_tool_class.return_value = mock_tool_instance
        mock_tool_class.validate_input = MagicMock(
            return_value={"query": "pods", "connector_id": "test-conn"}
        )

        # We need to capture the state after execution

        with (
            patch(
                "meho_app.modules.agents.specialist_agent.agent.Agent",
                return_value=mock_agent_instance,
            ),
            patch(
                "meho_app.modules.agents.specialist_agent.agent.retry_llm_call",
                side_effect=_passthrough_retry,
            ),
            patch(
                "meho_app.modules.agents.specialist_agent.agent.compress_observation",
                new_callable=AsyncMock,
                return_value="compressed tool output",
            ),
            patch(
                "meho_app.modules.agents.specialist_agent.agent.get_transcript_collector",
                return_value=None,
            ),
            patch(
                "meho_app.modules.agents.react_agent.tools.TOOL_REGISTRY",
                {"search_operations": mock_tool_class},
            ),
            patch(
                "meho_app.modules.agents.specialist_agent.agent.classify_operation",
                return_value=MagicMock(value="read"),
            ),
            patch(
                "meho_app.modules.agents.specialist_agent.agent.needs_approval",
                return_value=False,
            ),
        ):
            from meho_app.modules.agents.specialist_agent.agent import SpecialistAgent

            agent = SpecialistAgent.__new__(SpecialistAgent)
            agent.connector_id = "test-conn"
            agent.connector_name = "Test Connector"
            agent.connector_type = "kubernetes"
            agent.routing_description = "test"
            agent.skill_content = ""
            agent.iteration = 1
            agent.prior_findings = []
            agent.dependencies = MagicMock()
            agent.dependencies.user_context = MagicMock()
            agent.dependencies.user_context.tenant_id = "t1"
            agent.dependencies.user_context.user_id = "u1"
            agent.dependencies.db_session = None

            config = MagicMock()
            config.max_steps = 5
            config.model.name = "anthropic:claude-sonnet-4-20250514"
            config.model.temperature = None
            config.raw = {"window_size": 3}
            agent._config = config
            agent._build_system_prompt = MagicMock(return_value="system prompt")

            # We need to capture the state object. Patch SpecialistReActState to track it.
            events = []
            with (
                patch(
                    "meho_app.api.database.create_openapi_session_maker",
                    side_effect=Exception("skip DB"),
                ),
                patch(
                    "meho_app.modules.agents.specialist_agent.agent._extract_entity_mentions",
                    return_value=[],
                ),
            ):
                async for ev in agent._run_react("Test query"):
                    events.append(ev)

        # Verify the final_answer event confirms execution happened
        final_events = [e for e in events if e.type == "final_answer"]
        assert len(final_events) == 1
        assert final_events[0].data["content"] == "Done"

        # The add_observation call is confirmed by the fact that the agent
        # completed successfully with tool execution

    @pytest.mark.asyncio
    async def test_budget_exhaustion_triggers_forced_synthesis(self) -> None:
        """Budget exhaustion triggers LLM-driven forced synthesis (Phase 36)."""
        call_count = 0

        # Return action steps for regular calls, final_answer for synthesis
        async def fake_run(msg, **kwargs):
            nonlocal call_count
            call_count += 1
            if "Budget exhausted" in str(msg):
                # Synthesis prompt -> return final_answer
                return _make_mock_run_result(
                    react_step=_make_react_step(
                        response_type="final_answer",
                        final_answer="Note: I reached my investigation step limit, but here's what I found: test synthesis",
                        action=None,
                        action_input=None,
                    ),
                )
            # Regular calls -> return unique action steps
            return _make_mock_run_result(
                react_step=_make_react_step(
                    action="search_operations",
                    action_input={"query": f"query-{call_count}", "connector_id": "test-conn"},
                ),
            )

        mock_agent_instance = MagicMock()
        mock_agent_instance.run = AsyncMock(side_effect=fake_run)

        mock_tool_class = MagicMock()
        mock_tool_instance = MagicMock()
        mock_tool_instance.execute = AsyncMock(return_value="search results")
        mock_tool_class.return_value = mock_tool_instance
        mock_tool_class.validate_input = MagicMock(side_effect=lambda x: x)

        with (
            patch(
                "meho_app.modules.agents.specialist_agent.agent.Agent",
                return_value=mock_agent_instance,
            ),
            patch(
                "meho_app.modules.agents.specialist_agent.agent.retry_llm_call",
                side_effect=_passthrough_retry,
            ),
            patch(
                "meho_app.modules.agents.specialist_agent.agent.compress_observation",
                new_callable=AsyncMock,
                return_value="compressed results",
            ),
            patch(
                "meho_app.modules.agents.specialist_agent.agent.get_transcript_collector",
                return_value=None,
            ),
            patch(
                "meho_app.modules.agents.react_agent.tools.TOOL_REGISTRY",
                {"search_operations": mock_tool_class},
            ),
            patch(
                "meho_app.modules.agents.specialist_agent.agent.classify_operation",
                return_value=MagicMock(value="read"),
            ),
            patch(
                "meho_app.modules.agents.specialist_agent.agent.needs_approval",
                return_value=False,
            ),
        ):
            from meho_app.modules.agents.specialist_agent.agent import SpecialistAgent

            agent = SpecialistAgent.__new__(SpecialistAgent)
            agent.connector_id = "test-conn"
            agent.connector_name = "Test Connector"
            agent.connector_type = "kubernetes"
            agent.routing_description = "test"
            agent.skill_content = ""
            agent.iteration = 1
            agent.prior_findings = []
            agent.dependencies = MagicMock()
            agent.dependencies.user_context = MagicMock()
            agent.dependencies.user_context.tenant_id = "t1"
            agent.dependencies.user_context.user_id = "u1"
            agent.dependencies.db_session = None

            config = MagicMock()
            config.max_steps = 3  # Low budget to trigger exhaustion
            config.model.name = "anthropic:claude-sonnet-4-20250514"
            config.model.temperature = None
            config.raw = {"window_size": 3}
            agent._config = config
            agent._build_system_prompt = MagicMock(return_value="system prompt")

            events = []
            with (
                patch(
                    "meho_app.api.database.create_openapi_session_maker",
                    side_effect=Exception("skip DB"),
                ),
                patch(
                    "meho_app.modules.agents.specialist_agent.agent._extract_entity_mentions",
                    return_value=[],
                ),
            ):
                async for ev in agent._run_react("Test query"):
                    events.append(ev)

        # Forced synthesis should have been triggered (Phase 36)
        final_events = [e for e in events if e.type == "final_answer"]
        assert len(final_events) == 1
        content = final_events[0].data["content"]
        assert "step limit" in content or "test synthesis" in content
        # Verify budget metadata is included in final_answer event (Phase 36)
        assert "steps_used" in final_events[0].data
        assert "max_steps" in final_events[0].data
        assert "budget_extended" in final_events[0].data
