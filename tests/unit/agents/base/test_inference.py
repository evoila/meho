# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""Tests for the infer() quick inference utility.

These tests verify:
1. infer() function is importable
2. Function signature and overloads are correct
3. Works with mocked PydanticAI agent
4. Aliases (quick_llm, one_shot) work
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic import BaseModel

from meho_app.modules.agents.base.inference import (
    infer,
    one_shot,
    quick_llm,
    reset_agent,
)


class SampleOutput(BaseModel):
    """Sample output model for testing structured output."""

    answer: str
    confidence: float


class TestInferImports:
    """Tests for module imports."""

    def test_infer_importable_from_base(self) -> None:
        """infer should be importable from base module."""
        from meho_app.modules.agents.base import infer as base_infer

        assert base_infer is infer

    def test_aliases_importable(self) -> None:
        """Aliases should be importable from base module."""
        from meho_app.modules.agents.base import one_shot as os
        from meho_app.modules.agents.base import quick_llm as ql

        assert os is infer
        assert ql is infer


class TestInferWithMock:
    """Tests for infer() with mocked PydanticAI."""

    @pytest.fixture(autouse=True)
    def reset_cached_agent(self) -> None:
        """Reset the cached agent before each test."""
        reset_agent()

    @pytest.mark.asyncio
    async def test_infer_string_output(self) -> None:
        """infer() should return string when no output_schema."""
        mock_result = MagicMock()
        mock_result.output = "This is the answer"

        mock_agent = MagicMock()
        mock_agent.run = AsyncMock(return_value=mock_result)

        with patch(
            "meho_app.modules.agents.base.inference._get_agent",
            return_value=mock_agent,
        ):
            result = await infer(
                system_prompt="Answer the question.",
                message="What is 2+2?",
            )

        assert result == "This is the answer"
        mock_agent.run.assert_called_once()

    @pytest.mark.asyncio
    async def test_infer_structured_output(self) -> None:
        """infer() should return Pydantic model when output_schema provided."""
        expected_output = SampleOutput(answer="Yes", confidence=0.95)

        mock_result = MagicMock()
        mock_result.output = expected_output

        mock_agent = MagicMock()
        mock_agent.run = AsyncMock(return_value=mock_result)
        mock_agent.model = "openai:gpt-4.1-mini"

        with patch(  # noqa: SIM117 -- readability preferred over combined with
            "meho_app.modules.agents.base.inference._get_agent",
            return_value=mock_agent,
        ):
            # Patch Agent at pydantic_ai since it's imported inside function
            with patch("pydantic_ai.Agent") as MockAgent:
                MockAgent.return_value = mock_agent
                result = await infer(
                    system_prompt="Answer yes or no.",
                    message="Is the sky blue?",
                    output_schema=SampleOutput,
                )

        assert isinstance(result, SampleOutput)
        assert result.answer == "Yes"
        assert result.confidence == pytest.approx(0.95)

    @pytest.mark.asyncio
    async def test_infer_with_model_override(self) -> None:
        """infer() should use specified model when provided."""
        mock_result = MagicMock()
        mock_result.output = "Custom model response"

        mock_agent = MagicMock()
        mock_agent.run = AsyncMock(return_value=mock_result)

        # Patch Agent at pydantic_ai since it's imported inside function
        with patch("pydantic_ai.Agent") as MockAgent:
            MockAgent.return_value = mock_agent
            result = await infer(
                system_prompt="Test prompt",
                message="Test message",
                model="openai:o1",
            )

        assert result == "Custom model response"
        # Should have created agent with custom model
        MockAgent.assert_called()

    @pytest.mark.asyncio
    async def test_infer_with_temperature(self) -> None:
        """infer() should pass temperature to agent."""
        mock_result = MagicMock()
        mock_result.output = "Response"

        mock_agent = MagicMock()
        mock_agent.run = AsyncMock(return_value=mock_result)

        with patch(
            "meho_app.modules.agents.base.inference._get_agent",
            return_value=mock_agent,
        ):
            await infer(
                system_prompt="Be creative",
                message="Write a poem",
                temperature=0.8,
            )

        # Check that model_settings was passed
        call_kwargs = mock_agent.run.call_args.kwargs
        assert call_kwargs.get("model_settings") == {"temperature": 0.8}

    @pytest.mark.asyncio
    async def test_infer_passes_system_prompt_as_instructions(self) -> None:
        """infer() should pass system_prompt as instructions."""
        mock_result = MagicMock()
        mock_result.output = "Response"

        mock_agent = MagicMock()
        mock_agent.run = AsyncMock(return_value=mock_result)

        with patch(
            "meho_app.modules.agents.base.inference._get_agent",
            return_value=mock_agent,
        ):
            await infer(
                system_prompt="You are a helpful assistant.",
                message="Hello!",
            )

        call_kwargs = mock_agent.run.call_args.kwargs
        assert call_kwargs.get("instructions") == "You are a helpful assistant."


class TestInferAliases:
    """Tests for convenience aliases."""

    def test_quick_llm_is_infer(self) -> None:
        """quick_llm should be alias for infer."""
        assert quick_llm is infer

    def test_one_shot_is_infer(self) -> None:
        """one_shot should be alias for infer."""
        assert one_shot is infer


class TestResetAgent:
    """Tests for reset_agent function."""

    def test_reset_agent_clears_cached_agent(self) -> None:
        """reset_agent should clear the cached agent."""
        from meho_app.modules.agents.base import inference

        # Set a dummy agent
        inference._inference_agent = MagicMock()

        # Reset
        reset_agent()

        # Should be None
        assert inference._inference_agent is None


class TestInferTypeHints:
    """Tests to verify type hint correctness (static analysis)."""

    def test_overload_signatures_exist(self) -> None:
        """infer should have overloaded signatures."""
        import inspect

        # Get the function's annotations
        sig = inspect.signature(infer)
        params = sig.parameters

        # Should have expected parameters
        assert "system_prompt" in params
        assert "message" in params
        assert "output_schema" in params
        assert "model" in params
        assert "temperature" in params
